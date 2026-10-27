"""
monitor.py - the website-checking logic (no UI, no database).

Main entry points:
    validate_url(text)      -> a clean, safe URL, or raises InvalidURLError
    check_website(url)      -> CheckResult   (NEVER raises)
    check_websites(urls)    -> list of CheckResult (one failure never stops the others)

Status rules
    UP        the site answered with HTTP < 400 and was fast enough
    DEGRADED  the site is reachable but something is wrong: HTTP 4xx (e.g. 404),
              a slow response, or a redirect we refuse to follow
    DOWN      the site could not be reached (timeout, DNS, SSL, connection
              failure), or it answered with HTTP 5xx, or it redirects forever

A 404 therefore means "reachable, but the page is missing" - NOT down.
`CheckResult.reachable` is True whenever the server sent any HTTP response.
"""
from __future__ import annotations

import ipaddress
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse, urlunparse

import requests

import config

UP = "UP"
DOWN = "DOWN"
DEGRADED = "DEGRADED"

REDIRECT_CODES = {301, 302, 303, 307, 308}
_timer = time.perf_counter  # separate name so tests can fake the clock

_CLIENT_ERROR_HINTS = {
    401: "authentication required",
    403: "access forbidden",
    404: "page not found",
    429: "too many requests",
}

_SSL_REASONS = (
    ("has expired", "the certificate has expired"),
    ("self-signed", "the certificate is self-signed"),
    ("self signed", "the certificate is self-signed"),
    ("hostname mismatch", "the certificate does not match the domain"),
    ("doesn't match", "the certificate does not match the domain"),
    ("unable to get local issuer", "the certificate authority is not trusted"),
)

_DNS_MARKERS = (
    "name or service not known",
    "nodename nor servname",
    "getaddrinfo failed",
    "temporary failure in name resolution",
    "failed to resolve",
    "name resolution",
    "no address associated with hostname",
)


# --------------------------------------------------------------------------
# Data types
# --------------------------------------------------------------------------
class InvalidURLError(ValueError):
    """The text is not a usable http(s) URL."""


class BlockedHostError(InvalidURLError):
    """The URL points at a private, local or internal network address."""


@dataclass
class CheckResult:
    """The outcome of one check of one website."""

    url: str
    status: str  # UP / DOWN / DEGRADED
    checked_at: str  # ISO timestamp in UTC
    response_time_ms: float | None = None  # None when there was no HTTP response
    status_code: int | None = None  # None when the site was unreachable
    error_type: str | None = None  # short machine-friendly label
    error_message: str | None = None  # human-friendly explanation

    @property
    def reachable(self) -> bool:
        return self.status_code is not None

    def to_dict(self) -> dict:
        return asdict(self)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# URL validation
# --------------------------------------------------------------------------
def normalize_url(raw_url) -> str:
    """Clean up user input and check it is a well-formed http(s) URL.

    'example.com' becomes 'https://example.com'. Raises InvalidURLError otherwise.
    """
    if not isinstance(raw_url, str):
        raise InvalidURLError("The URL must be text.")
    url = raw_url.strip()
    if not url:
        raise InvalidURLError("The URL is empty.")
    if len(url) > config.MAX_URL_LENGTH:
        raise InvalidURLError("The URL is too long.")
    if any(ch.isspace() for ch in url):
        raise InvalidURLError("The URL must not contain spaces.")
    if "://" not in url:
        url = "https://" + url

    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        parsed.port  # noqa: B018 - raises ValueError if the port is not a number
    except ValueError:
        raise InvalidURLError("The URL is not valid (check the address and port).")

    if parsed.scheme.lower() not in ("http", "https"):
        raise InvalidURLError("Only http:// and https:// URLs are allowed.")
    if not hostname:
        raise InvalidURLError("The URL has no website address.")
    if parsed.username or parsed.password:
        raise InvalidURLError("URLs containing a username or password are not allowed.")

    cleaned = parsed._replace(scheme=parsed.scheme.lower(), netloc=parsed.netloc.lower(), fragment="")
    return urlunparse(cleaned)


def _resolve_ips(hostname: str) -> list[str]:
    """Look up the IP addresses of a hostname. Returns [] if it cannot be resolved."""
    try:
        infos = socket.getaddrinfo(hostname, None)
    except (socket.gaierror, UnicodeError, OSError):
        return []
    return sorted({info[4][0] for info in infos})


def is_private_host(hostname: str) -> bool:
    """True if the host points at a private, loopback, link-local or otherwise
    non-public address. Hosts that do not resolve are NOT treated as private
    (they are simply reported as DNS failures later)."""
    for ip_text in _resolve_ips(hostname):
        try:
            ip = ipaddress.ip_address(ip_text.split("%")[0])  # drop IPv6 zone id
        except ValueError:
            return True  # unparseable -> be safe
        if ip.version == 6 and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        if not ip.is_global or ip.is_multicast:
            return True
    return False


def validate_url(raw_url) -> str:
    """normalize_url() plus the private-network safety check."""
    url = normalize_url(raw_url)
    if is_private_host(urlparse(url).hostname):
        raise BlockedHostError("Private, local or internal network addresses are not allowed.")
    return url


# --------------------------------------------------------------------------
# Checking
# --------------------------------------------------------------------------
def _failure(url, error_type, message, *, status=DOWN, status_code=None, response_time_ms=None):
    return CheckResult(
        url=str(url),
        status=status,
        checked_at=utc_now_iso(),
        response_time_ms=response_time_ms,
        status_code=status_code,
        error_type=error_type,
        error_message=message,
    )


def _elapsed_ms(started: float) -> float:
    return round((_timer() - started) * 1000, 1)


def _classify(url: str, status_code: int, elapsed_ms: float) -> CheckResult:
    """Turn an HTTP response into UP / DEGRADED / DOWN."""
    if status_code >= 500:
        return _failure(
            url, "http_server_error",
            f"HTTP server error {status_code}: the server is reachable but reported a failure.",
            status=DOWN, status_code=status_code, response_time_ms=elapsed_ms,
        )
    if status_code >= 400:
        hint = _CLIENT_ERROR_HINTS.get(status_code, "the request was rejected")
        return _failure(
            url, "http_client_error",
            f"HTTP {status_code} ({hint}): the website is reachable, but this page returned an error.",
            status=DEGRADED, status_code=status_code, response_time_ms=elapsed_ms,
        )
    if elapsed_ms > config.DEGRADED_THRESHOLD_MS:
        return _failure(
            url, "slow_response",
            f"Slow response: {elapsed_ms:.0f} ms (threshold {config.DEGRADED_THRESHOLD_MS} ms).",
            status=DEGRADED, status_code=status_code, response_time_ms=elapsed_ms,
        )
    return CheckResult(
        url=url, status=UP, checked_at=utc_now_iso(),
        response_time_ms=elapsed_ms, status_code=status_code,
    )


def _ssl_message(exc: Exception) -> str:
    text = str(exc).lower()
    for marker, reason in _SSL_REASONS:
        if marker in text:
            return f"SSL certificate error: {reason}."
    return "SSL certificate error: the certificate could not be verified."


def _check(raw_url) -> CheckResult:
    try:
        url = validate_url(raw_url)
    except BlockedHostError as exc:
        return _failure(raw_url, "blocked_host", str(exc))
    except InvalidURLError as exc:
        return _failure(raw_url, "invalid_url", str(exc))

    current_url = url
    started = _timer()
    try:
        # Redirects are followed by hand so every hop can be safety-checked.
        for _hop in range(config.MAX_REDIRECTS + 1):
            response = requests.get(
                current_url,
                timeout=config.REQUEST_TIMEOUT_SECONDS,
                allow_redirects=False,
                stream=True,  # we only need the headers, not the page body
                headers={"User-Agent": config.USER_AGENT},
            )  # SSL verification stays ON (requests default)
            try:
                status_code = response.status_code
                location = response.headers.get("Location")
            finally:
                response.close()

            if status_code in REDIRECT_CODES and location:
                try:
                    next_url = urljoin(current_url, location.strip()).replace(" ", "%20")
                    current_url = validate_url(next_url)
                except InvalidURLError as exc:
                    return _failure(
                        url, "blocked_redirect", f"Redirect not followed: {exc}",
                        status=DEGRADED, status_code=status_code,
                        response_time_ms=_elapsed_ms(started),
                    )
                continue
            return _classify(url, status_code, _elapsed_ms(started))

        return _failure(
            url, "too_many_redirects",
            f"Too many redirects (more than {config.MAX_REDIRECTS}).",
            status_code=status_code, response_time_ms=_elapsed_ms(started),
        )

    # Order matters: SSLError and the timeout errors are subclasses of ConnectionError.
    except requests.exceptions.SSLError as exc:
        return _failure(url, "ssl_error", _ssl_message(exc))
    except requests.exceptions.Timeout:
        return _failure(url, "timeout", f"Connection timeout after {config.REQUEST_TIMEOUT_SECONDS} seconds.")
    except requests.exceptions.ConnectionError as exc:
        if any(marker in str(exc).lower() for marker in _DNS_MARKERS):
            return _failure(url, "dns_error", "DNS failure: the domain name could not be resolved.")
        return _failure(url, "connection_error", "Connection failed: the server refused, dropped or could not be reached.")
    except requests.exceptions.RequestException as exc:
        return _failure(url, "request_error", f"Request failed: {type(exc).__name__}.")


def check_website(raw_url) -> CheckResult:
    """Check one website. This function never raises - every problem becomes a result."""
    try:
        return _check(raw_url)
    except Exception as exc:  # last-resort safety net
        return _failure(raw_url, "unexpected_error", f"Unexpected error ({type(exc).__name__}).")


def check_websites(urls) -> list[CheckResult]:
    """Check several websites (a few at a time). Results keep the input order."""
    urls = list(urls)
    if not urls:
        return []
    workers = min(config.MAX_PARALLEL_CHECKS, len(urls))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(check_website, urls))

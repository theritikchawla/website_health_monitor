"""Tests for monitor.py. All HTTP requests are mocked - no internet needed."""
from unittest.mock import MagicMock, patch

import pytest
import requests

import config
import monitor


def fake_response(status_code=200, location=None):
    response = MagicMock()
    response.status_code = status_code
    response.headers = {"Location": location} if location else {}
    return response


# --- successful and HTTP-error responses ---------------------------------
def test_successful_response_is_up(monkeypatch):
    monkeypatch.setattr(config, "DEGRADED_THRESHOLD_MS", 2000)
    with patch("monitor.requests.get", return_value=fake_response(200)), \
         patch.object(monitor, "_timer", side_effect=[0.0, 0.123]):
        result = monitor.check_website("https://example.com")
    assert result.status == "UP"
    assert result.status_code == 200
    assert result.response_time_ms == 123.0
    assert result.error_message is None
    assert result.reachable is True


def test_slow_response_is_degraded(monkeypatch):
    monkeypatch.setattr(config, "DEGRADED_THRESHOLD_MS", 2000)
    with patch("monitor.requests.get", return_value=fake_response(200)), \
         patch.object(monitor, "_timer", side_effect=[0.0, 5.0]):
        result = monitor.check_website("https://example.com")
    assert result.status == "DEGRADED"
    assert result.error_type == "slow_response"
    assert result.response_time_ms == 5000.0


def test_404_is_reachable_not_down():
    with patch("monitor.requests.get", return_value=fake_response(404)):
        result = monitor.check_website("https://example.com/missing")
    assert result.status == "DEGRADED"
    assert result.status_code == 404
    assert result.reachable is True
    assert result.error_type == "http_client_error"
    assert "reachable" in result.error_message


def test_500_is_down_but_reachable():
    with patch("monitor.requests.get", return_value=fake_response(503)):
        result = monitor.check_website("https://example.com")
    assert result.status == "DOWN"
    assert result.status_code == 503
    assert result.reachable is True
    assert result.error_type == "http_server_error"


# --- network failures -----------------------------------------------------
@pytest.mark.parametrize(
    "error", [requests.exceptions.ConnectTimeout("t"), requests.exceptions.ReadTimeout("t")]
)
def test_timeout_is_down_and_unreachable(error):
    with patch("monitor.requests.get", side_effect=error):
        result = monitor.check_website("https://example.com")
    assert result.status == "DOWN"
    assert result.error_type == "timeout"
    assert result.status_code is None
    assert result.reachable is False
    assert "timeout" in result.error_message.lower()


def test_connection_failure():
    error = requests.exceptions.ConnectionError("Connection refused")
    with patch("monitor.requests.get", side_effect=error):
        result = monitor.check_website("https://example.com")
    assert result.status == "DOWN"
    assert result.error_type == "connection_error"
    assert result.reachable is False


def test_dns_failure_is_detected():
    error = requests.exceptions.ConnectionError("Failed to resolve 'x.example' (Name or service not known)")
    with patch("monitor.requests.get", side_effect=error):
        result = monitor.check_website("https://x.example")
    assert result.error_type == "dns_error"
    assert "DNS" in result.error_message


def test_ssl_error_is_reported_with_reason():
    error = requests.exceptions.SSLError("certificate verify failed: certificate has expired")
    with patch("monitor.requests.get", side_effect=error):
        result = monitor.check_website("https://example.com")
    assert result.status == "DOWN"
    assert result.error_type == "ssl_error"
    assert "expired" in result.error_message


def test_ssl_verification_is_never_disabled():
    with patch("monitor.requests.get", return_value=fake_response(200)) as get:
        monitor.check_website("https://example.com")
    assert "verify" not in get.call_args.kwargs  # requests default = verify ON
    assert get.call_args.kwargs["allow_redirects"] is False
    assert get.call_args.kwargs["timeout"] == config.REQUEST_TIMEOUT_SECONDS


def test_unexpected_error_never_crashes():
    with patch("monitor.requests.get", side_effect=RuntimeError("boom")):
        result = monitor.check_website("https://example.com")
    assert result.status == "DOWN"
    assert result.error_type == "unexpected_error"


# --- URL validation ---------------------------------------------------------
@pytest.mark.parametrize(
    "bad_url",
    [
        "", "   ", "ftp://example.com", "file:///etc/passwd", "javascript:alert(1)",
        "http://", "https://user:pass@example.com", "exa mple.com", "https://example.com:notaport",
        None, 123,
    ],
)
def test_invalid_urls_are_rejected_without_a_request(bad_url):
    with pytest.raises(monitor.InvalidURLError):
        monitor.validate_url(bad_url)
    with patch("monitor.requests.get") as get:
        result = monitor.check_website(bad_url)
    assert result.status == "DOWN"
    assert result.error_type == "invalid_url"
    get.assert_not_called()


def test_scheme_is_added_and_url_is_normalised():
    assert monitor.normalize_url("example.com") == "https://example.com"
    assert monitor.normalize_url("  HTTP://Example.COM/Path#frag ") == "http://example.com/Path"


# --- private network protection ---------------------------------------------
@pytest.mark.parametrize(
    "ip", ["127.0.0.1", "10.1.2.3", "192.168.1.1", "172.16.0.9", "169.254.169.254", "::1", "100.64.0.1", "::ffff:10.0.0.1"]
)
def test_private_addresses_are_detected(monkeypatch, ip):
    monkeypatch.setattr(monitor, "_resolve_ips", lambda hostname: [ip])
    assert monitor.is_private_host("whatever.example") is True


def test_public_and_unresolvable_hosts_are_not_private(monkeypatch):
    monkeypatch.setattr(monitor, "_resolve_ips", lambda hostname: ["93.184.216.34", "2606:2800:220:1::1"])
    assert monitor.is_private_host("example.com") is False
    monkeypatch.setattr(monitor, "_resolve_ips", lambda hostname: [])
    assert monitor.is_private_host("does-not-exist.example") is False


def test_private_start_url_is_blocked(monkeypatch):
    monkeypatch.setattr(monitor, "_resolve_ips", lambda hostname: ["127.0.0.1"])
    with patch("monitor.requests.get") as get:
        result = monitor.check_website("http://localhost:8000")
    assert result.status == "DOWN"
    assert result.error_type == "blocked_host"
    get.assert_not_called()


# --- redirects ---------------------------------------------------------------
def test_safe_redirect_is_followed():
    responses = [fake_response(301, "https://www.example.com/"), fake_response(200)]
    with patch("monitor.requests.get", side_effect=responses) as get:
        result = monitor.check_website("https://example.com")
    assert result.status == "UP"
    assert result.status_code == 200
    assert get.call_count == 2


def test_redirect_to_private_address_is_blocked(monkeypatch):
    monkeypatch.setattr(
        monitor, "_resolve_ips",
        lambda hostname: ["10.0.0.5"] if hostname == "internal.corp" else ["93.184.216.34"],
    )
    with patch("monitor.requests.get", return_value=fake_response(302, "http://internal.corp/admin")) as get:
        result = monitor.check_website("https://example.com")
    assert result.error_type == "blocked_redirect"
    assert result.status == "DEGRADED"
    assert result.status_code == 302
    assert get.call_count == 1  # the private address was never requested


def test_redirect_loop_is_down():
    with patch("monitor.requests.get", return_value=fake_response(302, "https://example.com/")) as get:
        result = monitor.check_website("https://example.com")
    assert result.status == "DOWN"
    assert result.error_type == "too_many_redirects"
    assert get.call_count == config.MAX_REDIRECTS + 1


# --- many websites -----------------------------------------------------------
def test_one_failure_does_not_stop_the_others():
    def fake_get(url, **kwargs):
        if "bad" in url:
            raise requests.exceptions.ConnectionError("Connection refused")
        return fake_response(200)

    with patch("monitor.requests.get", side_effect=fake_get):
        results = monitor.check_websites(["https://good.example", "https://bad.example", "not a url"])
    assert [r.status for r in results] == ["UP", "DOWN", "DOWN"]
    assert results[2].error_type == "invalid_url"


def test_check_websites_with_no_urls():
    assert monitor.check_websites([]) == []

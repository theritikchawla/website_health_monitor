"""
config.py - all settings in one place.

Change a value here (or set the matching environment variable) instead of
hunting through the code. Nothing in this file is secret: the app needs no
API keys or credentials.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "monitor.db"  # local SQLite file (ignored by Git)


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    """Read a whole-number setting from the environment, falling back safely."""
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return max(value, minimum)


# --- Checking -------------------------------------------------------------
REQUEST_TIMEOUT_SECONDS = _env_int("HM_REQUEST_TIMEOUT", 10)  # per request
DEGRADED_THRESHOLD_MS = _env_int("HM_DEGRADED_MS", 2000)      # slower = DEGRADED
MAX_REDIRECTS = 5
MAX_URL_LENGTH = 2048
MAX_PARALLEL_CHECKS = 5  # at most 5 sites are checked at the same time
USER_AGENT = "WebsiteHealthMonitor/1.0 (portfolio project; monitors sites you own)"

# --- Limits ---------------------------------------------------------------
MAX_WEBSITES = _env_int("HM_MAX_WEBSITES", 20)
RETENTION_DAYS = _env_int("HM_RETENTION_DAYS", 30)  # older history is deleted
HISTORY_ROW_LIMIT = 1000

# --- Scheduling -----------------------------------------------------------
DEFAULT_INTERVAL_SECONDS = 60
MIN_INTERVAL_SECONDS = 30   # protects websites from excessive requests
MAX_INTERVAL_SECONDS = 3600

# --- Dashboard ------------------------------------------------------------
UI_REFRESH_SECONDS = 5  # how often the dashboard re-reads the database

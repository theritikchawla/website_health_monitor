"""
database.py - everything that touches SQLite.

Two tables:
    websites  the list of URLs being monitored (so it survives restarts)
    checks    one row per check: url, time, response time, HTTP code, status, error

Every function opens its own short-lived connection, which makes it safe to use
from both the Streamlit page and the background scheduler thread.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS websites (
    url      TEXT PRIMARY KEY,
    added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS checks (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    url              TEXT NOT NULL,
    checked_at       TEXT NOT NULL,
    response_time_ms REAL,
    status_code      INTEGER,
    status           TEXT NOT NULL,
    error_type       TEXT,
    error_message    TEXT
);
CREATE INDEX IF NOT EXISTS idx_checks_url_time ON checks (url, checked_at);
"""

HISTORY_COLUMNS = "checked_at, status, response_time_ms, status_code, error_type, error_message"


class WebsiteLimitError(Exception):
    """Raised when adding a website would exceed the configured limit."""


@contextmanager
def get_connection(db_path=None):
    """Open a connection, commit on success, roll back on error, always close."""
    path = Path(db_path or config.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path=None) -> None:
    """Create the tables if they do not exist yet (safe to call repeatedly)."""
    with get_connection(db_path) as conn:
        conn.executescript(SCHEMA)


# --------------------------------------------------------------------------
# Monitored websites
# --------------------------------------------------------------------------
def add_website(url: str, db_path=None, max_websites: int | None = None) -> bool:
    """Add a URL. Returns False if it already exists; raises WebsiteLimitError at the limit."""
    limit = config.MAX_WEBSITES if max_websites is None else max_websites
    with get_connection(db_path) as conn:
        if conn.execute("SELECT 1 FROM websites WHERE url = ?", (url,)).fetchone():
            return False
        count = conn.execute("SELECT COUNT(*) FROM websites").fetchone()[0]
        if count >= limit:
            raise WebsiteLimitError(
                f"Limit reached: at most {limit} websites can be monitored. Remove one first."
            )
        cursor = conn.execute(
            "INSERT OR IGNORE INTO websites (url, added_at) VALUES (?, ?)",
            (url, datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )
        return cursor.rowcount == 1


def list_websites(db_path=None) -> list[str]:
    with get_connection(db_path) as conn:
        rows = conn.execute("SELECT url FROM websites ORDER BY added_at, url").fetchall()
    return [row[0] for row in rows]


def remove_website(url: str, db_path=None) -> bool:
    """Remove a website AND its history. Returns True if the website existed."""
    with get_connection(db_path) as conn:
        conn.execute("DELETE FROM checks WHERE url = ?", (url,))
        cursor = conn.execute("DELETE FROM websites WHERE url = ?", (url,))
        return cursor.rowcount > 0


# --------------------------------------------------------------------------
# Check results
# --------------------------------------------------------------------------
def save_result(result, db_path=None) -> None:
    """Store one monitor.CheckResult."""
    data = result.to_dict()
    with get_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO checks (url, checked_at, response_time_ms, status_code, status, error_type, error_message) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                data["url"], data["checked_at"], data["response_time_ms"], data["status_code"],
                data["status"], data["error_type"], data["error_message"],
            ),
        )


def get_history(url: str, limit: int | None = None, db_path=None) -> pd.DataFrame:
    """Most recent checks of one website, newest first."""
    limit = config.HISTORY_ROW_LIMIT if limit is None else limit
    with get_connection(db_path) as conn:
        return pd.read_sql_query(
            f"SELECT {HISTORY_COLUMNS} FROM checks WHERE url = ? "
            "ORDER BY checked_at DESC, id DESC LIMIT ?",
            conn, params=(url, int(limit)),
        )


def get_failures(url: str, limit: int | None = None, db_path=None) -> pd.DataFrame:
    """Previous problems (DOWN or DEGRADED) of one website, newest first."""
    limit = config.HISTORY_ROW_LIMIT if limit is None else limit
    with get_connection(db_path) as conn:
        return pd.read_sql_query(
            f"SELECT {HISTORY_COLUMNS} FROM checks WHERE url = ? AND status != 'UP' "
            "ORDER BY checked_at DESC, id DESC LIMIT ?",
            conn, params=(url, int(limit)),
        )


def get_latest_results(db_path=None) -> pd.DataFrame:
    """One row per monitored website with its most recent check.
    Websites that were never checked have empty (NaN) check columns."""
    with get_connection(db_path) as conn:
        return pd.read_sql_query(
            """
            SELECT w.url, c.status, c.response_time_ms, c.status_code,
                   c.checked_at, c.error_type, c.error_message
            FROM websites w
            LEFT JOIN checks c
              ON c.id = (SELECT MAX(id) FROM checks WHERE url = w.url)
            ORDER BY w.added_at, w.url
            """,
            conn,
        )


def get_stats(url: str, db_path=None) -> dict:
    """Summary of the stored history of one website.

    uptime_pct = share of checks that were not DOWN (DEGRADED still counts as reachable/serving).
    """
    with get_connection(db_path) as conn:
        total, not_down, not_up, avg_ms = conn.execute(
            "SELECT COUNT(*), "
            "SUM(CASE WHEN status != 'DOWN' THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN status != 'UP' THEN 1 ELSE 0 END), "
            "AVG(response_time_ms) FROM checks WHERE url = ?",
            (url,),
        ).fetchone()
    if total == 0:
        return {"total_checks": 0, "uptime_pct": None, "problem_checks": 0, "avg_response_ms": None}
    return {
        "total_checks": total,
        "uptime_pct": round(100.0 * not_down / total, 1),
        "problem_checks": not_up,
        "avg_response_ms": None if avg_ms is None else round(avg_ms, 1),
    }


def delete_old_checks(days: int | None = None, db_path=None) -> int:
    """Delete history older than `days` (keeps the database small). Returns rows removed."""
    days = config.RETENTION_DAYS if days is None else days
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    with get_connection(db_path) as conn:
        return conn.execute("DELETE FROM checks WHERE checked_at < ?", (cutoff,)).rowcount

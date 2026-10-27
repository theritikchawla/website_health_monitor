"""
helpers.py - small pure functions that turn database tables into what the
dashboard shows. Keeping them out of app.py makes them easy to test.
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd

STATUS_LABELS = {"UP": "🟢 UP", "DOWN": "🔴 DOWN", "DEGRADED": "🟡 DEGRADED"}
NOT_CHECKED = "⚪ NOT CHECKED"

STATUS_TABLE_COLUMNS = [
    "Website", "Status", "Response time (ms)", "HTTP status", "Reachable", "Last checked (UTC)", "Details",
]


def status_label(status) -> str:
    if status is None or pd.isna(status):
        return NOT_CHECKED
    return STATUS_LABELS.get(status, str(status))


def format_timestamp(value) -> str:
    if value is None or pd.isna(value):
        return "—"
    try:
        return datetime.fromisoformat(str(value)).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return str(value)


def _reachable_label(status, status_code) -> str:
    if pd.isna(status):
        return "—"
    return "Yes" if pd.notna(status_code) else "No"


def compute_summary(latest: pd.DataFrame) -> dict:
    """Numbers for the metric cards, based on each website's latest check."""
    counts = {name: int((latest["status"] == name).sum()) for name in ("UP", "DOWN", "DEGRADED")}
    times = pd.to_numeric(latest["response_time_ms"], errors="coerce").dropna()
    return {
        "total": len(latest),
        "up": counts["UP"],
        "down": counts["DOWN"],
        "degraded": counts["DEGRADED"],
        "not_checked": int(latest["status"].isna().sum()),
        "avg_response_ms": float(times.mean()) if len(times) else None,
    }


def prepare_status_table(latest: pd.DataFrame) -> pd.DataFrame:
    """The 'one row per website' table."""
    if latest.empty:
        return pd.DataFrame(columns=STATUS_TABLE_COLUMNS)
    return pd.DataFrame(
        {
            "Website": latest["url"],
            "Status": latest["status"].map(status_label),
            "Response time (ms)": pd.to_numeric(latest["response_time_ms"], errors="coerce"),
            "HTTP status": pd.to_numeric(latest["status_code"], errors="coerce").astype("Int64"),
            "Reachable": [_reachable_label(s, c) for s, c in zip(latest["status"], latest["status_code"])],
            "Last checked (UTC)": latest["checked_at"].map(format_timestamp),
            "Details": latest["error_message"].fillna(""),
        }
    )


def prepare_history(history: pd.DataFrame) -> pd.DataFrame:
    """History with real datetimes and numbers, sorted oldest -> newest (ready for charts)."""
    df = history.copy()
    df["checked_at_dt"] = pd.to_datetime(df["checked_at"], utc=True, errors="coerce")
    df["response_time_ms"] = pd.to_numeric(df["response_time_ms"], errors="coerce")
    df["status_code"] = pd.to_numeric(df["status_code"], errors="coerce").astype("Int64")
    return df.sort_values("checked_at_dt").reset_index(drop=True)


def history_table(history: pd.DataFrame) -> pd.DataFrame:
    """Readable history table, newest first."""
    df = prepare_history(history).iloc[::-1]
    return pd.DataFrame(
        {
            "Time (UTC)": df["checked_at"].map(format_timestamp),
            "Status": df["status"].map(status_label),
            "Response time (ms)": df["response_time_ms"],
            "HTTP status": df["status_code"],
            "Error type": df["error_type"].fillna(""),
            "Message": df["error_message"].fillna(""),
        }
    ).reset_index(drop=True)

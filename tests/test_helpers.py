"""Tests for helpers.py (dashboard data preparation)."""
import pandas as pd

import helpers


def sample_latest():
    return pd.DataFrame(
        {
            "url": ["https://a.example", "https://b.example", "https://c.example", "https://d.example"],
            "status": ["UP", "DOWN", "DEGRADED", None],
            "response_time_ms": [100.0, None, 300.0, None],
            "status_code": [200, None, 404, None],
            "checked_at": ["2026-01-01T10:00:00+00:00", "2026-01-01T10:00:05+00:00", "2026-01-01T10:00:09+00:00", None],
            "error_type": [None, "timeout", "http_client_error", None],
            "error_message": [None, "Connection timeout after 10 seconds.", "HTTP 404", None],
        }
    )


def test_status_label_handles_all_cases():
    assert helpers.status_label("UP") == "🟢 UP"
    assert helpers.status_label("DOWN") == "🔴 DOWN"
    assert helpers.status_label("DEGRADED") == "🟡 DEGRADED"
    assert helpers.status_label(None) == helpers.NOT_CHECKED
    assert helpers.status_label(float("nan")) == helpers.NOT_CHECKED


def test_compute_summary():
    summary = helpers.compute_summary(sample_latest())
    assert (summary["total"], summary["up"], summary["down"], summary["degraded"], summary["not_checked"]) == (4, 1, 1, 1, 1)
    assert summary["avg_response_ms"] == 200.0


def test_compute_summary_with_no_websites():
    empty = pd.DataFrame(columns=["url", "status", "response_time_ms", "status_code", "checked_at", "error_type", "error_message"])
    summary = helpers.compute_summary(empty)
    assert summary["total"] == 0 and summary["avg_response_ms"] is None


def test_status_table():
    table = helpers.prepare_status_table(sample_latest())
    assert list(table.columns) == helpers.STATUS_TABLE_COLUMNS
    assert list(table["Reachable"]) == ["Yes", "No", "Yes", "—"]
    assert table.loc[0, "Last checked (UTC)"] == "2026-01-01 10:00:00"
    assert table.loc[3, "Last checked (UTC)"] == "—"


def test_status_table_when_empty():
    empty = sample_latest().iloc[0:0]
    assert helpers.prepare_status_table(empty).empty


def test_history_table_is_newest_first():
    history = pd.DataFrame(
        {
            "checked_at": ["2026-01-01T10:05:00+00:00", "2026-01-01T10:00:00+00:00"],
            "status": ["DOWN", "UP"],
            "response_time_ms": [None, 120.0],
            "status_code": [None, 200],
            "error_type": ["timeout", None],
            "error_message": ["Timeout", None],
        }
    )
    table = helpers.history_table(history)
    assert list(table["Status"]) == ["🔴 DOWN", "🟢 UP"]
    chart_data = helpers.prepare_history(history)
    assert chart_data["checked_at_dt"].is_monotonic_increasing

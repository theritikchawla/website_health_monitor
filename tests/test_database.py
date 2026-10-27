"""Tests for database.py using a temporary SQLite file."""
import pandas as pd
import pytest

import database
import monitor


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    database.init_db(path)
    return path


def make_result(url="https://example.com", status="UP", ms=120.5, code=200, error_type=None, message=None, at=None):
    return monitor.CheckResult(
        url=url, status=status, checked_at=at or monitor.utc_now_iso(),
        response_time_ms=ms, status_code=code, error_type=error_type, error_message=message,
    )


def test_save_and_retrieve_history(db_path):
    database.save_result(make_result(at="2026-01-01T10:00:00+00:00"), db_path)
    database.save_result(
        make_result(status="DOWN", ms=None, code=None, error_type="timeout",
                    message="Connection timeout after 10 seconds.", at="2026-01-01T10:05:00+00:00"),
        db_path,
    )
    history = database.get_history("https://example.com", db_path=db_path)
    assert len(history) == 2
    assert history.iloc[0]["status"] == "DOWN"            # newest first
    assert history.iloc[0]["error_message"] == "Connection timeout after 10 seconds."
    assert pd.isna(history.iloc[0]["status_code"])
    assert history.iloc[1]["response_time_ms"] == 120.5
    assert history.iloc[1]["status_code"] == 200


def test_history_is_separate_per_website(db_path):
    database.save_result(make_result(url="https://a.example"), db_path)
    database.save_result(make_result(url="https://b.example"), db_path)
    assert len(database.get_history("https://a.example", db_path=db_path)) == 1


def test_get_failures_returns_only_problems(db_path):
    database.save_result(make_result(status="UP"), db_path)
    database.save_result(make_result(status="DEGRADED", code=404, error_type="http_client_error", message="404"), db_path)
    database.save_result(make_result(status="DOWN", ms=None, code=None, error_type="timeout", message="t"), db_path)
    failures = database.get_failures("https://example.com", db_path=db_path)
    assert sorted(failures["status"]) == ["DEGRADED", "DOWN"]


def test_add_list_and_duplicate_website(db_path):
    assert database.add_website("https://a.example", db_path) is True
    assert database.add_website("https://a.example", db_path) is False
    assert database.list_websites(db_path) == ["https://a.example"]


def test_website_limit_is_enforced(db_path):
    database.add_website("https://a.example", db_path, max_websites=2)
    database.add_website("https://b.example", db_path, max_websites=2)
    with pytest.raises(database.WebsiteLimitError):
        database.add_website("https://c.example", db_path, max_websites=2)
    assert len(database.list_websites(db_path)) == 2


def test_remove_website_also_removes_history(db_path):
    database.add_website("https://a.example", db_path)
    database.save_result(make_result(url="https://a.example"), db_path)
    assert database.remove_website("https://a.example", db_path) is True
    assert database.list_websites(db_path) == []
    assert database.get_history("https://a.example", db_path=db_path).empty
    assert database.remove_website("https://a.example", db_path) is False


def test_latest_results_has_one_row_per_site_including_unchecked(db_path):
    database.add_website("https://a.example", db_path)
    database.add_website("https://b.example", db_path)
    database.save_result(make_result(url="https://a.example", status="DOWN", ms=None, code=None), db_path)
    database.save_result(make_result(url="https://a.example", status="UP"), db_path)
    latest = database.get_latest_results(db_path).set_index("url")
    assert len(latest) == 2
    assert latest.loc["https://a.example", "status"] == "UP"   # the newest check wins
    assert pd.isna(latest.loc["https://b.example", "status"])  # never checked


def test_stats(db_path):
    database.save_result(make_result(status="UP", ms=100.0), db_path)
    database.save_result(make_result(status="DEGRADED", ms=300.0, code=404), db_path)
    database.save_result(make_result(status="DOWN", ms=None, code=None), db_path)
    stats = database.get_stats("https://example.com", db_path)
    assert stats["total_checks"] == 3
    assert stats["uptime_pct"] == 66.7
    assert stats["problem_checks"] == 2
    assert stats["avg_response_ms"] == 200.0
    assert database.get_stats("https://nothing.example", db_path)["total_checks"] == 0


def test_old_history_is_deleted(db_path):
    database.save_result(make_result(at="2020-01-01T00:00:00+00:00"), db_path)
    database.save_result(make_result(), db_path)
    assert database.delete_old_checks(30, db_path) == 1
    assert len(database.get_history("https://example.com", db_path=db_path)) == 1

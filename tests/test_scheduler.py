"""Tests for scheduler.py (the real checks are replaced by a fake)."""
import threading
import time
from unittest.mock import patch

import pytest

import scheduler


def test_run_once_skips_when_a_cycle_is_already_running():
    sched = scheduler.MonitorScheduler()
    with patch("scheduler.run_check_cycle", return_value=3) as cycle:
        assert sched.run_once() == 3
        sched._run_lock.acquire()  # pretend a cycle is in progress
        try:
            assert sched.run_once() is None
        finally:
            sched._run_lock.release()
    assert cycle.call_count == 1


def test_interval_below_minimum_is_rejected():
    with pytest.raises(ValueError):
        scheduler.MonitorScheduler().start(5)


def test_start_runs_checks_repeatedly_and_stop_works():
    calls = []
    sched = scheduler.MonitorScheduler(min_interval=0.01)
    with patch("scheduler.run_check_cycle", side_effect=lambda db_path=None: calls.append(1) or 1):
        sched.start(0.05)
        assert sched.is_running
        time.sleep(0.4)
        sched.stop()
        assert not sched.is_running
        time.sleep(0.2)
        count_after_stop = len(calls)
        time.sleep(0.2)
    assert count_after_stop >= 2
    assert len(calls) == count_after_stop  # nothing runs after stop


def test_cycles_never_overlap():
    running, overlaps = [0], []
    guard = threading.Lock()

    def slow_cycle(db_path=None):
        with guard:
            running[0] += 1
            if running[0] > 1:
                overlaps.append(1)
        time.sleep(0.1)
        with guard:
            running[0] -= 1
        return 1

    sched = scheduler.MonitorScheduler()
    with patch("scheduler.run_check_cycle", side_effect=slow_cycle):
        threads = [threading.Thread(target=sched.run_once) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert overlaps == []

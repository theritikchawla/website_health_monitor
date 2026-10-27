"""
scheduler.py - runs checks automatically in a background thread.

Important: this is NOT a system service. Checks only run while the Python
process (the Streamlit app) is running. Close the app and they stop.

Overlap protection: a lock guarantees only one check cycle runs at a time. If a
manual check or a scheduled check is already running, the new one is skipped.
"""
from __future__ import annotations

import logging
import threading

import config
import database
import monitor

logger = logging.getLogger(__name__)


def run_check_cycle(db_path=None) -> int:
    """Check every monitored website once and save the results. Returns how many were checked."""
    urls = database.list_websites(db_path)[: config.MAX_WEBSITES]
    if not urls:
        return 0
    results = monitor.check_websites(urls)
    for result in results:
        try:
            database.save_result(result, db_path)
        except Exception:  # one bad save must not lose the other results
            logger.exception("Could not save the result for %s", result.url)
    return len(results)


class MonitorScheduler:
    def __init__(self, db_path=None, min_interval: float | None = None):
        self.db_path = db_path
        self.min_interval = config.MIN_INTERVAL_SECONDS if min_interval is None else min_interval
        self.interval_seconds = config.DEFAULT_INTERVAL_SECONDS
        self.last_run_at: str | None = None
        self._run_lock = threading.Lock()    # held while a cycle is running
        self._state_lock = threading.Lock()  # protects start/stop bookkeeping
        self._stop_event: threading.Event | None = None

    @property
    def is_running(self) -> bool:
        with self._state_lock:
            return self._stop_event is not None and not self._stop_event.is_set()

    def run_once(self) -> int | None:
        """Run one cycle now. Returns None (and does nothing) if a cycle is already running."""
        if not self._run_lock.acquire(blocking=False):
            return None
        try:
            count = run_check_cycle(self.db_path)
            self.last_run_at = monitor.utc_now_iso()
            return count
        finally:
            self._run_lock.release()

    def start(self, interval_seconds: float) -> None:
        """Start automatic checks, or change the interval if already running."""
        if interval_seconds < self.min_interval:
            raise ValueError(f"The interval must be at least {self.min_interval} seconds.")
        with self._state_lock:
            self.interval_seconds = interval_seconds
            if self._stop_event is not None and not self._stop_event.is_set():
                return  # already running; the new interval applies after the current wait
            self._stop_event = threading.Event()
            thread = threading.Thread(
                target=self._loop, args=(self._stop_event,), name="monitor-scheduler", daemon=True
            )
            thread.start()

    def stop(self) -> None:
        with self._state_lock:
            if self._stop_event is not None:
                self._stop_event.set()

    def _loop(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            try:
                self.run_once()
            except Exception:
                logger.exception("A scheduled check failed")
            stop_event.wait(self.interval_seconds)

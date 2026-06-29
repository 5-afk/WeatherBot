"""Scheduler that watches NWS/GFS model cycle release times."""

from __future__ import annotations

import logging
import os
import time
import threading
from collections.abc import Callable
from datetime import datetime
import zoneinfo

import schedule


CYCLE_LEVEL = 25
ET = zoneinfo.ZoneInfo("America/New_York")


class NWSWatcher:
    """Run the trading pipeline at model-cycle release times and backups."""

    CYCLE_TIMES_UTC = {
        "00Z": "03:30",
        "06Z": "09:30",
        "12Z": "15:30",
        "18Z": "21:30",
    }

    SCAN_SCHEDULE = {
        0: 60,
        5: 15,
        6: 5,
        14: 15,
        18: 30,
        23: 60,
    }

    def __init__(self, pipeline: Callable[[], None]) -> None:
        """Store the pipeline function that should run on each trigger."""
        self.pipeline = pipeline
        self.backup_minutes = int(os.getenv("SCAN_INTERVAL_MINUTES", "30"))
        self._current_interval = self.backup_minutes
        self._schedule_lock = threading.Lock()

    def _get_current_interval(self) -> int:
        """Return scan interval in minutes based on current ET hour."""
        et_hour = datetime.now(ET).hour
        interval = 60
        for start_hour, minutes in sorted(self.SCAN_SCHEDULE.items()):
            if et_hour >= start_hour:
                interval = minutes
        return interval

    def register_jobs(self) -> None:
        """Register NWS cycle jobs. Backup scans are managed dynamically."""
        for cycle_name, release_time in self.CYCLE_TIMES_UTC.items():
            schedule.every().day.at(release_time, "UTC").do(
                self._run_cycle, cycle_name)
        logging.getLogger().log(CYCLE_LEVEL,
            "NWS watcher armed. Cycles: %s UTC. Dynamic scan schedule active.",
            ", ".join(self.CYCLE_TIMES_UTC.values()))
        self._log_next_interval()

    def run_forever(self) -> None:
        """Keep scheduler alive with dynamic interval adjustment."""
        logging.getLogger().log(CYCLE_LEVEL, "NWS watcher running forever...")
        last_scan_time = 0.0
        while True:
            try:
                schedule.run_pending()
                now = time.time()
                current_interval = self._get_current_interval()
                if current_interval != self._current_interval:
                    self._current_interval = current_interval
                    et_time = datetime.now(ET).strftime("%I:%M%p ET")
                    logging.getLogger().log(
                        CYCLE_LEVEL,
                        "Scan interval changed to %d min at %s",
                        current_interval,
                        et_time,
                    )
                if now - last_scan_time >= current_interval * 60:
                    last_scan_time = now
                    self._run_backup(current_interval)
                time.sleep(1)
            except KeyboardInterrupt:
                logging.info("NWS watcher stopped by user.")
                break
            except Exception as exc:
                logging.error("NWS watcher error (restarting in 30s): %s", exc)
                time.sleep(30)

    def _run_cycle(self, cycle_name: str) -> None:
        """Log the detected cycle, run pipeline, then burst scan for 30 mins."""
        et_time = datetime.now(ET).strftime("%I:%M%p ET")
        logging.getLogger().log(
            CYCLE_LEVEL,
            "[CYCLE] NWS %s detected at %s -- running pipeline",
            cycle_name,
            et_time,
        )
        self.pipeline()

        for i in range(1, 7):
            timer = threading.Timer(i * 300, self._run_burst, args=[cycle_name, i])
            timer.daemon = True
            timer.start()

    def _run_burst(self, cycle_name: str, burst_num: int) -> None:
        """Run a burst scan after a cycle drop."""
        logging.getLogger().log(
            CYCLE_LEVEL,
            "[CYCLE] Burst %d/6 after %s cycle",
            burst_num,
            cycle_name,
        )
        self.pipeline()

    def _run_backup(self, interval: int) -> None:
        """Run a backup scan and log the current window."""
        et_time = datetime.now(ET).strftime("%I:%M%p ET")
        window = self._get_window_name()
        logging.getLogger().log(
            CYCLE_LEVEL,
            "[CYCLE] Backup scan [%s] | Window: %s | Interval: %d min",
            et_time,
            window,
            interval,
        )
        self.pipeline()

    def _get_window_name(self) -> str:
        """Return human-readable name for current trading window."""
        et_hour = datetime.now(ET).hour
        if 6 <= et_hour < 14:
            return "PRIME (5min scans)"
        if 5 <= et_hour < 6:
            return "Pre-market (15min)"
        if 14 <= et_hour < 18:
            return "Afternoon (15min)"
        if 18 <= et_hour < 23:
            return "Evening (30min)"
        return "Overnight (60min)"

    def _log_next_interval(self) -> None:
        """Log the current scan interval on startup."""
        interval = self._get_current_interval()
        window = self._get_window_name()
        et_time = datetime.now(ET).strftime("%I:%M%p ET")
        logging.getLogger().log(
            CYCLE_LEVEL,
            "Current window: %s | Interval: %d min | ET: %s",
            window,
            interval,
            et_time,
        )

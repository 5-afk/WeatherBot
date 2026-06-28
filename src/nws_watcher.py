"""Scheduler that watches NWS/GFS model cycle release times."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable

import schedule


CYCLE_LEVEL = 25


class NWSWatcher:
    """Run the trading pipeline at model-cycle release times and backups."""

    CYCLE_TIMES_UTC = {
        "00Z": "03:30",
        "06Z": "09:30",
        "12Z": "15:30",
        "18Z": "21:30",
    }

    def __init__(self, pipeline: Callable[[], None]) -> None:
        """Store the pipeline function that should run on each trigger."""
        self.pipeline = pipeline
        self.backup_minutes = int(os.getenv("SCAN_INTERVAL_MINUTES", "30"))

    def register_jobs(self) -> None:
        """Register NWS cycle jobs and continuous backup scans."""
        for cycle_name, release_time in self.CYCLE_TIMES_UTC.items():
            schedule.every().day.at(release_time, "UTC").do(
                self._run_cycle, cycle_name)

        # Backup scan every 30 minutes - runs continuously forever
        schedule.every(self.backup_minutes).minutes.do(self._run_backup)

        logging.getLogger().log(CYCLE_LEVEL,
            "NWS watcher armed. Cycles: %s UTC. Backup every %s min.",
            ", ".join(self.CYCLE_TIMES_UTC.values()), self.backup_minutes)
        self._run_backup()

    def run_forever(self) -> None:
        """Keep the scheduler alive forever, restarting on any error."""
        logging.getLogger().log(CYCLE_LEVEL, "NWS watcher running forever...")
        while True:
            try:
                schedule.run_pending()
                time.sleep(1)
            except KeyboardInterrupt:
                logging.info("NWS watcher stopped by user.")
                break
            except Exception as exc:
                logging.error("NWS watcher error (restarting in 30s): %s", exc)
                time.sleep(30)

    def _run_cycle(self, cycle_name: str) -> None:
        """Log the detected cycle, run pipeline, then burst scan for 30 mins."""
        logging.getLogger().log(CYCLE_LEVEL, "[CYCLE] NWS %s cycle detected -- running full pipeline", cycle_name)
        self.pipeline()

        # Schedule 5-minute burst scans for the next 30 minutes
        # This catches early market price movements after fresh data drops
        import threading
        for i in range(1, 7):  # 6 scans x 5 minutes = 30 minutes of burst scanning
            timer = threading.Timer(i * 300, self._run_burst, args=[cycle_name, i])
            timer.daemon = True
            timer.start()

    def _run_burst(self, cycle_name: str, burst_num: int) -> None:
        """Run a burst scan after a cycle drop."""
        logging.getLogger().log(CYCLE_LEVEL, "[CYCLE] Burst scan %d/6 after %s cycle", burst_num, cycle_name)
        self.pipeline()

    def _run_backup(self) -> None:
        """Run a backup scan so missed cycle events do not leave the bot idle."""
        logging.getLogger().log(CYCLE_LEVEL, "[CYCLE] Backup 30-minute scan -- running full pipeline")
        self.pipeline()

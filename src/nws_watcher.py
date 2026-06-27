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
        """Register exact UTC cycle jobs and the 30-minute backup job."""
        for cycle_name, release_time in self.CYCLE_TIMES_UTC.items():
            schedule.every().day.at(release_time, "UTC").do(self._run_cycle, cycle_name)
        schedule.every(self.backup_minutes).minutes.do(self._run_backup)
        logging.getLogger().log(CYCLE_LEVEL, "NWS watcher armed for cycles: %s UTC.", ", ".join(self.CYCLE_TIMES_UTC.values()))
        logging.getLogger().log(CYCLE_LEVEL, "Backup full scan armed every %s minutes.", self.backup_minutes)

    def run_forever(self) -> None:
        """Keep the scheduler alive until the process is stopped."""
        while True:
            schedule.run_pending()
            time.sleep(1)

    def _run_cycle(self, cycle_name: str) -> None:
        """Log the detected cycle and run the full pipeline immediately."""
        logging.getLogger().log(CYCLE_LEVEL, "[CYCLE] NWS %s cycle detected -- running full pipeline", cycle_name)
        self.pipeline()

    def _run_backup(self) -> None:
        """Run a backup scan so missed cycle events do not leave the bot idle."""
        logging.getLogger().log(CYCLE_LEVEL, "[CYCLE] Backup 30-minute scan -- running full pipeline")
        self.pipeline()

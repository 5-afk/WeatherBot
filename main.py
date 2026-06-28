"""Windows-friendly entry point for the Kalshi weather trading bot.

The bot starts in dry-run mode by default. It runs immediately on startup when
RUN_ON_START=true, then watches the NWS/GFS release windows at 03:30, 09:30,
15:30, and 21:30 UTC. A 30-minute backup scan also runs in case a cycle job is
missed while the computer is asleep or the process is restarting.
"""

from __future__ import annotations

import logging
import time

import schedule
from dotenv import load_dotenv

from src.discord_commander import DiscordCommander
from src.nws_watcher import NWSWatcher
from src.trader import Trader, configure_logging


def main() -> None:
    """Load config, start bot, restart automatically on crash."""
    load_dotenv()
    configure_logging()
    logging.info("Starting KalshiBot...")

    while True:
        try:
            schedule.clear()
            trader = Trader()
            watcher = NWSWatcher(trader.run_full_pipeline)
            watcher.register_jobs()
            commander = DiscordCommander(trader.run_full_pipeline, trader)
            commander.start_in_background()
            logging.info("Discord commander started — listening for commands.")
            logging.info("Kalshi weather bot started. DRY_RUN=%s", trader.dry_run)
            watcher.run_forever()
        except KeyboardInterrupt:
            logging.info("KalshiBot stopped by user.")
            break
        except Exception as exc:
            logging.exception("KalshiBot crashed; restarting in 60 seconds: %s", exc)
            time.sleep(60)


if __name__ == "__main__":
    main()

"""Windows-friendly entry point for the Kalshi weather trading bot.

The bot starts in dry-run mode by default. It runs immediately on startup when
RUN_ON_START=true, then watches the NWS/GFS release windows at 03:30, 09:30,
15:30, and 21:30 UTC. A 30-minute backup scan also runs in case a cycle job is
missed while the computer is asleep or the process is restarting.
"""

from __future__ import annotations

import logging

from dotenv import load_dotenv

from src.discord_commander import DiscordCommander
from src.nws_watcher import NWSWatcher
from src.trader import CYCLE_LEVEL, Trader, configure_logging, env_bool


def main() -> None:
    """Load configuration, create the trader, and start the NWS watcher."""
    load_dotenv()
    configure_logging()

    trader = Trader()
    watcher = NWSWatcher(trader.run_full_pipeline)
    watcher.register_jobs()
    commander = DiscordCommander(trader.run_full_pipeline, trader)
    commander.start_in_background()
    logging.info("Discord commander started — listening for commands.")

    logging.info("Kalshi weather bot started. DRY_RUN=%s", trader.dry_run)
    if env_bool("RUN_ON_START", True):
        logging.getLogger().log(CYCLE_LEVEL, "[CYCLE] Startup scan requested -- running full pipeline")
        trader.run_full_pipeline()

    watcher.run_forever()


if __name__ == "__main__":
    main()

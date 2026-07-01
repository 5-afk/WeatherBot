"""File-based control signals between discord_launcher.py and main.py."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
SCAN_TRIGGER = DATA_DIR / "scan.trigger"
PAUSED_FLAG = DATA_DIR / "paused.flag"


def request_scan() -> None:
    """Ask the running main.py process to run one pipeline scan."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SCAN_TRIGGER.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")


def consume_scan_trigger() -> bool:
    """Return True and clear the trigger when a manual scan was requested."""
    if not SCAN_TRIGGER.exists():
        return False
    SCAN_TRIGGER.unlink(missing_ok=True)
    return True


def set_paused(paused: bool) -> None:
    """Persist pause state for the main.py trader process."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if paused:
        PAUSED_FLAG.write_text("1", encoding="utf-8")
    else:
        PAUSED_FLAG.unlink(missing_ok=True)


def is_paused() -> bool:
    """Return True when scanning is paused via the control file."""
    return PAUSED_FLAG.exists()

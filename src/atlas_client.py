"""ATLAS agent registration and heartbeat — safe no-op when dashboard is down."""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PORT = int(os.getenv("ATLAS_PORT", "5000"))
BASE_URL = os.getenv("ATLAS_URL", f"http://127.0.0.1:{DEFAULT_PORT}")
DASHBOARD_SECRET = os.getenv("DASHBOARD_SECRET", "")


def _api_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if DASHBOARD_SECRET:
        headers["X-Atlas-Secret"] = DASHBOARD_SECRET
    return headers

_heartbeat_thread: threading.Thread | None = None
_heartbeat_stop = threading.Event()
_state_provider: Callable[[], dict[str, Any]] | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def register_agent(manifest: dict[str, Any]) -> bool:
    """Register this bot with ATLAS. Returns True on success."""
    try:
        resp = requests.post(
            f"{BASE_URL}/api/agents/register",
            json=manifest,
            headers=_api_headers(),
            timeout=3,
        )
        return resp.status_code == 200 and resp.json().get("ok", False)
    except Exception as exc:
        logging.debug("ATLAS register skipped: %s", exc)
        return False


def send_heartbeat(payload: dict[str, Any]) -> bool:
    """Send heartbeat to ATLAS. Returns True on success."""
    try:
        resp = requests.post(
            f"{BASE_URL}/api/agents/heartbeat",
            json=payload,
            headers=_api_headers(),
            timeout=3,
        )
        return resp.status_code == 200
    except Exception as exc:
        logging.debug("ATLAS heartbeat skipped: %s", exc)
        return False


def whetherbot_manifest(*, pid: int | None = None) -> dict[str, Any]:
    """Build WhetherBot registration manifest."""
    dry_run = os.getenv("DRY_RUN", "true").strip().lower() in {"1", "true", "yes"}
    max_claude = int(os.getenv("MAX_CLAUDE_CALLS_PER_DAY", "5"))
    return {
        "id": "whetherbot",
        "name": "WhetherBot",
        "domain": "weather",
        "status": "running",
        "mode": "DRY_RUN" if dry_run else "LIVE",
        "pid": pid or os.getpid(),
        "started_at": _now_iso(),
        "scan_window": _scan_window_label(),
        "endpoints": {"control": "internal"},
        "capabilities": ["scan", "pause", "sell", "improvemodel"],
        "claude_calls": {"used": 0, "limit": max_claude},
    }


def _scan_window_label() -> str:
    import zoneinfo

    et = zoneinfo.ZoneInfo("America/New_York")
    hour = datetime.now(et).hour
    if 6 <= hour < 14:
        return "06:00-14:00 ET (PRIME)"
    if 14 <= hour < 18:
        return "14:00-18:00 ET"
    if 18 <= hour < 23:
        return "18:00-23:00 ET"
    return "overnight ET"


def start_heartbeat_loop(state_provider: Callable[[], dict[str, Any]]) -> None:
    """Start background heartbeat thread (60s interval)."""
    global _heartbeat_thread, _state_provider
    _state_provider = state_provider
    if _heartbeat_thread and _heartbeat_thread.is_alive():
        return
    _heartbeat_stop.clear()

    def _loop() -> None:
        while not _heartbeat_stop.wait(60):
            if _state_provider is None:
                continue
            try:
                state = _state_provider()
                send_heartbeat({
                    "id": "whetherbot",
                    "status": state.get("status", "running"),
                    "mode": state.get("mode", "DRY_RUN"),
                    "next_scan": state.get("next_scan"),
                    "last_scan": state.get("last_scan"),
                    "claude_calls": state.get("claude_calls"),
                })
            except Exception as exc:
                logging.debug("ATLAS heartbeat error: %s", exc)

    _heartbeat_thread = threading.Thread(target=_loop, daemon=True, name="atlas-heartbeat")
    _heartbeat_thread.start()


def stop_heartbeat_loop() -> None:
    """Stop the background heartbeat thread."""
    _heartbeat_stop.set()

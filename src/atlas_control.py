"""ATLAS control bridge — connects Flask API to discord_launcher bot process."""

from __future__ import annotations

import logging
from typing import Any, Callable

_launcher: Any | None = None
_control_handlers: dict[str, Callable[..., dict[str, Any]]] = {}


def register_launcher(launcher: Any) -> None:
    """Store the BotLauncher instance for process control from the API."""
    global _launcher
    _launcher = launcher


def get_launcher() -> Any | None:
    """Return the registered launcher, if any."""
    return _launcher


def register_control_handler(agent_id: str, handler: Callable[..., dict[str, Any]]) -> None:
    """Register a bot-specific control handler (for future multi-bot support)."""
    _control_handlers[agent_id] = handler


def execute_control(bot: str, action: str) -> dict[str, Any]:
    """Route control actions to launcher or registered handlers."""
    from src.bot_control import request_scan, set_paused

    launcher = _launcher
    dry_run = True
    try:
        import os

        dry_run = os.getenv("DRY_RUN", "true").strip().lower() in {"1", "true", "yes"}
    except Exception:
        pass

    if bot != "all" and bot in _control_handlers:
        return _control_handlers[bot](action)

    if action == "scan":
        if launcher and not launcher._is_running():
            raise ControlError("Bot is not running", 409)
        request_scan()
        return {"status": "running", "action": "scan"}

    if action == "pause":
        set_paused(True)
        return {"status": "paused", "action": "pause"}

    if action == "resume":
        if launcher and not launcher._is_running():
            raise ControlError("Bot is not running — use start first", 409)
        set_paused(False)
        request_scan()
        return {"status": "running", "action": "resume"}

    if action == "start":
        if launcher is None:
            raise ControlError("Launcher not available", 503)
        if launcher._is_running():
            raise ControlError("Bot already running", 409)
        launcher._start_process()
        return {"status": "running", "action": "start", "pid": launcher.process.pid if launcher.process else None}

    if action == "stop":
        if launcher is None:
            raise ControlError("Launcher not available", 503)
        if not launcher._is_running():
            raise ControlError("Bot is not running", 409)
        proc = launcher.process
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
        launcher.process = None
        launcher.start_time = None
        launcher._close_process_log()
        return {"status": "stopped", "action": "stop"}

    if action == "restart":
        if launcher is None:
            raise ControlError("Launcher not available", 503)
        if launcher._is_running():
            proc = launcher.process
            if proc:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except Exception:
                    proc.kill()
            launcher.process = None
            launcher.start_time = None
            launcher._close_process_log()
        launcher._start_process()
        return {"status": "running", "action": "restart", "pid": launcher.process.pid if launcher.process else None}

    if action == "killswitch":
        set_paused(True)
        if bot == "all" or bot == "whetherbot":
            if launcher and launcher._is_running():
                proc = launcher.process
                if proc:
                    proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except Exception:
                        proc.kill()
                launcher.process = None
                launcher.start_time = None
                launcher._close_process_log()
        logging.warning("ATLAS killswitch activated (dry_run=%s)", dry_run)
        return {"status": "stopped", "action": "killswitch", "killswitch": True}

    raise ControlError(f"Unknown action: {action}", 400)


class ControlError(Exception):
    """Raised when a control action is invalid for current state."""

    def __init__(self, message: str, code: int = 409):
        super().__init__(message)
        self.code = code

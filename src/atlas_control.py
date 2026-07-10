"""ATLAS control bridge — connects Flask API to discord_launcher bot process."""

from __future__ import annotations

import logging
import os
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


def _use_systemd() -> bool:
    return os.getenv("ATLAS_USE_SYSTEMD", "0").strip() == "1"


def execute_control(bot: str, action: str) -> dict[str, Any]:
    """Route control actions to launcher, systemd, or BotController fallback."""
    from src.bot_control import request_scan, set_paused
    from src.bot_controller import BotController, systemd_control

    launcher = _launcher
    dry_run = True
    try:
        dry_run = os.getenv("DRY_RUN", "true").strip().lower() in {"1", "true", "yes"}
    except Exception:
        pass

    if bot != "all" and bot in _control_handlers:
        return _control_handlers[bot](action)

    if action == "scan":
        ctrl = BotController()
        if launcher and not launcher._is_running() and not ctrl.is_running():
            raise ControlError("Bot is not running", 409)
        request_scan()
        return {"status": BotController().status(), "action": "scan"}

    if action == "pause":
        set_paused(True)
        return {"status": "paused", "action": "pause"}

    if action == "resume":
        ctrl = BotController()
        running = (launcher and launcher._is_running()) or ctrl.is_running()
        if not running:
            raise ControlError("Bot is not running — use start first", 409)
        set_paused(False)
        request_scan()
        return {"status": "running", "action": "resume"}

    if action in ("start", "stop", "restart"):
        if _use_systemd():
            return systemd_control(action)
        if launcher is not None:
            return _launcher_control(launcher, action)
        return _bot_controller_control(action)

    if action == "killswitch":
        set_paused(True)
        if bot == "all" or bot == "whetherbot":
            if _use_systemd():
                try:
                    systemd_control("stop")
                except Exception as exc:
                    logging.warning("systemd stop during killswitch: %s", exc)
            elif launcher and launcher._is_running():
                _launcher_stop(launcher)
            else:
                BotController().stop()
        logging.warning("ATLAS killswitch activated (dry_run=%s)", dry_run)
        return {"status": "stopped", "action": "killswitch", "killswitch": True}

    raise ControlError(f"Unknown action: {action}", 400)


def _launcher_control(launcher: Any, action: str) -> dict[str, Any]:
    """Control via discord_launcher subprocess handle."""
    if action == "start":
        if launcher._is_running():
            raise ControlError("Bot already running", 409)
        launcher._start_process()
        pid = launcher.process.pid if launcher.process else None
        return {"status": "running", "action": "start", "pid": pid, "via": "launcher"}

    if action == "stop":
        if not launcher._is_running():
            raise ControlError("Bot is not running", 409)
        _launcher_stop(launcher)
        return {"status": "stopped", "action": "stop", "via": "launcher"}

    if action == "restart":
        if launcher._is_running():
            _launcher_stop(launcher)
        launcher._start_process()
        pid = launcher.process.pid if launcher.process else None
        return {"status": "running", "action": "restart", "pid": pid, "via": "launcher"}

    raise ControlError(f"Unknown launcher action: {action}", 400)


def _launcher_stop(launcher: Any) -> None:
    proc = launcher.process
    if proc:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
    launcher.process = None
    launcher.start_time = None
    if hasattr(launcher, "_close_process_log"):
        launcher._close_process_log()


def _bot_controller_control(action: str) -> dict[str, Any]:
    """Fallback when discord_launcher is not registered."""
    from src.bot_controller import BotController

    ctrl = BotController()
    if action == "start":
        if ctrl.is_running():
            raise ControlError("Bot already running", 409)
        ctrl.start()
        return {"status": ctrl.status(), "action": "start", "pid": ctrl.pid(), "via": "bot_controller"}
    if action == "stop":
        if not ctrl.is_running():
            raise ControlError("Bot is not running", 409)
        ctrl.stop()
        return {"status": "stopped", "action": "stop", "via": "bot_controller"}
    if action == "restart":
        ctrl.restart()
        return {"status": ctrl.status(), "action": "restart", "pid": ctrl.pid(), "via": "bot_controller"}
    raise ControlError(f"Unknown action: {action}", 400)


class ControlError(Exception):
    """Raised when a control action is invalid for current state."""

    def __init__(self, message: str, code: int = 409):
        super().__init__(message)
        self.code = code

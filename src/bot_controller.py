"""Standalone main.py process control for ATLAS (fallback when discord_launcher absent)."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
MAIN_PID_FILE = DATA_DIR / "main.pid"
HEARTBEAT_FILE = DATA_DIR / "heartbeat.ts"
MAIN_SCRIPT = PROJECT_ROOT / "main.py"


def _venv_python() -> str:
    """Return project venv Python if present, else current interpreter."""
    for candidate in (
        PROJECT_ROOT / "venv" / "bin" / "python",
        PROJECT_ROOT / "venv" / "Scripts" / "python.exe",
    ):
        if candidate.exists():
            return str(candidate)
    return sys.executable


def _pid_exists(pid: int) -> bool:
    try:
        import psutil

        return psutil.pid_exists(pid)
    except ImportError:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


class BotController:
    """Manage main.py subprocess via PID file and heartbeat staleness."""

    def start(self) -> bool:
        """Start main.py if not already running. Returns True when a new process was started."""
        if self.is_running():
            return False
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        popen_kwargs: dict = {"cwd": PROJECT_ROOT}
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True
        proc = subprocess.Popen(
            [_venv_python(), str(MAIN_SCRIPT)],
            **popen_kwargs,
        )
        MAIN_PID_FILE.write_text(str(proc.pid), encoding="utf-8")
        logging.info("[BotController] Started main.py pid=%s", proc.pid)
        return True

    def stop(self) -> bool:
        """Stop main.py and clear PID file."""
        pid = self._read_pid()
        if pid and _pid_exists(pid):
            try:
                if sys.platform == "win32":
                    import psutil

                    p = psutil.Process(pid)
                    p.terminate()
                    try:
                        p.wait(timeout=10)
                    except psutil.TimeoutExpired:
                        p.kill()
                else:
                    os.killpg(os.getpgid(pid), signal.SIGTERM)
                    time.sleep(0.5)
                    if _pid_exists(pid):
                        os.killpg(os.getpgid(pid), signal.SIGKILL)
            except Exception as exc:
                logging.warning("[BotController] stop failed for pid %s: %s", pid, exc)
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
        MAIN_PID_FILE.unlink(missing_ok=True)
        return True

    def restart(self) -> bool:
        """Stop then start main.py."""
        self.stop()
        return self.start()

    def is_running(self) -> bool:
        """Return True if main.pid points to a live process."""
        pid = self._read_pid()
        return bool(pid and _pid_exists(pid))

    def status(self) -> str:
        """Return stopped | running | paused | stale."""
        from src.bot_control import is_paused

        if not self.is_running():
            return "stopped"
        if is_paused():
            return "paused"
        if self._is_stale():
            return "stale"
        return "running"

    def pid(self) -> int | None:
        """Return tracked main.py PID if running."""
        if not self.is_running():
            return None
        return self._read_pid()

    def _read_pid(self) -> int | None:
        try:
            return int(MAIN_PID_FILE.read_text(encoding="utf-8").strip())
        except (FileNotFoundError, ValueError):
            return None

    def _is_stale(self) -> bool:
        if not HEARTBEAT_FILE.exists():
            return True
        try:
            raw = HEARTBEAT_FILE.read_text(encoding="utf-8").strip()
            ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            interval_min = int(os.getenv("SCAN_INTERVAL_MINUTES", "30"))
            stale_seconds = interval_min * 2 * 60
            return (datetime.now(timezone.utc) - ts).total_seconds() > stale_seconds
        except Exception:
            return True


def systemd_control(action: str) -> dict:
    """Shell out to systemd user service when ATLAS_USE_SYSTEMD=1."""
    service = os.getenv("ATLAS_SYSTEMD_SERVICE", "kalshibot.service")
    cmd_map = {
        "start": ["systemctl", "--user", "start", service],
        "stop": ["systemctl", "--user", "stop", service],
        "restart": ["systemctl", "--user", "restart", service],
    }
    cmd = cmd_map.get(action)
    if not cmd:
        raise ValueError(f"Unknown systemd action: {action}")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"systemctl failed ({result.returncode})")
    ctrl = BotController()
    return {"status": ctrl.status(), "action": action, "via": "systemd"}

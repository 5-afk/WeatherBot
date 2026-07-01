"""Standalone Discord launcher for controlling the Kalshi weather bot process."""

from __future__ import annotations

import os
import sys
import atexit
import subprocess
import logging
import asyncio
import sqlite3
from datetime import datetime
from pathlib import Path
import zoneinfo

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))
from src.kalshi_client import KalshiClient


load_dotenv(dotenv_path=PROJECT_ROOT / ".env")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

PID_FILE = PROJECT_ROOT / "bot.pid"
ET = zoneinfo.ZoneInfo("America/New_York")


def check_single_instance():
    """Prevent multiple launcher instances from running at the same time."""
    if PID_FILE.exists():
        try:
            old_pid = int(PID_FILE.read_text(encoding="utf-8").strip())
        except ValueError:
            old_pid = None
        if old_pid:
            try:
                os.kill(old_pid, 0)
                print(f"Bot already running (PID {old_pid}). Exiting.")
                sys.exit(1)
            except OSError:
                pass
    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")


atexit.register(lambda: PID_FILE.unlink(missing_ok=True))


class BotLauncher:
    """Lightweight Discord bot that starts and stops main.py as a subprocess."""

    def __init__(self):
        """Create Discord command handlers and initialize process state."""
        self.process = None
        self.process_log_file = None
        self.start_time = None
        self.channel_id = os.getenv("DISCORD_CHANNEL_ID", "").strip()
        intents = discord.Intents.default()
        intents.message_content = True
        self.bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)
        self._register_commands()
        self._register_tasks()

    def _register_commands(self):
        """Register all launcher commands."""
        launcher = self

        @self.bot.check
        async def channel_only(ctx):
            """Ignore commands outside the configured Discord channel."""
            return launcher._is_allowed_channel(ctx.channel.id)

        @self.bot.event
        async def on_ready():
            """Announce launcher startup and start the health check loop."""
            logging.info("Discord launcher connected as %s", launcher.bot.user)
            logging.info("Registered Discord commands: %s", sorted(command.name for command in launcher.bot.commands))
            if not launcher.health_check.is_running():
                launcher.health_check.start()
            await launcher._send_channel(
                "🚀 KalshiBot Launcher is online!\n"
                "Type !help for commands.\n"
                f"KalshiBot is currently: {launcher._status_label()}"
            )

        @self.bot.event
        async def on_command_error(ctx, error):
            """Reply to command lookup failures in the configured channel."""
            if not launcher._is_allowed_channel(ctx.channel.id):
                return
            if isinstance(error, commands.CommandNotFound):
                await ctx.send(f"Unknown command. Try `!help`.")
                return
            if isinstance(error, commands.CheckFailure):
                return
            raise error

        @self.bot.command(name="start")
        async def start(ctx):
            """Start main.py if it is not already running."""
            if launcher._is_running():
                await ctx.send("⚠️ KalshiBot is already running.")
                return
            await ctx.send("▶️ Starting KalshiBot...")
            launcher._start_process()
            await ctx.send(f"✅ KalshiBot is running. DRY_RUN={os.getenv('DRY_RUN', 'true')}")

        @self.bot.command(name="stop")
        async def stop(ctx):
            """Stop main.py if running, then shut down the launcher cleanly."""
            await ctx.send("⏹️ Stopping KalshiBot launcher...")
            if launcher._is_running():
                await launcher._stop_process()
            await ctx.send("✅ KalshiBot launcher stopped.")
            await launcher.bot.close()

        @self.bot.command(name="restart")
        async def restart(ctx):
            """Restart main.py."""
            await ctx.send("🔄 Restarting KalshiBot...")
            if launcher._is_running():
                await launcher._stop_process()
            launcher._start_process()
            await ctx.send("✅ KalshiBot restarted successfully.")

        @self.bot.command(name="status")
        async def status(ctx):
            """Show launcher and bot process status."""
            dry_run = os.getenv("DRY_RUN", "true")
            k = KalshiClient()
            balance = k.get_balance()
            balance_str = f"${balance:.2f}" if balance else "unavailable"
            balance_line = f"Kalshi balance: {balance_str}"
            state = launcher._db_status()
            running = "Yes" if launcher._is_running() else "No"
            last_scan = launcher._last_scan_time()
            window = launcher._scan_window()
            if launcher._is_running():
                await ctx.send(
                    "🤖 KalshiBot Status\n"
                    f"Mode: {'DRY RUN' if dry_run.lower() == 'true' else 'LIVE'}\n"
                    f"Today's budget: ${state['todays_budget']:.2f} remaining\n"
                    f"Running budget: ${state['running_budget']:.2f} (compounded)\n"
                    f"{balance_line}\n"
                    f"Total pocketed: ${state['total_pocketed']:.2f}\n"
                    f"Open positions: {state['open_positions']} / 1\n"
                    f"Daily P&L: ${state['daily_pnl']:.2f}\n"
                    f"Scan window: {window}\n"
                    f"Bot running: {running}\n"
                    f"Last scan: {last_scan}\n"
                    f"KalshiBot: ✅ Running (PID: {launcher.process.pid})\n"
                    f"Uptime: {launcher._uptime()}"
                )
            else:
                await ctx.send(
                    "🤖 KalshiBot Status\n"
                    f"Mode: {'DRY RUN' if dry_run.lower() == 'true' else 'LIVE'}\n"
                    f"Today's budget: ${state['todays_budget']:.2f} remaining\n"
                    f"Running budget: ${state['running_budget']:.2f} (compounded)\n"
                    f"{balance_line}\n"
                    f"Total pocketed: ${state['total_pocketed']:.2f}\n"
                    f"Open positions: {state['open_positions']} / 1\n"
                    f"Daily P&L: ${state['daily_pnl']:.2f}\n"
                    f"Scan window: {window}\n"
                    f"Bot running: {running}\n"
                    f"Last scan: {last_scan}\n"
                    "KalshiBot: ⛔ Stopped"
                )

        @self.bot.command(name="logs")
        async def logs_cmd(ctx):
            """Show last 30 lines as text."""
            if str(ctx.channel.id) != launcher.channel_id:
                return
            try:
                log_path = PROJECT_ROOT / "logs" / "bot.log"
                if not log_path.exists():
                    await ctx.send("No log file found.")
                    return
                lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                last_30 = "\n".join(lines[-30:])
                if len(last_30) > 1900:
                    last_30 = last_30[-1900:]
                await ctx.send(f"```\n{last_30}\n```")
            except Exception as exc:
                await ctx.send(f"Error reading logs: {exc}")

        @self.bot.command(name="logsfull")
        async def logsfull_cmd(ctx):
            """Upload the entire log file as a Discord attachment."""
            if str(ctx.channel.id) != launcher.channel_id:
                return
            try:
                log_path = PROJECT_ROOT / "logs" / "bot.log"
                if not log_path.exists():
                    await ctx.send("No log file found.")
                    return
                size_mb = log_path.stat().st_size / (1024 * 1024)
                if size_mb > 8:
                    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                    trimmed = "\n".join(lines[-5000:])
                    temp_path = PROJECT_ROOT / "logs" / "bot_trimmed.log"
                    temp_path.write_text(trimmed, encoding="utf-8")
                    await ctx.send(
                        f"Full log is {size_mb:.1f}MB (too large) — sending last 5000 lines instead:",
                        file=discord.File(str(temp_path)),
                    )
                    temp_path.unlink(missing_ok=True)
                else:
                    await ctx.send(
                        f"Full log file ({size_mb:.2f}MB):",
                        file=discord.File(str(log_path)),
                    )
            except Exception as exc:
                await ctx.send(f"Error sending log file: {exc}")

        @self.bot.command(name="logssince")
        async def logssince_cmd(ctx, hours: int = 24):
            """Upload only log lines from the last N hours."""
            if str(ctx.channel.id) != launcher.channel_id:
                return
            try:
                from datetime import timedelta

                log_path = PROJECT_ROOT / "logs" / "bot.log"
                if not log_path.exists():
                    await ctx.send("No log file found.")
                    return
                cutoff = datetime.now() - timedelta(hours=hours)
                lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                recent = []
                for line in lines:
                    try:
                        ts_str = line[:19]
                        ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                        if ts >= cutoff:
                            recent.append(line)
                    except (ValueError, IndexError):
                        if recent:
                            recent.append(line)
                content = "\n".join(recent)
                temp_path = PROJECT_ROOT / "logs" / f"last_{hours}h.log"
                temp_path.write_text(content, encoding="utf-8")
                await ctx.send(
                    f"Logs from last {hours} hours ({len(recent)} lines):",
                    file=discord.File(str(temp_path)),
                )
                temp_path.unlink(missing_ok=True)
            except Exception as exc:
                await ctx.send(f"Error: {exc}")

        @self.bot.command(name="balance")
        async def balance_cmd(ctx):
            """Fetch and show the real Kalshi account balance."""
            if str(ctx.channel.id) != launcher.channel_id:
                return
            from src.kalshi_client import KalshiClient

            k = KalshiClient()
            b = k.get_balance()
            if b:
                await ctx.send(f"💰 Kalshi Balance: **${b:.2f}**")
            else:
                await ctx.send("⚠️ Could not fetch balance")

        @self.bot.command(name="ps")
        async def ps_cmd(ctx):
            """Show discord_launcher processes for remote diagnostics."""
            if str(ctx.channel.id) != launcher.channel_id:
                return
            try:
                result = subprocess.run(
                    "ps aux | grep discord_launcher",
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                output = result.stdout.strip() or result.stderr.strip() or "No output."
                if len(output) > 1900:
                    output = output[-1900:]
                await ctx.send(f"```\n{output}\n```")
            except Exception as exc:
                await ctx.send(f"Error running ps: {exc}")

        @self.bot.command(name="gitstatus")
        async def gitstatus_cmd(ctx):
            """Show git status and latest commit for remote diagnostics."""
            if str(ctx.channel.id) != launcher.channel_id:
                return
            try:
                status = subprocess.run(
                    ["git", "status"],
                    cwd=PROJECT_ROOT,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                latest = subprocess.run(
                    ["git", "log", "-1", "--oneline"],
                    cwd=PROJECT_ROOT,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                output = (
                    "$ git status\n"
                    f"{status.stdout or status.stderr}\n"
                    "$ git log -1 --oneline\n"
                    f"{latest.stdout or latest.stderr}"
                ).strip()
                if len(output) > 1900:
                    output = output[-1900:]
                await ctx.send(f"```\n{output}\n```")
            except Exception as exc:
                await ctx.send(f"Error running git status: {exc}")

        @self.bot.command(name="help")
        async def help_command(ctx):
            """Show all launcher commands."""
            await ctx.send(
                "🎮 KalshiBot Launcher Commands\n"
                "!start   — start the trading bot\n"
                "!stop    — stop the trading bot\n"
                "!restart — restart the trading bot\n"
                "!status  — check if bot is running\n"
                "!balance — show real Kalshi account balance\n"
                "!logs    — show last 30 log lines\n"
                "!logsfull — upload full log file\n"
                "!logssince [hours] — upload recent logs\n"
                "!ps      — show launcher processes\n"
                "!gitstatus — show git status and latest commit\n"
                "!pocket  — show pocketed profits\n"
                "!budget  — show compounding budget\n"
                "!golive CONFIRM — switch to live trading\n"
                "!help    — show this message"
            )

        @self.bot.command(name="pocket")
        async def pocket(ctx):
            """Show total pocketed amount and recent pocket history."""
            state = launcher._db_status()
            lines = [f"Total pocketed: ${state['total_pocketed']:.2f}"]
            for row in launcher._budget_history():
                lines.append(f"{row['created_at'][:10]} pocketed=${row['pocketed']:.2f} profit=${row['gross_profit']:.2f}")
            await ctx.send("💰 Pocketed Profits\n" + "\n".join(lines))

        @self.bot.command(name="budget")
        async def budget(ctx):
            """Show current running budget and recent compounding history."""
            state = launcher._db_status()
            lines = [
                f"Running budget: ${state['running_budget']:.2f}",
                f"Today's remaining: ${state['todays_budget']:.2f}",
            ]
            for row in launcher._budget_history():
                lines.append(
                    f"{row['created_at'][:10]} profit=${row['gross_profit']:.2f} "
                    f"reinvested=${row['reinvested']:.2f} budget=${row['running_budget']:.2f}"
                )
            await ctx.send("📈 Budget Compounding\n" + "\n".join(lines))

        @self.bot.command(name="golive")
        async def golive(ctx, confirmation=None):
            """Switch .env to production live trading after explicit confirmation."""
            if confirmation != "CONFIRM":
                await ctx.send("Type `!golive CONFIRM` to switch to live trading.")
                return
            await ctx.send("⚠️ Switching to LIVE TRADING. Real money at risk.")
            launcher._update_env({"KALSHI_ENV": "prod", "DRY_RUN": "false"})
            await ctx.send("✅ KALSHI_ENV=prod and DRY_RUN=false written to .env. Restart the bot for changes to apply.")

    def _register_tasks(self):
        """Register the 60-second process health check loop."""
        launcher = self

        @tasks.loop(seconds=60)
        async def health_check():
            """Alert Discord if main.py exits unexpectedly."""
            if launcher.process is None:
                return
            if launcher.process.poll() is not None:
                launcher._close_process_log()
                launcher.process = None
                launcher.start_time = None
                await launcher._send_channel("🚨 KalshiBot crashed unexpectedly! Use !start to restart.")

        self.health_check = health_check

    def run(self):
        """Run the Discord launcher."""
        token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
        if not token or token == "your_token_here":
            logging.warning("DISCORD_BOT_TOKEN is not configured.")
            return
        self.bot.run(token)

    def _start_process(self):
        """Start main.py as a subprocess."""
        log_dir = PROJECT_ROOT / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "bot.log"
        self.process_log_file = log_path.open("a", encoding="utf-8", buffering=1)
        self.process_log_file.write(
            f"\n{datetime.now():%Y-%m-%d %H:%M:%S} LAUNCHER  Starting main.py subprocess\n"
        )
        venv_python = PROJECT_ROOT / "venv" / "bin" / "python"
        python_bin = str(venv_python) if venv_python.exists() else sys.executable
        self.process = subprocess.Popen(
            [python_bin, str(PROJECT_ROOT / "main.py")],
            cwd=PROJECT_ROOT,
            stdout=self.process_log_file,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.start_time = datetime.now()

    async def _stop_process(self):
        """Terminate the running main.py subprocess."""
        if self.process is None:
            return
        self.process.terminate()
        try:
            await asyncio.to_thread(self.process.wait, timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            await asyncio.to_thread(self.process.wait)
        self.process = None
        self.start_time = None
        self._close_process_log()

    def _close_process_log(self):
        """Close the subprocess log file handle if open."""
        if self.process_log_file is None:
            return
        try:
            self.process_log_file.write(
                f"{datetime.now():%Y-%m-%d %H:%M:%S} LAUNCHER  main.py subprocess ended\n"
            )
            self.process_log_file.close()
        finally:
            self.process_log_file = None

    def _is_running(self):
        """Return True when main.py is currently alive."""
        return self.process is not None and self.process.poll() is None

    def _status_label(self):
        """Return a compact running/stopped status label."""
        return "✅ Running" if self._is_running() else "⛔ Stopped"

    def _uptime(self):
        """Return process uptime as hours and minutes."""
        if self.start_time is None:
            return "0h 0m"
        delta = datetime.now() - self.start_time
        total_minutes = int(delta.total_seconds() // 60)
        hours, minutes = divmod(total_minutes, 60)
        return f"{hours}h {minutes}m"

    def _last_log_lines(self):
        """Read the last 20 lines from logs/bot.log."""
        log_path = PROJECT_ROOT / "logs" / "bot.log"
        if not log_path.exists():
            return "No log file found."
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-20:]) or "Log file is empty."

    def _last_scan_time(self):
        """Return the last scan timestamp seen in logs/bot.log."""
        log_path = PROJECT_ROOT / "logs" / "bot.log"
        if not log_path.exists():
            return "Never"
        for line in reversed(log_path.read_text(encoding="utf-8", errors="replace").splitlines()):
            if "Full pipeline started" in line or "Startup scan requested" in line:
                return line[:19]
        return "Never"

    def _scan_window(self):
        """Return the current Eastern-time scan window label."""
        et_hour = datetime.now(ET).hour
        if 6 <= et_hour < 14:
            return "🟢 PRIME (every 5 min)"
        if 14 <= et_hour < 18:
            return "🟡 Afternoon (every 15 min)"
        if 18 <= et_hour < 23:
            return "🟠 Evening (every 30 min)"
        return "🔵 Overnight (every 60 min)"

    def _db_status(self):
        """Read budget and P&L status directly from SQLite."""
        running_budget = self._risk_state_float("running_budget", 100.0)
        total_pocketed = self._risk_state_float("total_pocketed", 0.0)
        spent_today = self._scalar(
            "SELECT COALESCE(SUM(stake), 0) FROM positions WHERE DATE(opened_at) = DATE('now')",
            0.0,
        )
        daily_pnl = self._scalar(
            "SELECT COALESCE(SUM(realized_pnl), 0) FROM pnl WHERE DATE(created_at) = DATE('now')",
            0.0,
        )
        open_positions = int(self._scalar("SELECT COUNT(*) FROM positions WHERE status = 'open'", 0.0))
        return {
            "running_budget": running_budget,
            "total_pocketed": total_pocketed,
            "todays_budget": max(0.0, round(running_budget - spent_today, 2)),
            "daily_pnl": daily_pnl,
            "open_positions": open_positions,
        }

    def _risk_state_float(self, key, default):
        """Read a numeric value from the risk_state table."""
        db_path = PROJECT_ROOT / "data" / "positions.db"
        if not db_path.exists():
            return default
        try:
            with sqlite3.connect(db_path) as conn:
                row = conn.execute("SELECT value FROM risk_state WHERE key = ?", (key,)).fetchone()
            return default if row is None else float(row[0])
        except Exception:
            return default

    def _scalar(self, query, default):
        """Read one numeric aggregate from SQLite."""
        db_path = PROJECT_ROOT / "data" / "positions.db"
        if not db_path.exists():
            return default
        try:
            with sqlite3.connect(db_path) as conn:
                row = conn.execute(query).fetchone()
            return default if row is None else float(row[0])
        except Exception:
            return default

    def _budget_history(self):
        """Read recent budget compounding history rows."""
        db_path = PROJECT_ROOT / "data" / "positions.db"
        if not db_path.exists():
            return []
        try:
            with sqlite3.connect(db_path) as conn:
                rows = conn.execute(
                    """
                    SELECT created_at, gross_profit, reinvested, pocketed, running_budget
                    FROM budget_history
                    ORDER BY created_at DESC
                    LIMIT 7
                    """
                ).fetchall()
            return [
                {
                    "created_at": str(row[0]),
                    "gross_profit": float(row[1]),
                    "reinvested": float(row[2]),
                    "pocketed": float(row[3]),
                    "running_budget": float(row[4]),
                }
                for row in rows
            ]
        except Exception:
            return []

    def _update_env(self, updates):
        """Update simple KEY=value pairs in the local .env file."""
        env_path = PROJECT_ROOT / ".env"
        lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
        seen = set()
        for index, line in enumerate(lines):
            key = line.split("=", 1)[0] if "=" in line else ""
            if key in updates:
                lines[index] = f"{key}={updates[key]}"
                seen.add(key)
        for key, value in updates.items():
            if key not in seen:
                lines.append(f"{key}={value}")
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    async def _send_channel(self, message):
        """Send a message to the configured channel."""
        if not self.channel_id:
            return
        channel_id = int(self.channel_id)
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            channel = await self.bot.fetch_channel(channel_id)
        await channel.send(message)

    def _is_allowed_channel(self, channel_id):
        """Return True only for the configured Discord channel."""
        return self.channel_id and str(channel_id) == self.channel_id


if __name__ == "__main__":
    check_single_instance()
    launcher = BotLauncher()
    launcher.run()

"""Standalone Discord launcher for controlling the Kalshi weather bot process."""

from __future__ import annotations

import os
import subprocess
import logging
import asyncio
import sqlite3
from datetime import datetime
from pathlib import Path

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv


load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


class BotLauncher:
    """Lightweight Discord bot that starts and stops main.py as a subprocess."""

    def __init__(self):
        """Create Discord command handlers and initialize process state."""
        self.process = None
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
            if not launcher.health_check.is_running():
                launcher.health_check.start()
            await launcher._send_channel(
                "🚀 KalshiBot Launcher is online!\n"
                "Type !help for commands.\n"
                f"KalshiBot is currently: {launcher._status_label()}"
            )

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
            """Stop main.py if it is running."""
            if not launcher._is_running():
                await ctx.send("⚠️ KalshiBot is not running.")
                return
            await ctx.send("⏹️ Stopping KalshiBot...")
            await launcher._stop_process()
            await ctx.send("✅ KalshiBot stopped.")

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
            state = launcher._db_status()
            running = "Yes" if launcher._is_running() else "No"
            last_scan = launcher._last_scan_time()
            if launcher._is_running():
                await ctx.send(
                    "🤖 KalshiBot Status\n"
                    f"Mode: {'DRY RUN' if dry_run.lower() == 'true' else 'LIVE'}\n"
                    f"Today's budget: ${state['todays_budget']:.2f} remaining\n"
                    f"Running budget: ${state['running_budget']:.2f} (compounded)\n"
                    f"Total pocketed: ${state['total_pocketed']:.2f}\n"
                    f"Open positions: {state['open_positions']} / 1\n"
                    f"Daily P&L: ${state['daily_pnl']:.2f}\n"
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
                    f"Total pocketed: ${state['total_pocketed']:.2f}\n"
                    f"Open positions: {state['open_positions']} / 1\n"
                    f"Daily P&L: ${state['daily_pnl']:.2f}\n"
                    f"Bot running: {running}\n"
                    f"Last scan: {last_scan}\n"
                    "KalshiBot: ⛔ Stopped"
                )

        @self.bot.command(name="logs")
        async def logs(ctx):
            """Show the last 20 lines of logs/bot.log."""
            log_text = launcher._last_log_lines()
            await ctx.send(f"📋 Last 20 log lines:\n{log_text}"[:1900])

        @self.bot.command(name="help")
        async def help_command(ctx):
            """Show all launcher commands."""
            await ctx.send(
                "🎮 KalshiBot Launcher Commands\n"
                "!start   — start the trading bot\n"
                "!stop    — stop the trading bot\n"
                "!restart — restart the trading bot\n"
                "!status  — check if bot is running\n"
                "!logs    — show last 20 log lines\n"
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
        self.process = subprocess.Popen(["python", "main.py"], cwd=os.getcwd())
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
        log_path = Path("logs") / "bot.log"
        if not log_path.exists():
            return "No log file found."
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-20:]) or "Log file is empty."

    def _last_scan_time(self):
        """Return the last scan timestamp seen in logs/bot.log."""
        log_path = Path("logs") / "bot.log"
        if not log_path.exists():
            return "Never"
        for line in reversed(log_path.read_text(encoding="utf-8", errors="replace").splitlines()):
            if "Full pipeline started" in line or "Startup scan requested" in line:
                return line[:19]
        return "Never"

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
        db_path = Path("data") / "positions.db"
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
        db_path = Path("data") / "positions.db"
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
        db_path = Path("data") / "positions.db"
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
        env_path = Path(".env")
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
    launcher = BotLauncher()
    launcher.run()

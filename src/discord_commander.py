"""Two-way Discord control panel for the Kalshi weather bot."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from datetime import datetime

import discord
from discord.ext import commands


class DiscordCommander:
    """Listen for Discord commands and control the running trader."""

    def __init__(self, pipeline_fn: Callable[[], None], trader: object) -> None:
        """Create the Discord bot and register all command handlers."""
        self.pipeline_fn = pipeline_fn
        self.trader = trader
        self.channel_id = self._read_channel_id()
        intents = discord.Intents.all()
        self.bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)
        self._register_events()
        self._register_commands()

    def start_in_background(self) -> None:
        """Start the Discord commander in a background thread."""
        import threading

        thread = threading.Thread(target=self._run, daemon=True)
        thread.start()

    def _run(self) -> None:
        """Run the Discord bot using the configured bot token."""
        token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
        if not token or token == "your_token_here":
            logging.warning("Discord token not configured — commander disabled.")
            return
        self.bot.run(token)

    def _register_events(self) -> None:
        """Register Discord lifecycle and message filter events."""
        commander = self

        @self.bot.event
        async def on_ready() -> None:
            """Log when the Discord commander is connected."""
            logging.info("Discord commander connected as %s.", commander.bot.user)

        @self.bot.check
        async def channel_only(ctx: commands.Context) -> bool:
            """Only accept commands from the configured channel ID."""
            return commander.channel_id is not None and ctx.channel.id == commander.channel_id

    def _register_commands(self) -> None:
        """Register all bot commands."""
        commander = self

        @self.bot.command(name="scan")
        async def scan(ctx: commands.Context) -> None:
            """Trigger an immediate market scan."""
            await ctx.reply("🔍 Manual scan started...")
            commander.pipeline_fn()
            await ctx.reply("✅ Scan complete.")

        @self.bot.command(name="status")
        async def status(ctx: commands.Context) -> None:
            """Report the current bot mode, risk status, and scan time."""
            risk = commander.trader.risk
            daily_pnl = risk.realized_pnl_today()
            open_positions = risk.open_position_count()
            budget_used = risk.opened_notional_today()
            daily_loss = abs(min(0.0, daily_pnl))
            mode = "DRY RUN" if commander.trader.dry_run else "LIVE"
            last_scan = commander._format_last_scan()
            await ctx.reply(
                "🤖 KalshiBot Status\n"
                f"Mode: {mode}\n"
                f"Daily P&L: ${daily_pnl:.2f}\n"
                f"Open positions: {open_positions}\n"
                f"Daily budget used: ${budget_used:.2f} / ${risk.daily_budget:.2f}\n"
                f"Daily loss limit: ${daily_loss:.2f} / ${risk.daily_loss_limit:.2f}\n"
                "Bot running: Yes\n"
                f"Last scan: {last_scan}"
            )

        @self.bot.command(name="pause")
        async def pause(ctx: commands.Context) -> None:
            """Pause scheduled and manual trading scans."""
            commander.trader.paused = True
            await ctx.reply("⏸️ Bot paused. Use !resume to restart scanning.")

        @self.bot.command(name="resume")
        async def resume(ctx: commands.Context) -> None:
            """Resume trading scans and immediately run one scan."""
            commander.trader.paused = False
            await ctx.reply("▶️ Bot resumed. Running scan now...")
            commander.pipeline_fn()

        @self.bot.command(name="pnl")
        async def pnl(ctx: commands.Context) -> None:
            """Report profit, loss, and open risk summary."""
            risk = commander.trader.risk
            await ctx.reply(
                "📊 P&L Summary\n"
                f"Today: ${risk.realized_pnl_today():.2f}\n"
                f"This month: ${risk.realized_pnl_month():.2f}\n"
                f"Open risk: ${risk.opened_notional_today():.2f}\n"
                f"Open positions: {risk.open_position_count()}"
            )

        @self.bot.command(name="help")
        async def help_command(ctx: commands.Context) -> None:
            """Show all available Discord commands."""
            await ctx.reply(
                "🤖 KalshiBot Commands\n"
                "!scan — trigger immediate market scan\n"
                "!status — show bot status and limits\n"
                "!pnl — show profit/loss summary\n"
                "!pause — pause trading\n"
                "!resume — resume trading\n"
                "!help — show this message\n"
                "!dryrun on — enable dry run mode\n"
                "!dryrun off — disable dry run mode"
            )

        @self.bot.group(name="dryrun", invoke_without_command=True)
        async def dryrun(ctx: commands.Context, mode: str | None = None) -> None:
            """Toggle dry-run mode from Discord."""
            if mode == "on":
                commander.trader.dry_run = True
                await ctx.reply("🧪 Dry run mode ON. No real money will be spent.")
            elif mode == "off":
                commander.trader.dry_run = False
                await ctx.reply("⚠️ Dry run mode OFF. Bot will place REAL trades.")
            else:
                await ctx.reply("Use `!dryrun on` or `!dryrun off`.")

    def _format_last_scan(self) -> str:
        """Return the trader's last scan time as readable text."""
        last_scan = getattr(self.trader, "last_scan_time", None)
        if isinstance(last_scan, datetime):
            return last_scan.isoformat()
        return "Never"

    def _read_channel_id(self) -> int | None:
        """Read DISCORD_CHANNEL_ID from the environment as an integer."""
        raw = os.getenv("DISCORD_CHANNEL_ID", "").strip()
        try:
            return int(raw)
        except ValueError:
            return None

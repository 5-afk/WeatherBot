"""Standalone Discord launcher for controlling the Kalshi weather bot process."""

from __future__ import annotations

import os
import sys
import atexit
import subprocess
import logging
import asyncio
import sqlite3
import threading
import socket
from datetime import datetime
from pathlib import Path
import zoneinfo

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))
from src.bot_control import request_scan, set_paused
from src.kalshi_client import KalshiClient
from src.atlas_control import register_launcher


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
        self._api_thread = None
        self._register_commands()
        self._register_tasks()
        register_launcher(self)
        self._start_atlas_api()

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

        @self.bot.command(name="scan")
        async def scan_cmd(ctx):
            """Trigger an immediate market scan on the running bot process."""
            if not launcher._is_running():
                await ctx.send("KalshiBot is not running. Use `!start` first.")
                return
            request_scan()
            await ctx.send("🔍 Manual scan requested — main.py will pick it up shortly.")

        @self.bot.command(name="pause")
        async def pause_cmd(ctx):
            """Pause scheduled and manual trading scans."""
            set_paused(True)
            await ctx.send("⏸️ Bot paused. Use `!resume` to restart scanning.")

        @self.bot.command(name="resume")
        async def resume_cmd(ctx):
            """Resume trading scans and request an immediate scan."""
            if not launcher._is_running():
                await ctx.send("KalshiBot is not running. Use `!start` first.")
                return
            set_paused(False)
            request_scan()
            await ctx.send("▶️ Bot resumed. Manual scan requested.")

        @self.bot.command(name="pnl")
        async def pnl_cmd(ctx):
            """Show profit/loss and open risk summary."""
            state = launcher._db_status()
            monthly_pnl = launcher._scalar(
                "SELECT COALESCE(SUM(realized_pnl), 0) FROM pnl "
                "WHERE SUBSTR(created_at, 1, 7) = strftime('%Y-%m', 'now')",
                0.0,
            )
            open_risk = launcher._scalar(
                "SELECT COALESCE(SUM(stake), 0) FROM positions WHERE status = 'open'",
                0.0,
            )
            await ctx.send(
                "📊 P&L Summary\n"
                f"Today: ${state['daily_pnl']:.2f}\n"
                f"This month: ${monthly_pnl:.2f}\n"
                f"Open risk: ${open_risk:.2f}\n"
                f"Open positions: {state['open_positions']}"
            )

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
            paused = "Yes" if (PROJECT_ROOT / "data" / "paused.flag").exists() else "No"
            last_scan = launcher._last_scan_time()
            window = launcher._scan_window()
            dynamic_max = launcher._dynamic_max_positions(state)
            position_line = f"Open positions: {state['open_positions']} / {dynamic_max} (dynamic)"
            if launcher._is_running():
                await ctx.send(
                    "🤖 KalshiBot Status\n"
                    f"Mode: {'DRY RUN' if dry_run.lower() == 'true' else 'LIVE'}\n"
                    f"Today's budget: ${state['todays_budget']:.2f} remaining\n"
                    f"Running budget: ${state['running_budget']:.2f} (compounded)\n"
                    f"{balance_line}\n"
                    f"Total pocketed: ${state['total_pocketed']:.2f}\n"
                    f"{position_line}\n"
                    f"Daily P&L: ${state['daily_pnl']:.2f}\n"
                    f"Scan window: {window}\n"
                    f"Paused: {paused}\n"
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
                    f"{position_line}\n"
                    f"Daily P&L: ${state['daily_pnl']:.2f}\n"
                    f"Scan window: {window}\n"
                    f"Paused: {paused}\n"
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

        @self.bot.command(name="resetstate")
        async def resetstate_cmd(ctx):
            """Sync SQLite running_budget and open positions with live Kalshi state."""
            if str(ctx.channel.id) != launcher.channel_id:
                return
            await ctx.send("🔄 Syncing state from Kalshi...")
            try:
                report = launcher._reset_state_from_kalshi()
            except Exception as exc:
                await ctx.send(f"⚠️ State reset failed: {exc}")
                return
            await ctx.send(report)

        @self.bot.command(name="clearhalt")
        async def clearhalt_cmd(ctx):
            """Clear permanent halt and reset drawdown tracking to current balance."""
            if str(ctx.channel.id) != launcher.channel_id:
                return
            await ctx.send("🔄 Clearing halt and resetting drawdown tracking...")
            try:
                report = launcher._clear_halt_and_reset_drawdown()
            except Exception as exc:
                await ctx.send(f"⚠️ Clear halt failed: {exc}")
                return
            await ctx.send(report)

        @self.bot.command(name="positions")
        async def positions_cmd(ctx):
            """Show currently open positions fetched live from Kalshi with P&L."""
            if str(ctx.channel.id) != launcher.channel_id:
                return
            try:
                report = launcher._open_positions_report()
            except Exception as exc:
                await ctx.send(f"⚠️ Could not fetch positions: {exc}")
                return
            await ctx.send(report)

        @self.bot.command(name="syncpositions")
        async def syncpositions_cmd(ctx):
            """Sync open positions from the Kalshi API into the local SQLite DB."""
            if str(ctx.channel.id) != launcher.channel_id:
                return
            await ctx.send("🔄 Syncing positions from Kalshi...")
            try:
                synced, lines = launcher._sync_positions_from_kalshi()
            except Exception as exc:
                await ctx.send(f"⚠️ Sync failed: {exc}")
                return
            if synced == 0:
                await ctx.send("No open positions found on Kalshi.")
                return
            body = "\n".join(lines)
            await ctx.send(f"✅ Synced {synced} position(s):\n```\n{body}\n```")

        @self.bot.command(name="auditpositions")
        async def auditpositions_cmd(ctx):
            """Audit open positions, recommend HOLD/SELL, auto-sell where possible."""
            if str(ctx.channel.id) != launcher.channel_id:
                return
            await ctx.send("🔍 Auditing open positions...")

            try:
                launcher._sync_positions_from_kalshi()
            except Exception as exc:
                await ctx.send(f"⚠️ Position sync failed: {exc}")
                return

            from src.trader import Trader

            try:
                trader = Trader()
                audit_results = trader.audit_positions()
            except Exception as exc:
                await ctx.send(f"⚠️ Audit failed: {exc}")
                return

            if not audit_results:
                await ctx.send("📊 No open positions to audit.")
                return

            for result in audit_results:
                ticker = result["ticker"]
                rec = result["recommendation"]
                reason = result["reason"]

                if rec in {"ALREADY_SETTLED", "ERROR"}:
                    emoji = "ℹ️" if rec == "ALREADY_SETTLED" else "❌"
                    await ctx.send(f"{emoji} **{ticker}** — **{rec}**: {reason}")
                    continue

                urgency = result.get("urgency", "LOW")
                has_liquidity = result.get("has_liquidity", False)
                pnl = result.get("unrealized_pnl", 0)
                pnl_pct = result.get("unrealized_pct", 0)
                contracts = result.get("contracts", 0)
                side = result.get("side", "")
                current_price = result.get("current_price", 0)
                hours = result.get("hours_until", 0)

                emoji = "✅" if rec == "HOLD" else "⚠️" if urgency == "MEDIUM" else "🚨"

                if result.get("auto_sell"):
                    sell_result = trader.kalshi.sell_position(ticker, side, contracts)
                    if sell_result["filled"]:
                        filled = sell_result["contracts_filled"]
                        price = sell_result["price"]
                        proceeds = filled * price
                        await ctx.send(
                            f"🚨 **AUTO-SOLD** {ticker}\n"
                            f"Sold {filled} {side.upper()} contracts @ ${price:.2f}\n"
                            f"Proceeds: ${proceeds:.2f}\n"
                            f"Reason: {reason}"
                        )
                        continue
                    result["has_liquidity"] = False
                    has_liquidity = False

                forecast_temp = result.get("forecast_temp")
                forecast_str = f"{forecast_temp:.1f}" if forecast_temp is not None else "N/A"
                current_edge = result.get("current_edge")
                edge_str = f"{current_edge:.2f}" if current_edge is not None else "N/A"

                msg = (
                    f"{emoji} **{ticker}**\n"
                    f"Side: {side.upper()} | Contracts: {contracts} | "
                    f"Current price: ${current_price:.2f}\n"
                    f"P&L: ${pnl:+.2f} ({pnl_pct:+.0f}%) | "
                    f"Settlement: {hours:.1f}h\n"
                    f"Forecast: {forecast_str}°F | Edge: {edge_str}\n"
                    f"**{rec}** — {reason}\n"
                )

                if rec == "SELL" and not has_liquidity:
                    msg += (
                        "\n⚠️ **LOW LIQUIDITY — Manual sell required:**\n"
                        "```\n"
                        f"Go to: kalshi.com/markets/{ticker}\n"
                        "Click: SELL tab\n"
                        f"Shares: {contracts}\n"
                        "Order type: LIMIT\n"
                        "Expiration: EOD (End of Day)\n"
                        f"Limit price: ${max(0.01, current_price - 0.02):.2f} "
                        "(or lower to 1¢ for immediate fill)\n"
                        "Submit as resting order: YES (wait for buyer)\n"
                        "```"
                    )
                elif rec == "SELL" and has_liquidity:
                    msg += f"\n💡 **Liquid — use `!sellposition {ticker}` to sell now**"

                await ctx.send(msg)

            sells = [r for r in audit_results if r["recommendation"] == "SELL"]
            holds = [r for r in audit_results if r["recommendation"] == "HOLD"]
            await ctx.send(
                f"📊 **Audit complete:** {len(holds)} HOLD, {len(sells)} SELL recommended\n"
                "Run `!auditpositions` again after manual sells to re-evaluate."
            )

        @self.bot.command(name="sellposition")
        async def sellposition_cmd(ctx, ticker: str | None = None):
            """Manually sell all contracts of a specific open position."""
            if str(ctx.channel.id) != launcher.channel_id:
                return
            if not ticker:
                await ctx.send("Usage: `!sellposition KXHIGHMIA-26JUL06-B91.5`")
                return

            positions = launcher._get_open_positions()
            pos = next((p for p in positions if p["ticker"] == ticker), None)
            if pos is None:
                pos = next((p for p in positions if p["ticker"].upper() == ticker.upper()), None)

            if not pos:
                await ctx.send(f"❌ No open position found for {ticker}")
                return

            side = str(pos["side"]).lower()
            contracts = int(pos["contracts"])
            await ctx.send(
                f"⏳ Attempting to sell {contracts} {side.upper()} contracts on {pos['ticker']}..."
            )

            from src.kalshi_client import KalshiClient

            k = KalshiClient()
            result = k.sell_position(pos["ticker"], side, contracts)

            if result["filled"]:
                proceeds = result["contracts_filled"] * result["price"]
                await ctx.send(
                    f"✅ **Sold** {result['contracts_filled']} contracts @ ${result['price']:.2f}\n"
                    f"Proceeds: **${proceeds:.2f}**\n"
                    "Run `!resetstate` to sync your balance."
                )
            else:
                market = k._get(f"/markets/{pos['ticker']}").get("market", {})
                if side == "no":
                    yes_bid = k._price_to_float(market.get("yes_bid_dollars")) or k._extract_price(market, "yes_bid") or 0.0
                    sell_price = yes_bid
                else:
                    no_bid = k._price_to_float(market.get("no_bid_dollars")) or k._extract_price(market, "no_bid") or 0.0
                    sell_price = 1.0 - no_bid

                await ctx.send(
                    "⚠️ **No immediate fill — Low liquidity**\n"
                    "Manual sell required on Kalshi.com:\n"
                    "```\n"
                    f"Market: {pos['ticker']}\n"
                    "Tab: SELL\n"
                    f"Shares: {contracts}\n"
                    "Order type: LIMIT\n"
                    "Expiration: EOD\n"
                    f"Limit price: ${max(0.01, sell_price):.2f}\n"
                    "Submit as resting order: YES\n"
                    "```\n"
                    "Lower limit price to 1¢ if you want immediate fill at any price."
                )

        @self.bot.command(name="metar")
        async def metar_cmd(ctx, station: str | None = None):
            """Show real-time METAR observation for a settlement station."""
            if str(ctx.channel.id) != launcher.channel_id:
                return
            if not station:
                await ctx.send("Usage: `!metar KLAX` or `!metar KNYC`")
                return
            station = station.upper()
            from src.metar_tracker import MetarTracker

            tracker = MetarTracker()
            obs = tracker.update_station(station)
            if not obs:
                await ctx.send(f"❌ Could not fetch METAR for {station}")
                return
            daily_max = obs.get("daily_max_f", obs["temp_f"])
            daily_min = obs.get("daily_min_f", obs["temp_f"])
            trend = tracker.get_temperature_trend(station) or "insufficient data"
            await ctx.send(
                f"🌡️ **{station} Live METAR**\n"
                f"Current temp: **{obs['temp_f']:.1f}°F**\n"
                f"Today's max so far: **{daily_max:.1f}°F**\n"
                f"Today's min so far: **{daily_min:.1f}°F**\n"
                f"Trend: {trend}\n"
                f"Wind: {obs.get('wind_speed_kt', 'N/A')}kt @ {obs.get('wind_dir', 'N/A')}°\n"
                f"Sky: {obs.get('sky_cover', 'N/A')}\n"
                f"Observed: {obs.get('obs_time_utc', 'N/A')} UTC\n"
                f"Raw: `{obs.get('raw_metar', 'N/A')}`"
            )

        @self.bot.command(name="calibration")
        async def calibration_cmd(ctx):
            """Generate full calibration report from settled trade history."""
            if str(ctx.channel.id) != launcher.channel_id:
                return
            from src.calibration import CalibrationEngine

            db_path = str(PROJECT_ROOT / "data" / "positions.db")
            engine = CalibrationEngine(db_path)
            report = engine.full_report()

            if "error" in report:
                await ctx.send(f"⚠️ {report['error']}")
                return

            n = report["total_settled_trades"]
            wr = report["overall_win_rate"] * 100
            profit = report["total_profit"]
            brier = report["brier_score"]
            brier_v = report["brier_verdict"]
            clv = report["clv_analysis"]

            msg = (
                f"📊 **WhetherBot Calibration Report**\n"
                f"Trades settled: {n} | Win rate: {wr:.1f}% | "
                f"Total P&L: ${profit:+.2f}\n\n"
                f"**Brier Score:** {brier} {brier_v}\n"
                f"**CLV:** {clv.get('avg_clv', 'N/A')} "
                f"({clv.get('positive_clv_pct', 'N/A')}% positive) "
                f"{clv.get('verdict', '')}\n\n"
            )

            msg += "**Calibration Curve:**\n"
            for bucket in report["calibration_curve"]:
                arrow = (
                    "⬆️" if bucket["error"] > 0.05
                    else "⬇️" if bucket["error"] < -0.05
                    else "✅"
                )
                msg += (
                    f"{arrow} {bucket['bucket']}: "
                    f"predicted {bucket['predicted']:.0%} → "
                    f"actual {bucket['actual']:.0%} "
                    f"(n={bucket['count']})\n"
                )

            errors = report["forecast_error_by_city"]
            if errors:
                msg += "\n**Biggest NWS Bias:**\n"
                for e in errors[:3]:
                    msg += (
                        f"• {e['city']}: {e['avg_forecast_error_f']:+.1f}°F "
                        f"({e['bias_direction']}) "
                        f"→ suggest correction {e['suggested_correction']:+.1f}°F\n"
                    )

            sigmas = report["sigma_accuracy"]
            if sigmas:
                msg += "\n**Sigma Accuracy:**\n"
                for s in sigmas[:3]:
                    msg += (
                        f"• {s['city']}: using {s['sigma_used']}°F, "
                        f"actual MAE {s['actual_mae_f']}°F {s['verdict']}\n"
                    )

            msg += "\n**Win Rate by Hours Until Settlement:**\n"
            for w in report["win_rate_by_hours"]:
                msg += (
                    f"• {w['window']}: {w['win_rate']:.0%} win rate "
                    f"(n={w['trade_count']})\n"
                )

            if len(msg) > 1900:
                msg = msg[:1900] + "\n...(truncated)"

            await ctx.send(msg)

        @self.bot.command(name="improvemodel")
        async def improvemodel_cmd(ctx):
            """Suggest the single most impactful model improvement right now."""
            if str(ctx.channel.id) != launcher.channel_id:
                return
            from src.calibration import CalibrationEngine

            db_path = str(PROJECT_ROOT / "data" / "positions.db")
            engine = CalibrationEngine(db_path)
            trades = engine.get_settled_trades()

            if len(trades) < 10:
                await ctx.send(
                    f"⚠️ Only {len(trades)} settled trades — need at least 10 "
                    f"for meaningful analysis. Keep trading and check back!"
                )
                return

            report = engine.full_report()
            errors = report["forecast_error_by_city"]
            sigmas = report["sigma_accuracy"]
            clv = report["clv_analysis"]
            brier = report["brier_score"]
            curve = report["calibration_curve"]

            recommendations = []

            if clv.get("avg_clv") is not None and clv["avg_clv"] < -0.02:
                recommendations.append({
                    "priority": 1,
                    "issue": "Negative CLV — betting too early before signal is clear",
                    "fix": "Raise MIN_SIGNAL_SCORE from 0.75 to 0.80, or require METAR confirmation before betting",
                    "impact": "HIGH",
                })

            for s in sigmas:
                if s["trade_count"] >= 5 and s["ratio"] and s["ratio"] > 1.3:
                    recommendations.append({
                        "priority": 2,
                        "issue": (
                            f"{s['city']} sigma too small ({s['sigma_used']}°F) — "
                            f"actual MAE is {s['actual_mae_f']}°F"
                        ),
                        "fix": f"Change {s['city']} summer sigma to {s['suggested_sigma']}°F in CITY_SIGMA",
                        "impact": "HIGH",
                    })

            for e in errors:
                if e["trade_count"] >= 5 and abs(e["avg_forecast_error_f"]) > 2.0:
                    recommendations.append({
                        "priority": 3,
                        "issue": f"{e['city']} NWS bias {e['avg_forecast_error_f']:+.1f}°F",
                        "fix": f"Update CITY_BIAS_CORRECTIONS['{e['city']}'] by {e['suggested_correction']:+.1f}°F",
                        "impact": "MEDIUM",
                    })

            for bucket in curve:
                if bucket["count"] >= 5 and bucket["error"] < -0.10:
                    recommendations.append({
                        "priority": 4,
                        "issue": (
                            f"Overconfident in {bucket['bucket']} range — "
                            f"predicting {bucket['predicted']:.0%} but winning {bucket['actual']:.0%}"
                        ),
                        "fix": "Increase sigma for markets in this probability range",
                        "impact": "MEDIUM",
                    })

            if not recommendations:
                await ctx.send(
                    f"✅ No significant issues found in {len(trades)} trades. "
                    f"Brier score: {brier}. Keep collecting data!"
                )
                return

            top = sorted(recommendations, key=lambda x: x["priority"])[0]
            await ctx.send(
                f"🎯 **Single Most Impactful Improvement:**\n\n"
                f"**Issue:** {top['issue']}\n"
                f"**Fix:** {top['fix']}\n"
                f"**Impact:** {top['impact']}\n\n"
                f"Make this ONE change, then run `!calibration` again after "
                f"10+ more trades to confirm improvement.\n"
                f"Do not change anything else until that is confirmed."
            )

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

        @self.bot.command(name="dashboard")
        async def dashboard_cmd(ctx):
            """Link to the ATLAS Command Center dashboard."""
            if str(ctx.channel.id) != launcher.channel_id:
                return
            port = int(os.getenv("ATLAS_PORT", "5000"))
            dry_run = os.getenv("DRY_RUN", "true")
            mode = "DRY RUN" if dry_run.lower() == "true" else "LIVE"
            from src.api import AGENTS, _load_agents

            _load_agents()
            agent_count = len(AGENTS)
            urls = f"http://localhost:{port}"
            if os.getenv("ATLAS_LAN", "").strip() == "1":
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    s.connect(("8.8.8.8", 80))
                    lan_ip = s.getsockname()[0]
                    s.close()
                    urls += f"\nhttp://{lan_ip}:{port}"
                except Exception:
                    pass
            embed = discord.Embed(
                title="ATLAS Command Center",
                description=f"**Mode:** {mode}\n**Agents:** {agent_count}\n\n{urls}",
                url=f"http://localhost:{port}",
            )
            await ctx.send(embed=embed)

        @self.bot.command(name="help")
        async def help_command(ctx):
            """Show all launcher commands."""
            await ctx.send(
                "🎮 KalshiBot Launcher Commands\n"
                "!start   — start the trading bot\n"
                "!stop    — stop the trading bot\n"
                "!restart — restart the trading bot\n"
                "!scan    — trigger immediate market scan\n"
                "!pause   — pause trading scans\n"
                "!resume  — resume trading and scan now\n"
                "!status  — check if bot is running\n"
                "!pnl     — show profit/loss summary\n"
                "!balance — show real Kalshi account balance\n"
                "!positions — show open positions with live P&L\n"
                "!syncpositions — sync open Kalshi positions into local DB\n"
                "!auditpositions — audit all open positions, auto-sell if needed\n"
                "!sellposition <ticker> — manually sell a specific open position\n"
                "!metar <station> — show live METAR temperature for a station\n"
                "!calibration — full calibration report (Brier score, CLV, city bias, sigma accuracy)\n"
                "!improvemodel — single most impactful model improvement to make right now\n"
                "!resetstate — sync cash balance and close settled positions\n"
                "!clearhalt — clear permanent halt and reset drawdown to current balance\n"
                "!logs    — show last 30 log lines\n"
                "!logsfull — upload full log file\n"
                "!logssince [hours] — upload recent logs\n"
                "!ps      — show launcher processes\n"
                "!gitstatus — show git status and latest commit\n"
                "!dashboard — ATLAS Command Center URL\n"
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

        required_commands = {
            "start", "stop", "restart", "scan", "pause", "resume", "pnl",
            "status", "balance", "positions", "syncpositions", "auditpositions", "sellposition", "metar",
            "calibration", "improvemodel",
            "resetstate", "clearhalt", "logs", "logsfull",
            "logssince", "ps", "gitstatus", "dashboard", "pocket", "budget", "golive", "help",
        }
        registered = {command.name for command in self.bot.commands}
        missing = sorted(required_commands - registered)
        if missing:
            logging.error("Discord launcher missing commands: %s", missing)
        else:
            logging.info("Discord launcher registered commands: %s", sorted(registered))

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

    def _start_atlas_api(self) -> None:
        """Start Flask ATLAS API in a daemon thread (never blocks bot startup)."""
        port = int(os.getenv("ATLAS_PORT", "5000"))

        def _run():
            try:
                from src.api import run_api
                run_api(host="127.0.0.1", port=port)
            except OSError as exc:
                logging.warning("ATLAS API port %d unavailable — continuing without dashboard: %s", port, exc)
            except Exception as exc:
                logging.warning("ATLAS API failed to start: %s", exc)

        self._api_thread = threading.Thread(target=_run, daemon=True, name="atlas-api")
        self._api_thread.start()

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
            if "[CYCLE] Pipeline started" in line or "Pipeline started" in line:
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

    def _reset_state_from_kalshi(self) -> str:
        """Reconcile SQLite with live Kalshi cash and positions. Returns Discord text."""
        from src.risk_manager import RiskManager

        client = KalshiClient()
        risk = RiskManager(PROJECT_ROOT / "data" / "positions.db")
        report = risk.sync_from_kalshi(client)
        real_balance = float(report["cash"])
        tracking = self._reset_risk_tracking(real_balance)

        lines = [
            "🔄 State Reset Complete",
            f"Real Kalshi balance: ${report['cash']:.2f}",
        ]
        if report["closed"]:
            for item in report["closed"]:
                if item.endswith(")") and "(-$" in item:
                    ticker, rest = item.split(" ", 1)
                    lines.append(f"Positions closed as lost: {ticker} {rest}")
                else:
                    lines.append(f"Positions closed: {item}")
        else:
            lines.append("Positions closed: none")
        lines.append(f"Running budget updated: ${report['running_budget']:.2f}")
        if report["positions_value"] > 0:
            lines.append(f"Open position value: ${report['positions_value']:.2f}")
            lines.append(f"Total portfolio: ${report['total']:.2f}")
        lines.extend(tracking)
        return "\n".join(lines)

    def _clear_halt_and_reset_drawdown(self) -> str:
        """Clear permanent halt and rebaseline drawdown to the live Kalshi balance."""
        client = KalshiClient()
        real_balance = client.get_balance()
        if real_balance is None:
            raise RuntimeError("Could not fetch Kalshi balance")
        was_halted = self._risk_state_value("permanent_halt") == "true"
        tracking = self._reset_risk_tracking(real_balance)
        lines = [
            "✅ Halt Cleared",
            f"Kalshi balance: ${real_balance:.2f}",
            f"Permanent halt was active: {'Yes' if was_halted else 'No'}",
        ]
        lines.extend(tracking)
        return "\n".join(lines)

    def _reset_risk_tracking(self, real_balance: float) -> list[str]:
        """Reset P&L/drawdown risk_state keys for a fresh start at the current balance."""
        balance_str = str(round(real_balance, 2))
        db_path = PROJECT_ROOT / "data" / "positions.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO risk_state (key, value) VALUES ('total_loss_today', '0')"
            )
            conn.execute(
                "INSERT OR REPLACE INTO risk_state (key, value) VALUES ('total_loss_month', '0')"
            )
            conn.execute(
                "INSERT OR REPLACE INTO risk_state (key, value) VALUES ('permanent_halt', '0')"
            )
            conn.execute(
                "INSERT OR REPLACE INTO risk_state (key, value) VALUES ('manual_restart_required', '0')"
            )
            conn.execute(
                "INSERT OR REPLACE INTO risk_state (key, value) VALUES ('peak_balance', ?)",
                (balance_str,),
            )
            conn.execute(
                "INSERT OR REPLACE INTO risk_state (key, value) VALUES ('running_budget', ?)",
                (balance_str,),
            )
            conn.execute("DELETE FROM pnl")
        return [
            "P&L tracking reset for fresh start:",
            "  permanent_halt → cleared",
            "  total_loss_today → $0.00",
            "  total_loss_month → $0.00",
            f"  peak_balance → ${real_balance:.2f}",
            f"  running_budget → ${real_balance:.2f}",
            "  stale pnl records → cleared",
        ]

    def _risk_state_value(self, key: str, default: str = "") -> str:
        """Read a string value from the risk_state table."""
        db_path = PROJECT_ROOT / "data" / "positions.db"
        if not db_path.exists():
            return default
        try:
            with sqlite3.connect(db_path) as conn:
                row = conn.execute("SELECT value FROM risk_state WHERE key = ?", (key,)).fetchone()
            return default if row is None else str(row[0])
        except Exception:
            return default

    def _dynamic_max_positions(self, state: dict) -> int:
        """Estimate max open positions at minimum bet size for current bankroll."""
        import math

        min_bet = float(os.getenv("MIN_BET_USD", "10.0"))
        max_deployment = float(os.getenv("MAX_BANKROLL_DEPLOYMENT", "0.70"))
        ceiling = int(os.getenv("POSITION_COUNT_CEILING", "6"))
        bankroll = float(state.get("running_budget") or state.get("todays_budget") or 0.0)
        if min_bet <= 0 or bankroll <= 0:
            return 1
        dynamic_max = math.floor((max_deployment * bankroll) / min_bet)
        return max(1, min(ceiling, dynamic_max))

    def _open_positions_report(self) -> str:
        """Build a Discord-friendly report of live open positions with P&L."""
        client = KalshiClient()
        positions = client.get_positions()
        rows = []
        for pos in positions:
            try:
                contracts_signed = int(float(pos.get("position_fp") or 0))
            except (TypeError, ValueError):
                contracts_signed = 0
            if contracts_signed == 0:
                continue
            rows.append((pos, contracts_signed))

        state = self._db_status()
        max_positions = self._dynamic_max_positions(state)
        if not rows:
            return f"📊 Open Positions (0/{max_positions})\nNo open positions on Kalshi."

        lines = [f"📊 Open Positions ({len(rows)}/{max_positions})"]
        for pos, contracts_signed in rows:
            ticker = str(pos.get("ticker", ""))
            side = "YES" if contracts_signed > 0 else "NO"
            contracts = abs(contracts_signed)
            try:
                cost = abs(float(pos.get("market_exposure_dollars") or 0.0))
            except (TypeError, ValueError):
                cost = 0.0
            avg_price = cost / contracts if contracts else 0.0
            payout = contracts * 1.0

            # Current mark: what we could sell the held side for right now.
            market = client.get_market(ticker)
            current_bid = None
            if market is not None:
                current_bid = market.yes_bid if side == "YES" else market.no_bid
            if current_bid is not None:
                current_value = contracts * current_bid
                pnl = current_value - cost
                pct = (pnl / cost * 100) if cost else 0.0
                pnl_str = f"P&L: {'-' if pnl < 0 else '+'}${abs(pnl):.2f} ({pct:+.0f}%)"
            else:
                pnl_str = "P&L: n/a"

            lines.append(
                f"{ticker} | {side} | {contracts} contracts @ ${avg_price:.2f} | "
                f"Cost: ${cost:.2f} | Payout: ${payout:.2f} | {pnl_str}"
            )

        balance = client.get_balance()
        if balance is not None:
            lines.append(f"Cash remaining: ${balance:.2f}")
        return "\n".join(lines)

    def _sync_positions_from_kalshi(self):
        """Pull non-zero positions from Kalshi and upsert them into SQLite.

        Returns (count_synced, summary_lines). Idempotent on ticker: an existing
        row is updated to reflect the live contract count/stake and reopened.
        """
        client = KalshiClient()
        positions = client.get_positions()
        db_path = PROJECT_ROOT / "data" / "positions.db"
        synced = 0
        lines = []
        with sqlite3.connect(db_path) as conn:
            for pos in positions:
                try:
                    contracts_signed = int(float(pos.get("position_fp") or 0))
                except (TypeError, ValueError):
                    contracts_signed = 0
                if contracts_signed == 0:
                    continue
                ticker = str(pos.get("ticker", ""))
                if not ticker:
                    continue
                side = "yes" if contracts_signed > 0 else "no"
                contracts = abs(contracts_signed)
                try:
                    stake = abs(float(pos.get("market_exposure_dollars") or 0.0))
                except (TypeError, ValueError):
                    stake = 0.0
                price = round(stake / contracts, 4) if contracts else 0.0
                city = ticker.split("-", 1)[0] or "synced"
                now = datetime.now(tz=zoneinfo.ZoneInfo("UTC")).isoformat()
                conn.execute(
                    """
                    INSERT INTO positions
                    (ticker, city, side, contracts, price, stake, status, dry_run, order_id, opened_at, closed_at, realized_pnl)
                    VALUES (?, ?, ?, ?, ?, ?, 'open', 0, NULL, ?, NULL, 0)
                    ON CONFLICT(ticker) DO UPDATE SET
                        side=excluded.side,
                        contracts=excluded.contracts,
                        price=excluded.price,
                        stake=excluded.stake,
                        status='open',
                        closed_at=NULL
                    """,
                    (ticker, city, side, contracts, price, stake, now),
                )
                synced += 1
                lines.append(f"{ticker} {side.upper()} {contracts} @ ${price:.2f} (${stake:.2f})")
        return synced, lines

    def _get_open_positions(self) -> list[dict]:
        """Read open live positions from SQLite."""
        from src.risk_manager import RiskManager

        return RiskManager(PROJECT_ROOT / "data" / "positions.db").get_open_live_positions()

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
        open_positions = int(self._scalar("SELECT COUNT(*) FROM positions WHERE status = 'open' AND dry_run = 0", 0.0))
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

"""Full trading pipeline for the Kalshi weather bot."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import colorlog
import requests

from src.claude_checker import ClaudeChecker, ClaudeDecision
from src.data_enricher import DataEnricher
from src.edge_engine import EdgeDecision, EdgeEngine
from src.kalshi_client import KalshiClient, KalshiMarket
from src.position_sizer import PositionSize, PositionSizer
from src.risk_manager import RiskManager
from src.weather_client import CityConfig, EnsembleForecast, NwsForecast, WeatherClient


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR = PROJECT_ROOT / "logs"
CYCLE_LEVEL = 25
BET_LEVEL = 26


def env_bool(name: str, default: bool) -> bool:
    """Read an environment variable as a boolean value."""
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def configure_logging() -> None:
    """Configure colorized console logs and the persistent bot log file."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.addLevelName(CYCLE_LEVEL, "CYCLE")
    logging.addLevelName(BET_LEVEL, "BET")
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)

    console = colorlog.StreamHandler()
    console.setFormatter(
        colorlog.ColoredFormatter(
            "%(log_color)s%(asctime)s %(levelname)-8s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
            log_colors={
                "DEBUG": "white",
                "INFO": "green",
                "CYCLE": "cyan",
                "BET": "green",
                "WARNING": "yellow",
                "ERROR": "red",
                "CRITICAL": "bold_red",
            },
            secondary_log_colors={},
        )
    )

    file_handler = logging.FileHandler(LOG_DIR / "bot.log", encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(message)s"))
    root.addHandler(console)
    root.addHandler(file_handler)


class Trader:
    """Coordinate the complete scan, filter, size, and order workflow."""

    def __init__(self) -> None:
        """Build the API clients, strategy helpers, and data directories."""
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.dry_run = env_bool("DRY_RUN", True)
        self.paused = False
        self.last_scan_time: datetime | None = None
        self.pnl_path = DATA_DIR / "pnl.json"
        self.kalshi = KalshiClient()
        self.weather = WeatherClient()
        self.edge_engine = EdgeEngine()
        self.claude = ClaudeChecker()
        self.enricher = DataEnricher()
        self.position_sizer = PositionSizer()
        self.risk = RiskManager(DATA_DIR / "positions.db")
        self._scan_bet_count = 0
        self._scan_skip_count = 0
        self._ensure_pnl_file()

    def run_full_pipeline(self) -> None:
        """Run one full market scan from Kalshi through order/dry-run action."""
        if self.paused:
            logging.info("[CYCLE] Bot is paused — skipping scan.")
            return

        self.last_scan_time = datetime.now()
        self._scan_bet_count = 0
        self._scan_skip_count = 0
        logging.getLogger().log(CYCLE_LEVEL, "[CYCLE] Full pipeline started. dry_run=%s", self.dry_run)
        self._send_discord(f"🤖 Bot started scan. DRY_RUN={self.dry_run}")
        for city in self.weather.watched_cities():
            for series_ticker in (city.high_series, city.low_series):
                self._scan_series(city, series_ticker)
        logging.getLogger().log(CYCLE_LEVEL, "[CYCLE] Full pipeline finished.")
        self._send_discord(f"✅ Scan complete. {self._scan_bet_count} bets placed, {self._scan_skip_count} skipped.")

    def _scan_series(self, city: CityConfig, series_ticker: str) -> None:
        """Fetch and process every open market in one city series."""
        try:
            markets = self.kalshi.list_markets(series_ticker)
        except requests.RequestException as exc:
            logging.error("[ERROR] %s | Kalshi fetch failed for %s: %s", city.short_code, series_ticker, exc)
            self._scan_skip_count += 1
            self._send_discord(f"BOT ERROR\nCity: {city.name}\nKalshi fetch failed: {exc}")
            return

        if not markets:
            logging.info("[SKIP] %s | No open markets for %s", city.short_code, series_ticker)
            self._scan_skip_count += 1
            return

        for market in markets:
            self._process_market(city, market)

    def _process_market(self, city: CityConfig, market: KalshiMarket) -> None:
        """Handle a single market without letting failures stop the scan."""
        try:
            self._process_market_inner(city, market)
        except Exception as exc:
            logging.exception("[ERROR] %s | Market %s failed: %s", city.short_code, market.ticker, exc)
            self.risk.record_decision(
                ticker=market.ticker,
                city=city.name,
                decision="ERROR",
                reason=str(exc),
                edge=None,
                confidence=None,
                market_price=None,
            )
            self._scan_skip_count += 1
            self._send_discord(f"BOT ERROR\nMarket: {market.ticker}\nReason: {exc}")

    def _process_market_inner(self, city: CityConfig, market: KalshiMarket) -> None:
        """Run data fetches, filters, Claude, risk checks, and order handling."""
        settlement_time = market.settlement_time or market.close_time
        if settlement_time is None:
            self._log_skip(city, market, "No settlement time available.", None)
            return

        market_type = self.edge_engine.market_type(market)
        target_date = settlement_time.astimezone(timezone.utc).date()

        try:
            gfs = self.weather.get_ensemble_forecast(city, "gfs", target_date, market_type)
            ecmwf = self.weather.get_ensemble_forecast(city, "ecmwf", target_date, market_type)
            nws = self.weather.get_nws_forecast(city, target_date, market_type)
        except requests.RequestException as exc:
            self._log_skip(city, market, f"Weather API fallback skip: {exc}", None)
            return

        edge_decision = self.edge_engine.evaluate(market, gfs=gfs, ecmwf=ecmwf, nws=nws)
        if not edge_decision.should_trade:
            self._log_skip(city, market, edge_decision.reason, edge_decision)
            return

        enrichment = self.enricher.enrich(city, target_date.isoformat())
        claude_payload = self._claude_payload(city, market, edge_decision, gfs, ecmwf, nws, enrichment)
        claude_decision = self.claude.check(claude_payload)
        logging.info("Claude market=%s decision=%s reason=%s", market.ticker, claude_decision.decision, claude_decision.reason)
        if not claude_decision.approved:
            self._log_skip(city, market, f"Claude NOGO: {claude_decision.reason}", edge_decision)
            return

        last_won, previous_payout = self.risk.last_trade_state()
        size = self.position_sizer.size_trade(
            win_probability=edge_decision.model_probability or 0.0,
            price=edge_decision.limit_price or edge_decision.ask_price or 0.0,
            confidence=edge_decision.confidence,
            previous_payout=previous_payout,
            last_trade_won=last_won,
        )
        if size.contracts < 1:
            self._log_skip(city, market, size.reason, edge_decision)
            return

        risk_check = self.risk.can_trade(market.ticker, size.stake)
        if not risk_check.allowed:
            self._log_skip(city, market, risk_check.reason, edge_decision)
            if risk_check.alert:
                self._send_risk_alert(risk_check.reason)
            return

        if self.dry_run:
            self._dry_run_bet(city, market, edge_decision, claude_decision, size)
        else:
            self._live_bet(city, market, edge_decision, claude_decision, size)

    def _dry_run_bet(
        self,
        city: CityConfig,
        market: KalshiMarket,
        edge: EdgeDecision,
        claude: ClaudeDecision,
        size: PositionSize,
    ) -> None:
        """Record a simulated bet without calling Kalshi's order endpoint."""
        logging.getLogger().log(
            BET_LEVEL,
            "[DRY RUN] WOULD BET $%.2f on %s @ $%.2f | %s %.0fF | Edge: %.1f%% | Confidence: %.0f%% | Claude: %s",
            size.stake,
            (edge.side or "").upper(),
            edge.limit_price or 0.0,
            city.short_code,
            edge.threshold_f or 0.0,
            (edge.edge or 0.0) * 100,
            edge.confidence * 100,
            claude.decision,
        )
        self.risk.record_open_position(
            ticker=market.ticker,
            city=city.name,
            side=edge.side or "unknown",
            contracts=size.contracts,
            price=edge.limit_price or 0.0,
            stake=size.stake,
            dry_run=True,
            order_id="dry-run",
        )
        self.risk.record_decision(
            ticker=market.ticker,
            city=city.name,
            decision="DRY_RUN_BET",
            reason=claude.reason,
            edge=edge.edge,
            confidence=edge.confidence,
            market_price=edge.ask_price,
        )
        self._scan_bet_count += 1
        self._append_pnl_position(city, market, edge, size, dry_run=True)
        self._send_bet_alert(city, edge, size, dry_run=True)

    def _live_bet(
        self,
        city: CityConfig,
        market: KalshiMarket,
        edge: EdgeDecision,
        claude: ClaudeDecision,
        size: PositionSize,
    ) -> None:
        """Place a real Kalshi limit order after every guardrail passes."""
        if not self.kalshi.has_credentials:
            self._log_skip(city, market, "Kalshi credentials missing for live trading.", edge)
            return

        try:
            response = self.kalshi.place_limit_order(
                ticker=market.ticker,
                side=edge.side or "yes",
                count=size.contracts,
                limit_price=edge.limit_price or 0.0,
            )
        except requests.RequestException as exc:
            self._log_skip(city, market, f"Kalshi limit order failed: {exc}", edge)
            return

        order_id = str(response.get("order", {}).get("order_id") or response.get("order_id") or "")
        logging.getLogger().log(
            BET_LEVEL,
            "[BET] %s %.0fF | $%.2f on %s @ $%.2f | Edge: %.1f%% | Kelly: $%.2f | Claude: GO -- %s",
            city.short_code,
            edge.threshold_f or 0.0,
            size.stake,
            (edge.side or "").upper(),
            edge.limit_price or 0.0,
            (edge.edge or 0.0) * 100,
            size.kelly_size,
            claude.reason,
        )
        self.risk.record_open_position(
            ticker=market.ticker,
            city=city.name,
            side=edge.side or "unknown",
            contracts=size.contracts,
            price=edge.limit_price or 0.0,
            stake=size.stake,
            dry_run=False,
            order_id=order_id,
        )
        self.risk.record_decision(
            ticker=market.ticker,
            city=city.name,
            decision="LIVE_BET",
            reason=claude.reason,
            edge=edge.edge,
            confidence=edge.confidence,
            market_price=edge.ask_price,
        )
        self._scan_bet_count += 1
        self._append_pnl_position(city, market, edge, size, dry_run=False)
        self._send_bet_alert(city, edge, size, dry_run=False)

    def _log_skip(
        self,
        city: CityConfig,
        market: KalshiMarket,
        reason: str,
        edge: EdgeDecision | None,
    ) -> None:
        """Log a skipped market in the required format and persist it."""
        threshold = edge.threshold_f if edge else None
        market_price = edge.ask_price if edge else market.yes_ask
        edge_value = edge.edge if edge else None
        logging.warning(
            "[SKIP] %s %s | Edge: %s | Market: %s | Reason: %s",
            city.short_code,
            f"{threshold:.0f}F" if threshold is not None else "n/a",
            f"{edge_value:.1%}" if edge_value is not None else "n/a",
            f"${market_price:.2f}" if market_price is not None else "n/a",
            reason,
        )
        self._scan_skip_count += 1
        self.risk.record_decision(
            ticker=market.ticker,
            city=city.name,
            decision="SKIP",
            reason=reason,
            edge=edge_value,
            confidence=edge.confidence if edge else None,
            market_price=market_price,
        )

    def _claude_payload(
        self,
        city: CityConfig,
        market: KalshiMarket,
        edge: EdgeDecision,
        gfs: EnsembleForecast,
        ecmwf: EnsembleForecast,
        nws: NwsForecast,
        enrichment: dict[str, Any],
    ) -> dict[str, Any]:
        """Build the full data payload required by the Claude checker."""
        settlement = market.settlement_time or market.close_time
        return {
            "city": city.name,
            "date": settlement.isoformat() if settlement else None,
            "temperature_threshold_f": edge.threshold_f,
            "side": edge.side,
            "gfs_probability": gfs.member_temperatures_f and edge.gfs_probability_yes,
            "ecmwf_probability": ecmwf.member_temperatures_f and edge.ecmwf_probability_yes,
            "gfs_members": gfs.member_count,
            "ecmwf_members": ecmwf.member_count,
            "nws_forecast_f": nws.temperature_f,
            "nws_adjusted_forecast_f": edge.nws_adjusted_temperature_f,
            "nws_short_forecast": nws.short_forecast,
            "market_price": edge.ask_price,
            "limit_price": edge.limit_price,
            "edge_percentage": None if edge.edge is None else round(edge.edge * 100, 2),
            "confidence_score": round(edge.confidence * 100, 2),
            "hours_until_settlement": edge.hours_until_settlement,
            "active_weather_alerts": enrichment.get("active_alerts", []),
            "has_severe_alert": enrichment.get("has_severe_alert", False),
            "current_temp_f": enrichment.get("current_temp_f"),
            "current_observation": enrichment.get("current_observation", {}),
            "web_context": enrichment.get("web_context", ""),
        }

    def _append_pnl_position(
        self,
        city: CityConfig,
        market: KalshiMarket,
        edge: EdgeDecision,
        size: PositionSize,
        *,
        dry_run: bool,
    ) -> None:
        """Track simulated/live open position data in data/pnl.json."""
        pnl = self._read_pnl()
        pnl.setdefault("realized_pnl", 0.0)
        pnl.setdefault("open_positions", [])
        pnl["open_positions"].append(
            {
                "opened_at": datetime.now(timezone.utc).isoformat(),
                "dry_run": dry_run,
                "ticker": market.ticker,
                "city": city.name,
                "side": edge.side,
                "contracts": size.contracts,
                "stake": size.stake,
                "price": edge.limit_price,
                "edge": edge.edge,
                "confidence": edge.confidence,
            }
        )
        pnl["open_risk"] = round(sum(float(item.get("stake", 0.0)) for item in pnl["open_positions"]), 2)
        self.pnl_path.write_text(json.dumps(pnl, indent=2), encoding="utf-8")

    def _ensure_pnl_file(self) -> None:
        """Create data/pnl.json if it does not already exist."""
        if not self.pnl_path.exists():
            self.pnl_path.write_text(
                json.dumps({"realized_pnl": 0.0, "open_risk": 0.0, "open_positions": []}, indent=2),
                encoding="utf-8",
            )

    def _read_pnl(self) -> dict[str, Any]:
        """Read data/pnl.json with a graceful fallback for bad JSON."""
        try:
            return json.loads(self.pnl_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {"realized_pnl": 0.0, "open_risk": 0.0, "open_positions": []}

    def _send_bet_alert(self, city: CityConfig, edge: EdgeDecision, size: PositionSize, *, dry_run: bool) -> None:
        """Send a readable Discord alert for a placed or simulated bet."""
        prefix = "🧪 DRY RUN" if dry_run else "💰 BET PLACED"
        self._send_discord(
            f"{prefix}\n"
            f"City: {city.name}\n"
            f"Bet: {(edge.side or '').upper()} ${size.stake:.2f} @ ${edge.limit_price:.2f}\n"
            f"Edge: {(edge.edge or 0.0):.1%}\n"
            f"Confidence: {edge.confidence:.1%}\n"
            f"Contracts: {size.contracts}\n"
            f"Profit if win: ${size.contracts * (1 - (edge.limit_price or 0)):.2f}\n"
            f"Settles in: {edge.hours_until_settlement:.1f} hours"
        )

    def _send_risk_alert(self, reason: str) -> None:
        """Send a Discord alert for hard risk-stop events."""
        if reason.startswith("Daily loss limit hit"):
            daily_loss = abs(min(0.0, self.risk.realized_pnl_today()))
            self._send_discord(f"⛔ DAILY LOSS LIMIT HIT\nBot stopped trading for today.\nLoss: ${daily_loss:.2f}")
        elif "Permanent halt" in reason or "Max drawdown" in reason:
            self._send_discord("🚨 PERMANENT HALT\nMax drawdown reached. Manual restart required.")
        else:
            self._send_discord(f"RISK HALT\nReason: {reason}")

    def send_win_alert(self, city: str, side: str, profit: float) -> None:
        """Send a Discord win alert when a settlement process records a win."""
        running_pnl = self.risk.current_balance() - self.risk.starting_balance
        self._send_discord(f"✅ WIN +${profit:.2f}\n{city} resolved {side}\nRunning P&L: ${running_pnl:.2f}")

    def _send_discord(self, message: str) -> None:
        """Send an alert to a private Discord channel."""
        token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
        channel_id = os.getenv("DISCORD_CHANNEL_ID", "").strip()
        if not token or not channel_id or token == "your_token_here":
            return
        try:
            url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
            headers = {
                "Authorization": f"Bot {token}",
                "Content-Type": "application/json",
            }
            # Discord has a 2000 character limit per message
            chunks = [message[i:i+1900] for i in range(0, len(message), 1900)]
            for chunk in chunks:
                response = requests.post(
                    url,
                    headers=headers,
                    json={"content": f"```\n{chunk}\n```"},
                    timeout=10,
                )
                response.raise_for_status()
        except requests.RequestException as exc:
            logging.warning("Discord alert failed: %s", exc)

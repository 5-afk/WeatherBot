"""Full trading pipeline for the Kalshi weather bot."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import colorlog
import requests

from src.bot_control import is_paused
from src.claude_checker import ClaudeChecker, ClaudeDecision
from src.edge_engine import EdgeDecision, EdgeEngine
from src.kalshi_client import KalshiClient, KalshiMarket
from src.position_sizer import PositionSize, PositionSizer
from src.risk_manager import RiskManager
from src.weather_client import CityConfig, NwsForecast, WeatherClient


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
        self.position_sizer = PositionSizer()
        self.risk = RiskManager(DATA_DIR / "positions.db")
        self._claude_calls_today = 0
        self._claude_call_date = datetime.now(timezone.utc).date()
        self._scan_lock = threading.Lock()
        # Always sync real balance from Kalshi on startup
        real_balance = self.kalshi.get_balance()
        if real_balance and real_balance > 0:
            self.risk._set_state("running_budget", str(real_balance))
            logging.info("Kalshi balance synced: $%.2f", real_balance)
        else:
            logging.warning("Could not fetch Kalshi balance — keeping local budget")
        self._ensure_pnl_file()

    def run_full_pipeline(self) -> None:
        """Run one full scan: collect markets, evaluate math, call Claude once, trade."""
        self.paused = is_paused()
        if self.paused:
            logging.info("[CYCLE] Bot is paused — skipping scan.")
            return

        self.last_scan_time = datetime.now(timezone.utc)
        logging.getLogger().log(CYCLE_LEVEL, "[CYCLE] Pipeline started. dry_run=%s", self.dry_run)

        self._scan_bets = 0
        self._scan_skips = 0
        self._scan_signals = 0
        self._weather_cache: dict[tuple[str, str, str], NwsForecast] = {}
        self._observation_cache: dict[str, float | None] = {}

        if not self.dry_run:
            real_balance = self.kalshi.get_balance()
            if real_balance is None:
                real_balance = float(self.risk._get_state("running_budget") or 100)
                logging.warning("Balance fetch failed — using last known: $%.2f", real_balance)
            if real_balance < 1.0:
                logging.warning("Balance confirmed too low: $%.2f", real_balance)
                self._send_discord(f"⛔ Balance too low: ${real_balance:.2f}")
                return
            self.risk._set_state("running_budget", str(real_balance))
            logging.info("Balance: $%.2f | running_budget synced from Kalshi", real_balance)
        else:
            real_balance = None

        today = datetime.now(timezone.utc).date()
        if today != self._claude_call_date:
            self._claude_calls_today = 0
            self._claude_call_date = today
        max_calls = int(os.getenv("MAX_CLAUDE_CALLS_PER_DAY", "5"))
        if self._claude_calls_today >= max_calls:
            logging.info("Daily Claude limit reached — math-only scan")

        market_rows: list[tuple[CityConfig, KalshiMarket, float]] = []
        city_count = len(self.weather.watched_cities())
        for city in self.weather.watched_cities():
            for series_ticker in (city.high_series, city.low_series, city.lowt_series):
                try:
                    markets = self.kalshi.list_markets(series_ticker)
                except Exception as exc:
                    logging.error("Fetch failed %s: %s", series_ticker, exc)
                    continue
                ladder_multiplier = self.edge_engine.calculate_ladder_sum(
                    [market.yes_ask for market in markets if market.yes_ask is not None]
                )
                market_rows.extend((city, market, ladder_multiplier) for market in markets)

        candidates: list[tuple[CityConfig, KalshiMarket, EdgeDecision]] = []
        total_checked = len(market_rows)
        candidates = self._evaluate_all_markets(market_rows)
        for city, market, edge in candidates:
            logging.info(
                "[CANDIDATE] %s %.0fF %s | Edge: %.0f%% | EV: %.2f | Signal: %.2f | Imbalance: %.2f",
                city.short_code,
                edge.threshold_f or 0.0,
                (edge.side or "").upper(),
                (edge.edge or 0.0) * 100,
                edge.ev or 0.0,
                edge.signal_score,
                edge.imbalance_score,
            )

        logging.info(
            "[CYCLE] Checked %d markets, %d candidates (%d cities x ~%d markets each)",
            total_checked,
            len(candidates),
            city_count,
            total_checked // city_count if city_count else 0,
        )
        if not candidates:
            logging.info("[CYCLE] No qualifying candidates — scan complete")
            self._maybe_record_day_end()
            self._send_scan_summary(real_balance, total_checked, 0)
            return

        if self._claude_calls_today >= max_calls:
            best_city, best_market, best_edge = candidates[0]
            logging.info("No Claude available — using top math signal")
            self._execute_trade(
                best_city,
                best_market,
                best_edge,
                ClaudeDecision("GO", "Math-only: Claude limit reached"),
                real_balance,
            )
            self._maybe_record_day_end()
            self._send_scan_summary(real_balance, total_checked, len(candidates))
            return

        batch = [self._batch_candidate_payload(city, market, edge) for city, market, edge in candidates[:5]]
        self._claude_calls_today += 1
        logging.info(
            "Claude batch call %d/%d — evaluating %d candidates",
            self._claude_calls_today,
            max_calls,
            len(batch),
        )
        self._scan_signals = getattr(self, "_scan_signals", 0) + 1
        claude_decision = self.claude.check_batch(batch, balance=real_balance)
        logging.info("Claude decision=%s reason=%s", claude_decision.decision, claude_decision.reason)
        if not claude_decision.approved:
            logging.warning("[SKIP] Claude rejected all candidates: %s", claude_decision.reason)
            self._maybe_record_day_end()
            self._send_scan_summary(real_balance, total_checked, len(candidates))
            return

        best_city, best_market, best_edge = self._select_candidate(candidates, claude_decision.ticker)
        self._execute_trade(best_city, best_market, best_edge, claude_decision, real_balance)
        self._maybe_record_day_end()
        self._send_scan_summary(real_balance, total_checked, len(candidates))

    def _evaluate_all_markets(
        self,
        market_rows: list[tuple[CityConfig, KalshiMarket, float]],
    ) -> list[tuple[CityConfig, KalshiMarket, EdgeDecision]]:
        """Evaluate markets in parallel and return candidates sorted by signal score."""
        results: list[tuple[CityConfig, KalshiMarket, EdgeDecision]] = []
        max_workers = int(os.getenv("MAX_SCAN_WORKERS", "10"))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(self._math_evaluate, city, market, ladder): market
                for city, market, ladder in market_rows
            }
            for future in as_completed(futures):
                market = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    logging.error("Eval failed %s: %s", market.ticker, exc)
                    continue
                if result is not None:
                    results.append(result)
        results.sort(key=lambda item: item[2].signal_score, reverse=True)
        return results

    def _math_evaluate(
        self,
        city: CityConfig,
        market: KalshiMarket,
        ladder_multiplier: float = 1.0,
    ) -> tuple[CityConfig, KalshiMarket, EdgeDecision] | None:
        """Run numeric/weather evaluation only; never call Claude or place orders."""
        settlement_time = market.settlement_time or market.close_time
        if settlement_time is None:
            self._log_skip(city, market, "No settlement time available.", None)
            return None

        market_type = self.edge_engine.market_type(market)
        target_date = settlement_time.astimezone(timezone.utc).date()
        cache_key = (city.name, target_date.isoformat(), market_type)
        try:
            with self._scan_lock:
                cached_weather = self._weather_cache.get(cache_key)
            if cached_weather is None:
                nws = self.weather.get_nws_gridded_forecast(city, target_date, market_type)
                with self._scan_lock:
                    if cache_key not in self._weather_cache:
                        self._weather_cache[cache_key] = nws
                    nws = self._weather_cache[cache_key]
            else:
                nws = cached_weather
        except Exception as exc:
            logging.debug("Weather fetch failed %s: %s", market.ticker, exc)
            self._log_skip(city, market, f"Weather fetch failed: {exc}", None)
            return None

        current_temp_f = self._current_temp_f(city)
        imbalance_score = self._fetch_imbalance(market)
        edge = self.edge_engine.evaluate(
            market,
            nws=nws,
            current_temp_f=current_temp_f,
            ladder_multiplier=ladder_multiplier,
            imbalance_score=imbalance_score,
        )
        if not edge.should_trade:
            if edge.reason.startswith("Pre-Claude gate failed"):
                logging.warning("[SKIP] %s", edge.reason)
            self._log_skip(city, market, edge.reason, edge)
            return None
        return city, market, edge

    def _execute_trade(
        self,
        city: CityConfig,
        market: KalshiMarket,
        edge: EdgeDecision,
        claude: ClaudeDecision,
        real_balance: float | None,
    ) -> None:
        """Size and place the bet after the single batch Claude decision."""
        del real_balance
        current_budget = self.risk.get_todays_budget()
        if current_budget < 1.0:
            logging.warning("No budget remaining")
            self._log_skip(city, market, f"No budget remaining today (${current_budget:.2f})", edge)
            return

        last_won, previous_payout = self.risk.last_trade_state()
        size = self.position_sizer.size_trade(
            win_probability=min(edge.model_probability or 0.99, 0.99),
            price=edge.limit_price or edge.ask_price or 0.0,
            confidence=edge.confidence,
            current_budget=current_budget,
            ladder_multiplier=edge.ladder_multiplier,
            previous_payout=previous_payout,
            last_trade_won=last_won,
        )
        if size.contracts < 1:
            logging.warning("[SKIP] Stake too small: %s", size.reason)
            self._log_skip(city, market, size.reason, edge)
            return

        risk_check = self.risk.can_trade(market.ticker, size.stake)
        if not risk_check.allowed:
            logging.warning("[SKIP] Risk check failed: %s", risk_check.reason)
            self._log_skip(city, market, risk_check.reason, edge)
            if risk_check.alert:
                self._send_risk_alert(risk_check.reason)
            return

        if self.dry_run:
            self._dry_run_bet(city, market, edge, claude, size)
        else:
            self._live_bet(city, market, edge, claude, size)

    def _fetch_imbalance(self, market: KalshiMarket) -> float:
        """Fetch order book imbalance; return neutral 0.5 on failure."""
        yes_ask = market.yes_ask
        no_ask = market.no_ask
        if yes_ask is None and no_ask is None:
            return 0.5
        try:
            book = self.kalshi.get_orderbook(market.ticker)
            return float(book.get("imbalance_score", 0.5))
        except Exception:
            return 0.5

    def _batch_candidate_payload(
        self,
        city: CityConfig,
        market: KalshiMarket,
        edge: EdgeDecision,
    ) -> dict[str, Any]:
        """Build one candidate row for the single Claude batch call."""
        settlement = market.settlement_time or market.close_time
        return {
            "ticker": market.ticker,
            "city": city.name,
            "side": edge.side,
            "market_type": self.edge_engine.market_type_label(market),
            "threshold_f": edge.threshold_f,
            "market_price": edge.ask_price,
            "edge_pct": round((edge.edge or 0.0) * 100, 1),
            "expected_value_per_contract": edge.ev,
            "signal_score": edge.signal_score,
            "buffer_score": edge.buffer_score,
            "observation_score": edge.observation_score,
            "imbalance_score": edge.imbalance_score,
            "confidence": round(edge.confidence * 100, 1),
            "nws_probability": edge.nws_probability_yes,
            "nws_forecast_f": edge.nws_forecast_temperature_f,
            "hours_until_settlement": edge.hours_until_settlement,
            "settlement": settlement.isoformat() if settlement else None,
        }

    def _select_candidate(
        self,
        candidates: list[tuple[CityConfig, KalshiMarket, EdgeDecision]],
        ticker: str | None,
    ) -> tuple[CityConfig, KalshiMarket, EdgeDecision]:
        """Use Claude's ticker choice when present; otherwise use top signal score."""
        if ticker:
            wanted = ticker.upper()
            for candidate in candidates:
                if candidate[1].ticker.upper() == wanted:
                    return candidate
            logging.warning("Claude selected unknown ticker %s — using top signal", ticker)
        return candidates[0]

    def _current_temp_f(self, city: CityConfig) -> float | None:
        """Fetch latest station temperature from NWS and convert Celsius to Fahrenheit."""
        with self._scan_lock:
            if city.name in self._observation_cache:
                return self._observation_cache[city.name]
        observation = self.weather.latest_station_observation(city)
        if not observation:
            with self._scan_lock:
                self._observation_cache[city.name] = None
            return None
        value = (
            observation.get("properties", {})
            .get("temperature", {})
            .get("value")
        )
        try:
            celsius = float(value)
        except (TypeError, ValueError):
            with self._scan_lock:
                self._observation_cache[city.name] = None
            return None
        current_temp = round(celsius * 9 / 5 + 32, 2)
        with self._scan_lock:
            self._observation_cache[city.name] = current_temp
        return current_temp

    def _send_scan_summary(self, balance: float | None, total_checked: int, candidate_count: int) -> None:
        """Post the end-of-scan summary to Discord."""
        balance_str = f"${balance:.2f}" if balance else "n/a"
        mode = "🧪 DRY RUN" if self.dry_run else "💰 LIVE"
        now = datetime.now(timezone.utc).strftime("%H:%M UTC")
        if self._scan_bets > 0:
            summary = (
                f"📊 Scan complete [{now}] | {mode}\n"
                f"✅ Bets placed: {self._scan_bets}\n"
                f"🔎 Markets checked: {total_checked}\n"
                f"🎯 Candidates: {candidate_count}\n"
                f"💰 Balance: {balance_str}"
            )
        else:
            summary = (
                f"📊 Scan complete [{now}] | {mode}\n"
                f"🔍 No bet placed\n"
                f"🔎 Markets checked: {total_checked}\n"
                f"🎯 Candidates: {candidate_count}\n"
                f"💰 Balance: {balance_str}"
            )
        self._send_discord(summary)

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
            "[DRY RUN] %s %.0fF | Edge: %.1f%% | EV: %.4f | Signal: %.2f | Buffer: %s | $%.2f on %s @ $%.2f | Claude: %s",
            city.short_code,
            edge.threshold_f or 0.0,
            (edge.edge or 0.0) * 100,
            edge.ev or 0.0,
            edge.signal_score,
            self._buffer_text(edge),
            size.stake,
            (edge.side or "").upper(),
            edge.limit_price or 0.0,
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
        self._scan_bets = getattr(self, "_scan_bets", 0) + 1
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
            if not self.kalshi.is_market_open(market.ticker):
                self._log_skip(city, market, "Market already settled — skipping order", edge)
                return
        except requests.RequestException as exc:
            self._log_skip(city, market, f"Market status check failed: {exc}", edge)
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
        if not order_id:
            self._log_skip(city, market, "Kalshi order placed but no order_id returned.", edge)
            return

        filled_count = 0
        for _attempt in range(6):
            time.sleep(5)
            status = self.kalshi.get_order_status(order_id)
            if status["status"] == "filled":
                filled_count = status["filled_count"]
                logging.info(
                    "[BET CONFIRMED] %s %s | %d contracts filled",
                    market.ticker,
                    edge.side,
                    filled_count,
                )
                break
            if status["status"] == "canceled":
                logging.warning("[BET CANCELED] %s order was canceled before filling", market.ticker)
                return

        if filled_count < 1:
            self.kalshi.cancel_order(order_id)
            logging.warning("[BET TIMEOUT] %s order not filled in 30s — canceled", market.ticker)
            return

        actual_stake = round(filled_count * (edge.limit_price or 0.0), 2)
        logging.getLogger().log(
            BET_LEVEL,
            "[BET] %s %.0fF | Edge: %.1f%% | EV: %.4f | Signal: %.2f | Buffer: %s | $%.2f on %s @ $%.2f | Kelly: $%.2f | Claude: GO -- %s",
            city.short_code,
            edge.threshold_f or 0.0,
            (edge.edge or 0.0) * 100,
            edge.ev or 0.0,
            edge.signal_score,
            self._buffer_text(edge),
            actual_stake,
            (edge.side or "").upper(),
            edge.limit_price or 0.0,
            size.kelly_size,
            claude.reason,
        )
        self.risk.record_open_position(
            ticker=market.ticker,
            city=city.name,
            side=edge.side or "unknown",
            contracts=filled_count,
            price=edge.limit_price or 0.0,
            stake=actual_stake,
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
        self._scan_bets = getattr(self, "_scan_bets", 0) + 1
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
            "[SKIP] %s %s %s | Edge: %s | EV: %s | Signal: %.2f | Imbalance: %.2f | Buffer: %s | Market: %s | Reason: %s",
            city.short_code,
            f"{threshold:.0f}F" if threshold is not None else "n/a",
            (edge.side or "").upper() if edge and edge.side else "",
            f"{edge_value:.1%}" if edge_value is not None else "n/a",
            f"{edge.ev:.4f}" if edge and edge.ev is not None else "n/a",
            edge.signal_score if edge else 0.5,
            edge.imbalance_score if edge else 0.5,
            self._buffer_text(edge),
            f"${market_price:.2f}" if market_price is not None else "n/a",
            reason,
        )
        self._scan_skips = getattr(self, "_scan_skips", 0) + 1
        self.risk.record_decision(
            ticker=market.ticker,
            city=city.name,
            decision="SKIP",
            reason=reason,
            edge=edge_value,
            confidence=edge.confidence if edge else None,
            market_price=market_price,
        )

    def _buffer_text(self, edge: EdgeDecision | None) -> str:
        """Format the forecast-threshold buffer for logs."""
        if edge is None or edge.nws_forecast_temperature_f is None or edge.threshold_f is None:
            return "n/a"
        return f"{abs(edge.nws_forecast_temperature_f - edge.threshold_f):.1f}F"

    def _claude_payload(
        self,
        city: CityConfig,
        market: KalshiMarket,
        edge: EdgeDecision,
        nws: NwsForecast,
        enrichment: dict[str, Any],
    ) -> dict[str, Any]:
        """Build the full data payload required by the Claude checker."""
        settlement = market.settlement_time or market.close_time
        threshold = edge.threshold_f
        return {
            "city": city.name,
            "date": settlement.isoformat() if settlement else None,
            "temperature_threshold_f": edge.threshold_f,
            "side": edge.side,
            "nws_probability": edge.nws_probability_yes,
            "nws_forecast_f": edge.nws_forecast_temperature_f,
            "nws_short_forecast": nws.short_forecast,
            "market_price": edge.ask_price,
            "limit_price": edge.limit_price,
            "edge_percentage": None if edge.edge is None else round(edge.edge * 100, 2),
            "expected_value_per_contract": edge.ev,
            "confidence_score": round(edge.confidence * 100, 2),
            "hours_until_settlement": edge.hours_until_settlement,
            "active_weather_alerts": enrichment.get("active_alerts", []),
            "has_severe_alert": enrichment.get("has_severe_alert", False),
            "current_temp_f": enrichment.get("current_temp_f"),
            "current_observation": enrichment.get("current_observation", {}),
            "web_context": enrichment.get("web_context", ""),
            "buffer_score": edge.buffer_score,
            "observation_score": edge.observation_score,
            "signal_score": edge.signal_score,
            "ladder_multiplier": edge.ladder_multiplier,
            "temperature_buffer_f": abs(
                (nws.temperature_f or threshold) - threshold
            ) if nws.temperature_f and threshold is not None else None,
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

    def _maybe_record_day_end(self) -> None:
        """Apply daily compounding once after 23:00 UTC."""
        now = datetime.now(timezone.utc)
        if now.hour < 23:
            return
        today = now.date().isoformat()
        if self.risk._get_state("last_day_end_date") == today:
            return
        gross_profit = self.risk.realized_pnl_today()
        pocketed = self.risk.record_day_end(gross_profit)
        self.risk._set_state("last_day_end_date", today)
        logging.info(
            "Day-end compounding applied: gross_profit=$%.2f pocketed=$%.2f running_budget=$%.2f",
            gross_profit,
            pocketed,
            self.risk.get_running_budget(),
        )

    def _read_pnl(self) -> dict[str, Any]:
        """Read data/pnl.json with a graceful fallback for bad JSON."""
        try:
            return json.loads(self.pnl_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {"realized_pnl": 0.0, "open_risk": 0.0, "open_positions": []}

    def _send_bet_alert(self, city: CityConfig, edge: EdgeDecision, size: PositionSize, *, dry_run: bool) -> None:
        """Send a readable Discord alert for a placed or simulated bet."""
        side_label = (edge.side or "yes").upper()
        if dry_run:
            prefix = f"🧪 DRY RUN BET {side_label}"
        else:
            prefix = f"💰 BET {side_label}"
        self._send_discord(
            f"{self._user_ping()}{prefix}\n"
            f"City: {city.name}\n"
            f"Bet: {(edge.side or '').upper()} ${size.stake:.2f} @ ${edge.limit_price:.2f}\n"
            f"Edge: {(edge.edge or 0.0):.1%}\n"
            f"EV: {(edge.ev or 0.0):.4f}/contract\n"
            f"Confidence: {edge.confidence:.1%}\n"
            f"Contracts: {size.contracts}\n"
            f"Profit if win: ${size.contracts * (1 - (edge.limit_price or 0)):.2f}\n"
            f"Settles in: {edge.hours_until_settlement:.1f} hours"
        )

    def _send_risk_alert(self, reason: str) -> None:
        """Send a Discord alert for hard risk-stop events."""
        if reason.startswith("Daily loss limit hit"):
            daily_loss = abs(min(0.0, self.risk.realized_pnl_today()))
            self._send_discord(f"{self._user_ping()}⛔ DAILY LOSS LIMIT HIT\nBot stopped trading for today.\nLoss: ${daily_loss:.2f}")
        elif "Permanent halt" in reason or "Max drawdown" in reason:
            self._send_discord(f"{self._user_ping()}🚨 PERMANENT HALT\nMax drawdown reached. Manual restart required.")
        else:
            self._send_discord(f"{self._user_ping()}RISK HALT\nReason: {reason}")

    def send_win_alert(self, city: str, side: str, profit: float) -> None:
        """Send a Discord win alert when a settlement process records a win."""
        running_pnl = self.risk.current_balance() - self.risk.starting_balance
        self._send_discord(f"{self._user_ping()}✅ WIN +${profit:.2f}\n{city} resolved {side}\nRunning P&L: ${running_pnl:.2f}")

    def _user_ping(self) -> str:
        """Return a Discord user mention string for notifications."""
        user_id = os.getenv("DISCORD_USER_ID", "").strip()
        if user_id:
            return f"<@{user_id}>\n"
        return ""

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

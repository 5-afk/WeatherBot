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
from src.edge_engine import EdgeDecision, EdgeEngine, estimate_probability
from src.metar_tracker import MetarTracker
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
        self._last_settlement_check: datetime | None = None
        self.pnl_path = DATA_DIR / "pnl.json"
        self.kalshi = KalshiClient()
        self.weather = WeatherClient()
        self.metar = MetarTracker(DATA_DIR / "metar_obs.db")
        self.edge_engine = EdgeEngine()
        self.claude = ClaudeChecker()
        self.position_sizer = PositionSizer()
        self.risk = RiskManager(DATA_DIR / "positions.db")
        self._claude_calls_today = 0
        self._claude_call_date = datetime.now(timezone.utc).date()
        self._scan_lock = threading.Lock()
        if not self.dry_run:
            try:
                report = self.risk.sync_from_kalshi(self.kalshi)
                logging.info(
                    "[STATE SYNC] cash=$%.2f running_budget=$%.2f closed=%s live_on_kalshi=%d",
                    report["cash"],
                    report["running_budget"],
                    report["closed"] or "none",
                    report["live_position_count"],
                )
            except Exception as exc:
                logging.warning("Startup Kalshi state sync failed: %s", exc)
                real_balance = self.kalshi.get_balance()
                if real_balance is not None:
                    self.risk._set_state("running_budget", str(real_balance))
                    logging.info("Kalshi cash balance synced: $%.2f", real_balance)
        else:
            logging.info("Dry run — skipping Kalshi state sync")
        self._ensure_pnl_file()
        try:
            self.weather.verify_contract_driven_station_parsing(self.kalshi)
        except Exception as exc:
            logging.warning("Contract-driven station verification failed: %s", exc)
        lax = self.weather.city_for_market("KXHIGHLAX")
        if lax is not None:
            try:
                lax_markets = self.kalshi.list_markets(lax.high_series, limit=1)
                if lax_markets:
                    rules_primary = self._rules_primary_for_market(lax_markets[0])
                    station_id = self.weather.parse_settlement_station(rules_primary) or lax.nws_station
                    self.weather.log_forecast_vs_observation(lax, station_id=station_id)
            except Exception as exc:
                logging.warning("KLAX forecast vs observation check failed: %s", exc)

    def run_full_pipeline(self) -> None:
        """Run one full scan: collect markets, evaluate math, call Claude once, trade."""
        self.paused = is_paused()
        if self.paused:
            logging.info("[CYCLE] Bot is paused — skipping scan.")
            return

        self.last_scan_time = datetime.now(timezone.utc)
        logging.getLogger().log(CYCLE_LEVEL, "[CYCLE] Pipeline started. dry_run=%s", self.dry_run)

        # Hourly-throttled settlement check for any open live positions.
        try:
            self.check_settlements()
        except Exception as exc:
            logging.warning("Settlement check failed: %s", exc)

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

        active_stations: set[str] = set()
        for _city, market, _ladder in market_rows:
            rules_primary = self._rules_primary_for_market(market)
            station = self.weather.parse_settlement_station(rules_primary)
            if station:
                active_stations.add(station)
        if active_stations:
            self.metar.update_all_stations(sorted(active_stations))

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
        # NO preferred when signal scores are equal — more brackets resolve NO than YES.
        results.sort(key=lambda item: (0 if item[2].side == "no" else 1, -item[2].signal_score))
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

        rules_primary = self._rules_primary_for_market(market)
        settlement_station = self.weather.parse_settlement_station(rules_primary)
        if not settlement_station:
            logging.warning("[SKIP] %s — could not determine settlement station from rules", market.ticker)
            self._log_skip(city, market, "Could not determine settlement station from rules.", None)
            return None

        cache_key = (settlement_station, target_date.isoformat(), market_type)
        try:
            with self._scan_lock:
                cached_weather = self._weather_cache.get(cache_key)
            if cached_weather is None:
                nws = self.weather.get_station_forecast(
                    settlement_station, target_date, market_type, city=city
                )
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

        if nws.temperature_f is None:
            logging.warning("[SKIP] %s — no forecast available for station %s", market.ticker, settlement_station)
            self._log_skip(city, market, f"No forecast available for station {settlement_station}.", None)
            return None
        logging.debug(
            "[STATION] %s using %s forecast=%.1f°F",
            market.ticker,
            settlement_station,
            nws.temperature_f,
        )

        current_temp_f = self._current_temp_f(settlement_station)
        imbalance_score = self._fetch_imbalance(market)
        threshold = self.edge_engine.parse_threshold(market)
        hours_until = self.edge_engine.hours_until_settlement(settlement_time)
        strike_type = self.edge_engine.strike_type(market)
        metar_threshold = threshold or 0.0
        upper_threshold: float | None = None
        if strike_type == "between":
            floor = market.raw.get("floor_strike")
            cap = market.raw.get("cap_strike")
            if floor is not None:
                metar_threshold = float(floor)
            if cap is not None:
                upper_threshold = float(cap)
        metar_assessment = self.metar.assess_market_vs_observation(
            settlement_station,
            threshold_f=metar_threshold,
            side="yes",
            strike_type=strike_type,
            market_type=market_type.upper(),
            hours_until_settlement=hours_until or 24.0,
            upper_threshold_f=upper_threshold,
        )
        edge = self.edge_engine.evaluate(
            market,
            nws=nws,
            settlement_station=settlement_station,
            target_date=target_date,
            current_temp_f=current_temp_f,
            ladder_multiplier=ladder_multiplier,
            imbalance_score=imbalance_score,
            metar_assessment=metar_assessment,
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
        ask_price = edge.ask_price
        if ask_price is not None and ask_price < 0.05:
            logging.warning(
                "[SKIP] %s %s ask price $%.2f too low — market effectively resolved",
                market.ticker,
                (edge.side or "").upper(),
                ask_price,
            )
            self._log_skip(city, market, f"Ask price ${ask_price:.2f} too low — market effectively resolved", edge)
            return
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

        risk_check = self.risk.can_trade(market.ticker, size.stake, current_budget)
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

    def _rules_primary_for_market(self, market: KalshiMarket) -> str:
        """Return rules_primary from list payload or fall back to a per-ticker fetch."""
        rules_primary = str(market.raw.get("rules_primary", ""))
        if rules_primary:
            return rules_primary
        return str(self.kalshi.get_market_rules(market.ticker).get("rules_primary", ""))

    def _batch_candidate_payload(
        self,
        city: CityConfig,
        market: KalshiMarket,
        edge: EdgeDecision,
    ) -> dict[str, Any]:
        """Build one candidate row for the single Claude batch call."""
        settlement = market.settlement_time or market.close_time
        rules_primary = self._rules_primary_for_market(market)
        settlement_station = self.weather.parse_settlement_station(rules_primary) or ""
        market_rules = self.kalshi.get_market_rules(market.ticker)
        if not rules_primary:
            rules_primary = str(market_rules.get("rules_primary", ""))
            if not settlement_station:
                settlement_station = self.weather.parse_settlement_station(rules_primary) or ""
        price = edge.limit_price or edge.ask_price or 0.0
        budget = self.risk.get_todays_budget()
        sized = self.position_sizer.size_trade(
            win_probability=min(edge.model_probability or 0.99, 0.99),
            price=price,
            confidence=edge.confidence,
            current_budget=budget,
        )
        contracts = sized.contracts
        stake = sized.stake
        profit_if_wins = sized.profit_if_wins
        metar = edge.metar_assessment or {}
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
            "rules_primary": rules_primary,
            "expiration_time": market_rules.get("expiration_time", ""),
            "settlement_station": settlement_station,
            "last_trading_time": market_rules.get("close_time", ""),
            "proposed_stake": stake,
            "contracts": contracts,
            "profit_if_wins": profit_if_wins,
            "return_multiple": round((1.0 - price) / price, 2) if price > 0 else 0.0,
            "min_required_profit": round(stake, 2),
            "observed_daily_max_f": metar.get("observed_max_f"),
            "observed_daily_min_f": metar.get("observed_min_f"),
            "current_station_temp_f": metar.get("current_temp_f"),
            "metar_resolved_direction": metar.get("resolved_direction"),
            "metar_confidence": metar.get("confidence"),
            "metar_reason": metar.get("reason"),
            "temperature_trend": metar.get("trend"),
            "hours_of_heating_remaining": metar.get("hours_of_heating_remaining"),
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

    def _current_temp_f(self, settlement_station: str) -> float | None:
        """Fetch latest station temperature from NWS and convert Celsius to Fahrenheit."""
        with self._scan_lock:
            if settlement_station in self._observation_cache:
                return self._observation_cache[settlement_station]
        observation = self.weather.latest_station_observation(settlement_station)
        if not observation:
            with self._scan_lock:
                self._observation_cache[settlement_station] = None
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
                self._observation_cache[settlement_station] = None
            return None
        current_temp = round(celsius * 9 / 5 + 32, 2)
        with self._scan_lock:
            self._observation_cache[settlement_station] = current_temp
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

        # IOC orders are terminal on return: Kalshi fills what it can against the
        # resting ask and cancels any remainder. The V2 create response reports
        # the immediate fill count, so read it directly — no polling, no timeout,
        # no follow-up cancel needed.
        fill_raw = response.get("fill_count", response.get("filled_count", 0))
        try:
            filled_count = int(float(fill_raw or 0))
        except (TypeError, ValueError):
            filled_count = 0

        if filled_count < 1:
            logging.warning(
                "[BET UNFILLED] %s IOC order filled 0 contracts — no marketable liquidity at $%.2f",
                market.ticker,
                edge.limit_price or 0.0,
            )
            return

        logging.info(
            "[BET CONFIRMED] %s %s | %d contracts filled (order %s)",
            market.ticker,
            edge.side,
            filled_count,
            order_id or "n/a",
        )

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
        self._send_live_bet_alert(city, market, edge, filled_count, actual_stake)

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

    def _send_live_bet_alert(
        self,
        city: CityConfig,
        market: KalshiMarket,
        edge: EdgeDecision,
        filled_count: int,
        actual_stake: float,
    ) -> None:
        """Notify Discord of a confirmed live fill using the actual fill data."""
        side_label = (edge.side or "yes").upper()
        price = edge.limit_price or edge.ask_price or 0.0
        profit_if_wins = round(filled_count * (1.0 - price), 2)
        return_multiple = round(profit_if_wins / actual_stake, 1) if actual_stake > 0 else 0.0
        budget = self.risk.get_running_budget()
        open_count = self.risk.open_position_count()
        dynamic_max = self.risk.max_positions_for_bet(
            actual_stake,
            self.risk.get_todays_budget(),
        )
        exposure = self.risk.get_total_open_exposure()
        deployment_pct = (exposure / budget * 100) if budget > 0 else 0.0
        budget_remaining = self.risk.get_todays_budget()
        title = market.title or market.ticker
        sub = market.subtitle or (f"{edge.threshold_f:.0f}°+" if edge.threshold_f else "")
        self._send_discord(
            "🎯 BET PLACED — LIVE\n"
            f"Market: {title}\n"
            f"Side: {side_label}" + (f" ({sub})" if sub else "") + "\n"
            f"Contracts: {filled_count} @ ${price:.2f}\n"
            f"Total cost: ${actual_stake:.2f}\n"
            f"Profit if wins: **+${profit_if_wins:.2f}** ({return_multiple:.1f}x return)\n"
            f"Positions: {open_count}/{dynamic_max}\n"
            f"Bankroll deployed: {deployment_pct:.0f}%\n"
            f"Budget remaining: ${budget_remaining:.2f}",
            mention=True,
        )

    def audit_positions(self) -> list[dict[str, Any]]:
        """Audit open positions and recommend HOLD or SELL for each."""
        results: list[dict[str, Any]] = []
        positions = self.risk.get_open_live_positions()

        for pos in positions:
            ticker = pos["ticker"]
            side = str(pos["side"]).lower()
            contracts = int(pos["contracts"])
            original_cost = float(pos["stake"])
            avg_price = original_cost / contracts if contracts > 0 else 0.0
            del avg_price

            try:
                market_data = self.kalshi._get(f"/markets/{ticker}")
                m = market_data.get("market", {})
                status = str(m.get("status", "")).lower()
                rules_primary = str(m.get("rules_primary", ""))

                if status in ("finalized", "settled"):
                    results.append({
                        "ticker": ticker,
                        "recommendation": "ALREADY_SETTLED",
                        "reason": f"Market already {status}",
                        "auto_sell": False,
                    })
                    continue

                yes_bid = self.kalshi._price_to_float(m.get("yes_bid_dollars")) or self.kalshi._extract_price(m, "yes_bid") or 0.0
                yes_ask = self.kalshi._price_to_float(m.get("yes_ask_dollars")) or self.kalshi._extract_price(m, "yes_ask") or 0.0
                no_bid = 1.0 - yes_ask if yes_ask > 0 else 0.0
                no_ask = 1.0 - yes_bid if yes_bid > 0 else 0.0

                if side == "no":
                    current_price = no_bid
                    current_value = current_price * contracts
                else:
                    current_price = yes_bid
                    current_value = current_price * contracts

                unrealized_pnl = current_value - original_cost
                unrealized_pct = (unrealized_pnl / original_cost * 100) if original_cost > 0 else 0.0

                self.kalshi.get_orderbook(ticker)
                has_liquidity = current_price > 0.02

                settlement_station = self.weather.parse_settlement_station(rules_primary)
                hours_until = self.kalshi._hours_until_settlement(m)
                target_date = self.kalshi._target_date_from_ticker(ticker)
                market_type = "high" if "HIGH" in ticker.upper() else "low"
                strike_type = str(m.get("strike_type", "greater"))
                threshold = float(m.get("floor_strike") or m.get("cap_strike") or 0)

                forecast_temp: float | None = None
                current_edge: float | None = None
                edge_flipped = False

                series_prefix = ticker.split("-", 1)[0]
                city = self.weather.city_for_market(series_prefix, ticker)
                if settlement_station and target_date and city is not None:
                    forecast = self.weather.get_station_forecast(
                        settlement_station,
                        target_date,
                        market_type,
                        city=city,
                    )
                    if forecast.temperature_f is not None:
                        forecast_temp = forecast.temperature_f
                        yes_prob = estimate_probability(
                            forecast_temp,
                            threshold,
                            market_type,
                            strike_type=strike_type,
                            station_id=settlement_station,
                            target_date=target_date,
                            hours_until_settlement=hours_until,
                        )
                        model_prob = yes_prob if side == "yes" else (1.0 - yes_prob)
                        market_price = yes_ask if side == "yes" else no_ask
                        current_edge = model_prob - market_price
                        edge_flipped = current_edge < 0

                recommendation = "HOLD"
                reason = ""
                urgency = "LOW"

                if edge_flipped:
                    recommendation = "SELL"
                    reason = (
                        f"Edge FLIPPED — model now says {side.upper()} is wrong side "
                        f"(edge={current_edge:.2f})"
                    )
                    urgency = "HIGH"
                elif hours_until < 2 and unrealized_pct < -30:
                    recommendation = "SELL"
                    reason = (
                        f"Settling in {hours_until:.1f}h and down {unrealized_pct:.0f}% — cut losses"
                    )
                    urgency = "HIGH"
                elif unrealized_pct < -70 and hours_until > 6:
                    recommendation = "SELL"
                    reason = (
                        f"Down {unrealized_pct:.0f}% with {hours_until:.1f}h left — recover some value"
                    )
                    urgency = "MEDIUM"
                elif current_price > 0 and (1.0 - current_price) / current_price < 0.5:
                    recommendation = "SELL"
                    return_multiple = (1.0 - current_price) / current_price
                    reason = (
                        f"Return multiple fallen to {return_multiple:.2f}x — no longer profitable"
                    )
                    urgency = "MEDIUM"
                elif current_edge is not None and current_edge > 0.05:
                    recommendation = "HOLD"
                    reason = (
                        f"Edge still positive ({current_edge:.2f}), forecast {forecast_temp:.1f}°F, "
                        f"{hours_until:.1f}h to settle"
                    )
                else:
                    recommendation = "HOLD"
                    reason = (
                        f"No strong sell signal, {hours_until:.1f}h to settlement, "
                        f"P&L {unrealized_pct:+.0f}%"
                    )

                results.append({
                    "ticker": ticker,
                    "side": side,
                    "contracts": contracts,
                    "original_cost": original_cost,
                    "current_value": round(current_value, 2),
                    "unrealized_pnl": round(unrealized_pnl, 2),
                    "unrealized_pct": round(unrealized_pct, 1),
                    "current_price": current_price,
                    "has_liquidity": has_liquidity,
                    "forecast_temp": forecast_temp,
                    "current_edge": current_edge,
                    "hours_until": hours_until,
                    "recommendation": recommendation,
                    "reason": reason,
                    "urgency": urgency,
                    "auto_sell": recommendation == "SELL" and has_liquidity and urgency == "HIGH",
                })

            except Exception as exc:
                logging.warning("Audit failed for %s: %s", ticker, exc)
                results.append({
                    "ticker": ticker,
                    "recommendation": "ERROR",
                    "reason": str(exc),
                    "auto_sell": False,
                })

        return results

    def check_settlements(self) -> None:
        """Check open positions for settlement. Only book P&L when finalized."""
        if self.dry_run:
            return

        now = datetime.now(timezone.utc)
        last = getattr(self, "_last_settlement_check", None)
        if last is not None and (now - last).total_seconds() < 3600:
            return
        self._last_settlement_check = now

        open_positions = self.risk.get_open_live_positions()
        for position in open_positions:
            ticker = position["ticker"]
            try:
                market_obj = self.kalshi.get_market(ticker)
                if market_obj is None:
                    logging.debug("[SETTLEMENT] %s — market not found, skipping", ticker)
                    continue
                m = market_obj.raw
                status = str(m.get("status", "")).lower()
                result = m.get("result")

                if status not in ("finalized", "settled"):
                    logging.debug(
                        "[SETTLEMENT] %s status=%s — not yet finalized, skipping",
                        ticker,
                        status,
                    )
                    continue

                if not result or str(result).lower() not in ("yes", "no"):
                    logging.debug(
                        "[SETTLEMENT] %s status=%s result=%s — no result yet, skipping",
                        ticker,
                        status,
                        result,
                    )
                    continue

                side = str(position["side"]).lower()
                result = str(result).lower()
                won = side == result
                contracts = int(position["contracts"])
                stake = float(position["stake"])

                if won:
                    payout = contracts * 1.00
                    pnl = payout - stake
                    logging.info(
                        "[SETTLEMENT] WON %s %s | %d contracts | payout $%.2f profit $%.2f",
                        ticker,
                        side.upper(),
                        contracts,
                        payout,
                        pnl,
                    )
                else:
                    payout = 0.0
                    pnl = -stake
                    logging.info(
                        "[SETTLEMENT] LOST %s %s | %d contracts | loss -$%.2f",
                        ticker,
                        side.upper(),
                        contracts,
                        stake,
                    )

                self.risk.close_position(ticker, pnl, payout)
                try:
                    self.risk.sync_from_kalshi(self.kalshi)
                except Exception as exc:
                    logging.warning("[SETTLEMENT] Balance sync failed after %s: %s", ticker, exc)

                if won:
                    self._send_discord(
                        f"✅ **BET WON** — {ticker}\n"
                        f"{side.upper()} {contracts} contracts\n"
                        f"Profit: **+${pnl:.2f}**\n"
                        f"New balance: ${self.risk.get_running_budget():.2f}\n"
                        f"{self._user_ping()}",
                        mention=True,
                    )
                else:
                    self._send_discord(
                        f"❌ **BET LOST** — {ticker}\n"
                        f"{side.upper()} {contracts} contracts\n"
                        f"Loss: **-${stake:.2f}**\n"
                        f"New balance: ${self.risk.get_running_budget():.2f}\n"
                        f"{self._user_ping()}",
                        mention=True,
                    )

            except Exception as exc:
                logging.warning("[SETTLEMENT] Error checking %s: %s", ticker, exc)

    def _user_ping(self) -> str:
        """Return a Discord user mention string for notifications."""
        user_id = os.getenv("DISCORD_USER_ID", "").strip()
        if user_id:
            return f"<@{user_id}>\n"
        return ""

    def _send_discord(self, message: str, *, mention: bool = False) -> None:
        """Send an alert to a private Discord channel.

        When mention=True, the user ping is placed OUTSIDE the code block so
        Discord actually delivers the notification (mentions inside code fences
        do not trigger alerts).
        """
        token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
        channel_id = os.getenv("DISCORD_CHANNEL_ID", "").strip()
        if not token or not channel_id or token == "your_token_here":
            return
        ping = self._user_ping().strip() if mention else ""
        try:
            url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
            headers = {
                "Authorization": f"Bot {token}",
                "Content-Type": "application/json",
            }
            # Discord has a 2000 character limit per message
            chunks = [message[i:i+1900] for i in range(0, len(message), 1900)]
            for index, chunk in enumerate(chunks):
                content = f"```\n{chunk}\n```"
                if ping and index == 0:
                    content = f"{ping}\n{content}"
                response = requests.post(
                    url,
                    headers=headers,
                    json={"content": content},
                    timeout=10,
                )
                response.raise_for_status()
        except requests.RequestException as exc:
            logging.warning("Discord alert failed: %s", exc)

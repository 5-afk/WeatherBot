"""Strategy math for turning forecasts and prices into a trade candidate."""

from __future__ import annotations

import logging
import math
import os
import re
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone

from src.kalshi_client import KalshiMarket
from src.weather_client import NwsForecast, get_sigma

MODEL_VERSION = os.getenv("MODEL_VERSION", "v1.0")


def _norm_cdf(x: float, mean: float, sigma: float) -> float:
    """Return P(X <= x) for a normal distribution with the given mean and sigma."""
    return 0.5 * (1 + math.erf((x - mean) / (sigma * math.sqrt(2))))


def _time_adjusted_sigma(base_sigma: float, hours_until_settlement: float) -> float:
    """Collapse sigma as settlement approaches — forecast accuracy improves within same-day window."""
    if hours_until_settlement >= 24:
        multiplier = 1.0
    elif hours_until_settlement >= 18:
        multiplier = 0.85
    elif hours_until_settlement >= 12:
        multiplier = 0.70
    elif hours_until_settlement >= 6:
        multiplier = 0.50
    elif hours_until_settlement >= 2:
        multiplier = 0.30
    else:
        multiplier = 0.20
    return base_sigma * multiplier


def estimate_probability(
    forecast_temp_f: float,
    threshold_f: float,
    market_type: str,
    *,
    strike_type: str = "greater",
    upper_threshold_f: float | None = None,
    station_id: str,
    target_date: date,
    hours_until_settlement: float | None = None,
    sigma_multiplier: float = 1.0,
) -> float:
    """Estimate YES probability from an NWS forecast using per-city seasonal sigma.

    For "between" markets (e.g. B-prefix brackets), YES resolves only when the
    temperature lands inside the bracket, so the probability is
    P(threshold <= temp < upper), NOT P(temp > threshold).
    """
    hours = 24.0 if hours_until_settlement is None else hours_until_settlement
    base_sigma = get_sigma(station_id, target_date)
    sigma = _time_adjusted_sigma(base_sigma, hours) * sigma_multiplier
    if strike_type == "between":
        upper = upper_threshold_f if upper_threshold_f is not None else threshold_f + 2.0
        p_above_lower = 1 - _norm_cdf(threshold_f, forecast_temp_f, sigma)
        p_above_upper = 1 - _norm_cdf(upper, forecast_temp_f, sigma)
        return round(max(0.0, p_above_lower - p_above_upper), 4)
    if market_type.lower() == "low":
        return round(_norm_cdf(threshold_f, forecast_temp_f, sigma), 4)
    return round(1 - _norm_cdf(threshold_f, forecast_temp_f, sigma), 4)


@dataclass(frozen=True)
class EdgeDecision:
    """Full strategy decision for a single market."""

    should_trade: bool
    side: str | None
    ask_price: float | None
    limit_price: float | None
    model_probability: float | None
    edge: float | None
    confidence: float
    reason: str
    market_type: str
    threshold_f: float | None
    nws_probability_yes: float | None
    nws_forecast_temperature_f: float | None
    hours_until_settlement: float | None
    buffer_score: float = 0.5
    observation_score: float = 0.5
    ladder_multiplier: float = 1.0
    signal_score: float = 0.5
    ev: float | None = None
    imbalance_score: float = 0.5
    metar_assessment: dict | None = None
    validation_result: object | None = None


class EdgeEngine:
    """Apply the bot's price, timing, confidence, and forecast filters."""

    MIN_SIGNAL_SCORE = 0.75
    MIN_CONFIDENCE_FLOOR = 0.60
    MAX_HOURS_UNTIL_SETTLEMENT = 36

    # Signal score weights (must sum to 1.0)
    WEIGHT_BUFFER = 0.35
    WEIGHT_OBSERVATION = 0.20
    WEIGHT_CONFIDENCE = 0.25
    WEIGHT_EV = 0.20

    def __init__(self) -> None:
        """Read edge-engine thresholds from environment variables."""
        self.min_edge = float(os.getenv("MIN_EDGE", "0.08"))
        self.min_confidence = float(os.getenv("MIN_CONFIDENCE", "0.60"))
        self.min_ev_per_contract = float(os.getenv("MIN_EV_PER_CONTRACT", "0.05"))
        self.denver_min_edge = float(os.getenv("DENVER_MIN_EDGE", "0.15"))
        self.settlement_window_hours = float(os.getenv("SETTLEMENT_WINDOW_HOURS", "36"))
        self.no_side_bias = float(os.getenv("NO_SIDE_BIAS", "1.15"))
        self.min_liquidity = float(os.getenv("MIN_LIQUIDITY_CONTRACTS", "10"))
        self.min_liquidity_new_city = float(os.getenv("MIN_LIQUIDITY_NEW_CITY", "10"))
        self.min_tradeable_price = float(os.getenv("MIN_TRADEABLE_PRICE", "0.15"))
        self.max_tradeable_price = float(os.getenv("MAX_TRADEABLE_PRICE", "0.50"))
        self.min_return_multiple = float(os.getenv("MIN_RETURN_MULTIPLE", "2.0"))
        self._expansion_series = {
            "KXHIGHTSEA", "KXHIGHTSFO", "KXHIGHTDAL", "KXHIGHTMIN",
            "KXHIGHTOKC", "KXHIGHTATL", "KXHIGHTBOS", "KXHIGHTDC",
        }
        logging.info(
            "EdgeEngine MIN_EV=%.4f NO_SIDE_BIAS=%.2f MIN_LIQ=%.0f NEW_CITY_LIQ=%.0f",
            self.min_ev_per_contract,
            self.no_side_bias,
            self.min_liquidity,
            self.min_liquidity_new_city,
        )

    def evaluate(
        self,
        market: KalshiMarket,
        *,
        nws: NwsForecast,
        settlement_station: str,
        target_date: date,
        current_temp_f: float | None = None,
        ladder_multiplier: float = 1.0,
        imbalance_score: float = 0.5,
        metar_assessment: dict | None = None,
        validation_result: object | None = None,
    ) -> EdgeDecision:
        """Return a complete GO/skip decision before Claude and risk checks."""
        if validation_result is not None and getattr(validation_result, "valid", False):
            market_type = str(getattr(validation_result, "confirmed_market_type", "HIGH")).lower()
            threshold = getattr(validation_result, "confirmed_threshold", None)
            strike_type = str(getattr(validation_result, "confirmed_strike_type", "") or self.strike_type(market))
            lower_threshold = threshold
            upper_threshold = getattr(validation_result, "confirmed_upper_threshold", None)
        else:
            market_type = self.market_type(market)
            threshold = self.parse_threshold(market)
            strike_type = self.strike_type(market)
            lower_threshold = threshold
            upper_threshold: float | None = None
            if strike_type == "between":
                floor = self._safe_float(market.raw.get("floor_strike"))
                cap = self._safe_float(market.raw.get("cap_strike"))
                if floor is not None:
                    lower_threshold = floor
                if cap is not None:
                    upper_threshold = cap

        settlement_time = market.settlement_time or market.close_time
        hours_until_settlement = self.hours_until_settlement(settlement_time)
        if validation_result is not None and getattr(validation_result, "confirmed_hours_until_close", None):
            hours_until_settlement = getattr(validation_result, "confirmed_hours_until_close")

        if threshold is None:
            return self._skip("Could not parse one temperature threshold.", market_type, None, hours_until_settlement)
        if hours_until_settlement is None or not -1 <= hours_until_settlement <= self.settlement_window_hours:
            return self._skip(
                f"Contract is not within {self.settlement_window_hours:.0f} hours of settlement.",
                market_type,
                threshold,
                hours_until_settlement,
            )

        nws_temp = nws.temperature_f
        if nws_temp is None:
            return self._skip("NWS station forecast unavailable.", market_type, threshold, hours_until_settlement)

        if strike_type == "between" and upper_threshold is None:
            cap = self._safe_float(market.raw.get("cap_strike"))
            if cap is not None:
                upper_threshold = cap
            logging.debug(
                "between market %s bracket lower=%s upper=%s (forecast=%.1f)",
                market.ticker,
                lower_threshold,
                upper_threshold if upper_threshold is not None else lower_threshold + 2.0,
                nws_temp,
            )

        sigma_multiplier = 1.4 if nws.uncertain else 1.0
        nws_probability_yes = estimate_probability(
            nws_temp,
            lower_threshold,
            market_type,
            strike_type=strike_type,
            upper_threshold_f=upper_threshold,
            station_id=settlement_station,
            target_date=target_date,
            hours_until_settlement=hours_until_settlement,
            sigma_multiplier=sigma_multiplier,
        )
        nws_probability_yes = self._blend_metar_probability(
            nws_probability_yes,
            metar_assessment,
            hours_until_settlement,
            market.ticker,
        )

        side_kwargs = dict(
            settlement_station=settlement_station,
            target_date=target_date,
            nws_probability_yes=nws_probability_yes,
            nws_temp=nws_temp,
            threshold=threshold,
            market_type=market_type,
            hours_until_settlement=hours_until_settlement,
            current_temp_f=current_temp_f,
            ladder_multiplier=ladder_multiplier,
            imbalance_score=imbalance_score,
            strike_type=strike_type,
            floor_strike=lower_threshold if strike_type == "between" else None,
            cap_strike=upper_threshold if strike_type == "between" else None,
        )
        yes_decision = self._evaluate_side(market, side="yes", **side_kwargs)
        no_decision = self._evaluate_side(market, side="no", **side_kwargs)

        passing = [d for d in (yes_decision, no_decision) if d.should_trade]
        if passing:
            chosen = max(passing, key=lambda d: (0 if d.side == "no" else 1, -(d.ev or 0.0)))
            return replace(chosen, metar_assessment=metar_assessment, validation_result=validation_result)

        yes_wrong_side = (
            not yes_decision.should_trade
            and "wrong side of threshold" in yes_decision.reason.lower()
        )
        if yes_wrong_side and no_decision.should_trade:
            return replace(no_decision, metar_assessment=metar_assessment, validation_result=validation_result)
        if yes_wrong_side and no_decision.ev is not None and no_decision.buffer_score > 0:
            logging.debug(
                "YES buffer failed for %s — NO side ev=%.4f buffer=%.2f reason=%s",
                market.ticker,
                no_decision.ev,
                no_decision.buffer_score,
                no_decision.reason,
            )

        candidates = [yes_decision, no_decision]
        if yes_wrong_side:
            chosen = max(candidates, key=lambda d: (0 if d.side == "no" else 1, d.ev if d.ev is not None else -999.0))
            return replace(chosen, metar_assessment=metar_assessment, validation_result=validation_result)
        chosen = max(candidates, key=lambda d: d.ev if d.ev is not None else -999.0)
        return replace(chosen, metar_assessment=metar_assessment, validation_result=validation_result)

    @staticmethod
    def _blend_metar_probability(
        nws_probability_yes: float,
        metar_assessment: dict | None,
        hours_until_settlement: float | None,
        ticker: str,
    ) -> float:
        """Blend NWS forecast probability with METAR-derived resolution signal."""
        if not metar_assessment or metar_assessment.get("resolved_direction") == "uncertain":
            return nws_probability_yes

        hours_until = hours_until_settlement if hours_until_settlement is not None else 24.0
        metar_confidence = float(metar_assessment["confidence"])
        metar_direction = metar_assessment["resolved_direction"]
        metar_weight = min(0.80, (1 - hours_until / 24) * 0.80)
        nws_weight = 1.0 - metar_weight
        metar_prob = metar_confidence if metar_direction == "yes" else (1.0 - metar_confidence)
        blended_prob = (nws_weight * nws_probability_yes) + (metar_weight * metar_prob)
        logging.info(
            "[METAR SIGNAL] %s | obs_max=%.1f°F | direction=%s | conf=%.2f | "
            "NWS_prob=%.2f metar_prob=%.2f blended=%.2f",
            ticker,
            float(metar_assessment.get("observed_max_f") or 0),
            metar_direction,
            metar_confidence,
            nws_probability_yes,
            metar_prob,
            blended_prob,
        )
        return round(blended_prob, 4)

    def _evaluate_side(
        self,
        market: KalshiMarket,
        *,
        side: str,
        settlement_station: str,
        target_date: date,
        nws_probability_yes: float,
        nws_temp: float,
        threshold: float,
        market_type: str,
        hours_until_settlement: float,
        current_temp_f: float | None,
        ladder_multiplier: float,
        imbalance_score: float,
        strike_type: str = "greater",
        floor_strike: float | None = None,
        cap_strike: float | None = None,
    ) -> EdgeDecision:
        """Evaluate one side (yes or no) through all gates."""
        ask_price = market.yes_ask if side == "yes" else market.no_ask
        if ask_price is None:
            return self._skip(
                f"Missing {side.upper()} ask price.",
                market_type,
                threshold,
                hours_until_settlement,
                side=side,
            )
        if ask_price < 0.02 or ask_price > 0.98:
            return self._skip(
                f"Market price ${ask_price:.2f} is essentially settled",
                market_type,
                threshold,
                hours_until_settlement,
                side=side,
                ask_price=ask_price,
                nws_probability_yes=nws_probability_yes,
                nws_temp=nws_temp,
            )
        if ask_price < 0.05:
            return self._skip(
                f"Ask price ${ask_price:.2f} too low — market effectively resolved (Kalshi rejects orders)",
                market_type,
                threshold,
                hours_until_settlement,
                side=side,
                ask_price=ask_price,
                nws_probability_yes=nws_probability_yes,
                nws_temp=nws_temp,
            )
        price_band_reason = self._check_price_band(ask_price, side)
        if price_band_reason:
            return self._skip(
                price_band_reason,
                market_type,
                threshold,
                hours_until_settlement,
                side=side,
                ask_price=ask_price,
                nws_probability_yes=nws_probability_yes,
                nws_temp=nws_temp,
            )
        if not self._check_liquidity(market, side):
            return self._skip(
                "Insufficient liquidity at ask price.",
                market_type,
                threshold,
                hours_until_settlement,
                side=side,
                ask_price=ask_price,
                nws_probability_yes=nws_probability_yes,
                nws_temp=nws_temp,
                ladder_multiplier=ladder_multiplier,
            )

        raw_buffer = self._raw_buffer(
            nws_temp,
            threshold,
            market_type,
            side,
            strike_type=strike_type,
            floor_strike=floor_strike,
            cap_strike=cap_strike,
        )
        buffer_score = self._temperature_buffer_score(
            nws_temp,
            threshold,
            market_type,
            side,
            strike_type=strike_type,
            floor_strike=floor_strike,
            cap_strike=cap_strike,
        )
        if buffer_score == 0.0:
            return EdgeDecision(
                False,
                side,
                ask_price,
                self.limit_price(side, ask_price),
                None,
                None,
                self._confidence_for_side(nws_probability_yes, side),
                "Temperature buffer is on the wrong side of threshold.",
                market_type,
                threshold,
                nws_probability_yes,
                nws_temp,
                hours_until_settlement,
                buffer_score,
                0.5,
                ladder_multiplier,
                0.0,
                None,
                imbalance_score,
            )
        min_buffer = self._minimum_buffer_for_station(settlement_station, target_date)
        if raw_buffer < min_buffer:
            return self._skip(
                f"Buffer {raw_buffer:.1f}°F below minimum {min_buffer:.1f}°F for {settlement_station}",
                market_type,
                threshold,
                hours_until_settlement,
                side=side,
                ask_price=ask_price,
                nws_probability_yes=nws_probability_yes,
                nws_temp=nws_temp,
                ladder_multiplier=ladder_multiplier,
            )

        if not self._nws_agrees(
            nws_temp,
            threshold,
            market_type,
            side,
            strike_type=strike_type,
            floor_strike=floor_strike,
            cap_strike=cap_strike,
        ):
            return EdgeDecision(
                False,
                side,
                ask_price,
                self.limit_price(side, ask_price),
                None,
                None,
                self._confidence_for_side(nws_probability_yes, side),
                "NWS forecast contradicts the proposed side.",
                market_type,
                threshold,
                nws_probability_yes,
                nws_temp,
                hours_until_settlement,
                buffer_score,
                0.5,
                ladder_multiplier,
                0.0,
                None,
                imbalance_score,
            )

        model_probability = self._model_probability_for_side(nws_probability_yes, side)
        corrected_price = self._correct_market_price(ask_price, side)
        ev = self._calculate_ev(model_probability, corrected_price)
        if ev < self.min_ev_per_contract:
            return self._skip(
                f"EV {ev:.4f} below threshold {self.min_ev_per_contract:.4f}",
                market_type,
                threshold,
                hours_until_settlement,
                side=side,
                ask_price=ask_price,
                nws_probability_yes=nws_probability_yes,
                nws_temp=nws_temp,
                ev=ev,
                imbalance_score=imbalance_score,
            )

        edge = model_probability - corrected_price
        confidence = self._confidence_for_side(nws_probability_yes, side)

        required_edge = self._required_edge(market)
        if buffer_score == 0.1:
            required_edge = max(required_edge, 0.20)
        observation_score = self._observation_score(
            current_temp_f,
            threshold,
            market_type,
            side,
            hours_until_settlement,
        )
        signal_score = self._combined_signal_score(
            buffer_score,
            observation_score,
            confidence,
            ev,
        )
        # Statistically more brackets resolve NO than YES on Kalshi temperature markets.
        if side == "no":
            signal_score = min(1.0, round(signal_score * self.no_side_bias, 3))
            ev = round(ev * self.no_side_bias, 4)
        pre_claude_decision = EdgeDecision(
            False,
            side,
            ask_price,
            self.limit_price(side, ask_price),
            model_probability,
            edge,
            confidence,
            "",
            market_type,
            threshold,
            nws_probability_yes,
            nws_temp,
            hours_until_settlement,
            buffer_score,
            observation_score,
            ladder_multiplier,
            signal_score,
            ev,
            imbalance_score,
        )
        pre_claude_reason = self.pre_claude_gate_failure(pre_claude_decision, required_edge)
        if pre_claude_reason:
            return EdgeDecision(
                False,
                side,
                ask_price,
                self.limit_price(side, ask_price),
                model_probability,
                edge,
                confidence,
                f"Pre-Claude gate failed: {pre_claude_reason}",
                market_type,
                threshold,
                nws_probability_yes,
                nws_temp,
                hours_until_settlement,
                buffer_score,
                observation_score,
                ladder_multiplier,
                signal_score,
                ev,
                imbalance_score,
            )

        if edge <= required_edge:
            return EdgeDecision(
                False,
                side,
                ask_price,
                self.limit_price(side, ask_price),
                model_probability,
                edge,
                confidence,
                f"Edge {edge:.1%} is below required {required_edge:.1%} threshold.",
                market_type,
                threshold,
                nws_probability_yes,
                nws_temp,
                hours_until_settlement,
                buffer_score,
                observation_score,
                ladder_multiplier,
                signal_score,
                ev,
                imbalance_score,
            )
        return EdgeDecision(
            True,
            side,
            ask_price,
            self.limit_price(side, ask_price),
            model_probability,
            edge,
            confidence,
            "Strong NWS station forecast with favorable buffer.",
            market_type,
            threshold,
            nws_probability_yes,
            nws_temp,
            hours_until_settlement,
            buffer_score,
            observation_score,
            ladder_multiplier,
            signal_score,
            ev,
            imbalance_score,
        )

    def pre_claude_gate_failure(self, decision: EdgeDecision, required_edge: float | None = None) -> str | None:
        """Return a reason when the strict pre-Claude gate should block the trade."""
        if required_edge is None:
            required_edge = self.min_edge
        if decision.signal_score < self.MIN_SIGNAL_SCORE:
            return f"signal_score {decision.signal_score:.2f} < {self.MIN_SIGNAL_SCORE}"
        if decision.edge is None or decision.edge < required_edge:
            edge_text = "n/a" if decision.edge is None else f"{decision.edge:.1%}"
            return f"edge {edge_text} < {required_edge:.1%}"
        if decision.confidence < self.MIN_CONFIDENCE_FLOOR:
            return f"confidence {decision.confidence:.2f} below sanity floor {self.MIN_CONFIDENCE_FLOOR}"
        if decision.buffer_score < 0.50:
            return f"buffer_score {decision.buffer_score:.2f} < 0.50"
        if decision.hours_until_settlement is None:
            return "hours_until_settlement unavailable"
        if decision.hours_until_settlement > self.MAX_HOURS_UNTIL_SETTLEMENT:
            return f"hours_until_settlement {decision.hours_until_settlement:.1f} > {self.MAX_HOURS_UNTIL_SETTLEMENT}"
        return None

    def market_type(self, market: KalshiMarket) -> str:
        """Infer whether a market resolves on a daily high or low."""
        ticker_text = f"{market.series_ticker} {market.ticker}".upper()
        if "KXLOW" in ticker_text:
            return "low"
        if "KXHIGH" in ticker_text:
            return "high"
        return "low" if re.search(r"\blow\b", f"{market.title} {market.subtitle}".lower()) else "high"

    def market_type_label(self, market: KalshiMarket) -> str:
        """Return HIGH or LOW for Claude payloads."""
        return self.market_type(market).upper()

    def strike_type(self, market: KalshiMarket) -> str:
        """Return Kalshi's strike_type (e.g. 'greater', 'less', 'between')."""
        return str(market.raw.get("strike_type", "")).lower()

    @staticmethod
    def _safe_float(value: object) -> float | None:
        """Convert a value to float, returning None when conversion fails."""
        try:
            return float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    def parse_threshold(self, market: KalshiMarket) -> float | None:
        """Extract one Fahrenheit threshold from the market title/subtitle."""
        market_type = self.market_type(market)
        text = f"{market.title} {market.subtitle}"
        matches = re.findall(
            r"(?<![A-Z0-9])(-?\d+(?:\.\d+)?)\s*(?:°|degrees?|F\b|fahrenheit\b)",
            text,
            re.IGNORECASE,
        )
        if not matches:
            matches = re.findall(r"(?<![A-Z0-9])(\d+(?:\.\d+)?)(?![A-Z0-9])", market.subtitle, re.IGNORECASE)
        if not matches:
            return None

        threshold = float(matches[-1])
        if market_type == "high" and threshold is not None and threshold < 0:
            return None
        if market_type == "low" and threshold is not None and threshold < -50:
            return None
        return threshold

    def hours_until_settlement(self, settlement_time: datetime | None) -> float | None:
        """Calculate hours from now until settlement in UTC."""
        if settlement_time is None:
            return None
        delta = settlement_time.astimezone(timezone.utc) - datetime.now(timezone.utc)
        return round(delta.total_seconds() / 3600, 2)

    def limit_price(self, side: str, ask_price: float) -> float:
        """Return the exact ask so the order is marketable and fills instantly.

        Shaving a cent off the ask left the order resting below the book and
        never filling. Pricing at the ask crosses immediately against the
        resting offer.
        """
        del side
        return round(max(0.01, min(0.99, ask_price)), 2)

    def _check_price_band(self, ask_price: float, side: str) -> str | None:
        """Enforce price band and minimum return multiple. Returns skip reason or None."""
        del side
        if ask_price < self.min_tradeable_price:
            return (
                f"Price ${ask_price:.2f} below minimum ${self.min_tradeable_price:.2f} — fee drag too high"
            )
        if ask_price > self.max_tradeable_price:
            return (
                f"Price ${ask_price:.2f} above ${self.max_tradeable_price:.2f} — "
                f"violates {self.min_return_multiple}x minimum return"
            )
        profit_per_dollar = (1.0 - ask_price) / ask_price
        if profit_per_dollar < (self.min_return_multiple - 1.0):
            return (
                f"Return {profit_per_dollar:.2f}x below minimum "
                f"{self.min_return_multiple - 1:.1f}x profit on cost"
            )
        return None

    @staticmethod
    def _correct_market_price(raw_price: float, side: str) -> float:
        """Correct for favorite-longshot bias in Kalshi weather markets (signal only)."""
        del side
        if raw_price > 0.85:
            corrected = raw_price - 0.06
        elif raw_price > 0.70:
            corrected = raw_price - 0.03
        elif raw_price < 0.15:
            corrected = raw_price + 0.06
        elif raw_price < 0.30:
            corrected = raw_price + 0.03
        else:
            corrected = raw_price
        return max(0.02, min(0.98, corrected))

    @staticmethod
    def _minimum_buffer_for_station(station_id: str, target_date: date) -> float:
        """Minimum temperature buffer required scales with city sigma (1.5×)."""
        return get_sigma(station_id, target_date) * 1.5

    def _raw_buffer(
        self,
        nws_temp: float | None,
        threshold: float,
        market_type: str,
        side: str,
        *,
        strike_type: str = "greater",
        floor_strike: float | None = None,
        cap_strike: float | None = None,
    ) -> float:
        """Return signed favorable buffer distance in °F (negative = wrong side)."""
        if nws_temp is None:
            return -999.0

        if strike_type == "between" and floor_strike is not None and cap_strike is not None:
            floor, cap = floor_strike, cap_strike
            inside = floor <= nws_temp <= cap
            if side == "yes":
                if inside:
                    return min(nws_temp - floor, cap - nws_temp)
                if nws_temp < floor:
                    return nws_temp - floor
                return cap - nws_temp
            if not inside:
                return (floor - nws_temp) if nws_temp < floor else (nws_temp - cap)
            return -min(nws_temp - floor, cap - nws_temp)
        if market_type == "high":
            return nws_temp - threshold if side == "yes" else threshold - nws_temp
        return threshold - nws_temp if side == "yes" else nws_temp - threshold

    def _temperature_buffer_score(
        self,
        nws_temp: float | None,
        threshold: float,
        market_type: str,
        side: str,
        *,
        strike_type: str = "greater",
        floor_strike: float | None = None,
        cap_strike: float | None = None,
    ) -> float:
        """Score 0.0-1.0 based on how far the forecast favors this side."""
        buffer = self._raw_buffer(
            nws_temp,
            threshold,
            market_type,
            side,
            strike_type=strike_type,
            floor_strike=floor_strike,
            cap_strike=cap_strike,
        )
        if buffer < 0.0:
            return 0.0
            return 1.0
        if buffer >= 3.0:
            return 0.8
        if buffer >= 1.0:
            return 0.3
        if buffer >= 0.0:
            return 0.1
        return 0.0

    def _observation_score(
        self,
        current_temp_f: float | None,
        threshold: float,
        market_type: str,
        side: str,
        hours_until_settlement: float | None,
    ) -> float:
        """Score based on current live temperature vs threshold."""
        del side
        if current_temp_f is None or hours_until_settlement is None or hours_until_settlement > 8:
            return 0.5
        if market_type == "high":
            already_exceeded = current_temp_f >= threshold
        else:
            already_exceeded = current_temp_f <= threshold
        if already_exceeded and hours_until_settlement < 4:
            return 1.0
        if already_exceeded and hours_until_settlement < 8:
            return 0.9
        buffer = abs(current_temp_f - threshold)
        if buffer >= 5:
            return 0.8
        if buffer >= 2:
            return 0.6
        return 0.4

    def calculate_ladder_sum(self, all_market_prices: list[float]) -> float:
        """Return ladder multiplier from summed YES ask prices."""
        if not all_market_prices:
            return 1.0
        total = sum(all_market_prices)
        if total < 0.92:
            return 1.20
        if total > 1.08:
            return 0.80
        return 1.00

    def _min_liquidity_for_market(self, market: KalshiMarket) -> float:
        """Return minimum ask depth; expansion cities use a lower threshold."""
        series = str(market.series_ticker or market.ticker).upper()
        for expansion in self._expansion_series:
            if expansion in series:
                return self.min_liquidity_new_city
        return self.min_liquidity

    def _check_liquidity(self, market: KalshiMarket, side: str | None = None) -> bool:
        """Only trade markets with meaningful liquidity on the proposed side.

        Kalshi often omits no_ask_size_fp even when a NO ask price exists — if the
        ask price is present and tradeable, do not block on missing size data.
        """
        yes_size = float(market.raw.get("yes_ask_size_fp") or 0)
        no_size = float(market.raw.get("no_ask_size_fp") or 0)
        min_size = self._min_liquidity_for_market(market)
        if side == "yes":
            if yes_size <= 0 and market.yes_ask is not None and market.yes_ask >= 0.05:
                return True
            return yes_size >= min_size
        if side == "no":
            if no_size <= 0 and market.no_ask is not None and market.no_ask >= 0.05:
                return True
            return no_size >= min_size
        if yes_size >= min_size or no_size >= min_size:
            return True
        return (
            (market.yes_ask is not None and market.yes_ask >= 0.05)
            or (market.no_ask is not None and market.no_ask >= 0.05)
        )

    def _calculate_ev(
        self,
        model_probability: float,
        market_price: float,
    ) -> float:
        """Calculate expected value per contract (positive = profitable)."""
        if not 0 < market_price < 1:
            return -999.0
        profit_if_win = 1.0 - market_price
        loss_if_lose = market_price
        ev = (model_probability * profit_if_win) - ((1 - model_probability) * loss_if_lose)
        return round(ev, 4)

    def _combined_signal_score(
        self,
        buffer_score: float,
        observation_score: float,
        confidence: float,
        ev: float,
    ) -> float:
        """Return the weighted combined signal score used before Claude."""
        ev_score = min(1.0, max(0.0, ev / 0.20))
        contributions = {
            "buffer": buffer_score * self.WEIGHT_BUFFER,
            "obs": observation_score * self.WEIGHT_OBSERVATION,
            "conf": confidence * self.WEIGHT_CONFIDENCE,
            "ev": ev_score * self.WEIGHT_EV,
        }
        weights_sum = (
            self.WEIGHT_BUFFER + self.WEIGHT_OBSERVATION
            + self.WEIGHT_CONFIDENCE + self.WEIGHT_EV
        )
        signal_score = round(min(sum(contributions.values()), 1.0), 3)
        logging.debug(
            "signal components ev=%.3f ev_score=%.3f | buffer=%.3f obs=%.3f conf=%.3f "
            "ev=%.3f | weights_sum=%.2f => signal=%.3f",
            ev,
            ev_score,
            contributions["buffer"],
            contributions["obs"],
            contributions["conf"],
            contributions["ev"],
            weights_sum,
            signal_score,
        )
        return signal_score

    def _required_edge(self, market: KalshiMarket) -> float:
        """Return the market-specific minimum edge threshold."""
        ticker_text = f"{market.series_ticker} {market.ticker}".upper()
        return self.denver_min_edge if "DEN" in ticker_text else self.min_edge

    def _model_probability_for_side(self, nws_probability_yes: float, side: str) -> float:
        """Return model probability for the proposed side."""
        return nws_probability_yes if side == "yes" else 1 - nws_probability_yes

    def _confidence_for_side(self, nws_probability_yes: float, side: str) -> float:
        """Use the side's probability as confidence (larger buffer -> further from 0.5)."""
        return nws_probability_yes if side == "yes" else 1 - nws_probability_yes

    def _nws_agrees(
        self,
        temperature_f: float | None,
        threshold: float,
        market_type: str,
        side: str,
        *,
        strike_type: str = "greater",
        floor_strike: float | None = None,
        cap_strike: float | None = None,
    ) -> bool:
        """Return True when NWS points in the same YES/NO direction."""
        if temperature_f is None:
            return False
        if strike_type == "between" and floor_strike is not None and cap_strike is not None:
            inside = floor_strike <= temperature_f <= cap_strike
            return inside if side == "yes" else not inside
        nws_yes = temperature_f > threshold if market_type == "high" else temperature_f < threshold
        return nws_yes if side == "yes" else not nws_yes

    def _skip(
        self,
        reason: str,
        market_type: str,
        threshold: float | None,
        hours_until_settlement: float | None,
        *,
        side: str | None = None,
        ask_price: float | None = None,
        nws_probability_yes: float | None = None,
        nws_temp: float | None = None,
        ladder_multiplier: float = 1.0,
        ev: float | None = None,
        imbalance_score: float = 0.5,
    ) -> EdgeDecision:
        """Build a detailed skip decision for early filters."""
        return EdgeDecision(
            False,
            side,
            ask_price,
            self.limit_price(side, ask_price) if side and ask_price else None,
            None,
            None,
            0.0,
            reason,
            market_type,
            threshold,
            nws_probability_yes,
            nws_temp,
            hours_until_settlement,
            0.5,
            0.5,
            ladder_multiplier,
            0.5,
            ev,
            imbalance_score,
        )

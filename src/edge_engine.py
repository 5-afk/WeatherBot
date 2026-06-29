"""Strategy math for turning forecasts and prices into a trade candidate."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from src.kalshi_client import KalshiMarket
from src.weather_client import EnsembleForecast, NwsForecast


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
    gfs_probability_yes: float | None
    ecmwf_probability_yes: float | None
    nws_adjusted_temperature_f: float | None
    hours_until_settlement: float | None
    buffer_score: float = 0.5
    observation_score: float = 0.5
    icon_probability_yes: float | None = None
    ladder_multiplier: float = 1.0
    signal_score: float = 0.5


class EdgeEngine:
    """Apply the bot's price, timing, confidence, and forecast filters."""

    def __init__(self) -> None:
        """Read edge-engine thresholds from environment variables."""
        self.min_edge = float(os.getenv("MIN_EDGE", "0.10"))
        self.min_confidence = float(os.getenv("MIN_CONFIDENCE", "0.70"))
        self.min_price = float(os.getenv("MIN_CONTRACT_PRICE", "0.30"))
        self.max_price = float(os.getenv("MAX_CONTRACT_PRICE", "0.85"))
        self.denver_min_edge = float(os.getenv("DENVER_MIN_EDGE", "0.15"))
        self.settlement_window_hours = float(os.getenv("SETTLEMENT_WINDOW_HOURS", "24"))
        self.nws_warm_bias_f = float(os.getenv("NWS_SUMMER_HIGH_WARM_BIAS_F", "1.5"))
        self.expected_members = {
            "gfs": int(os.getenv("EXPECTED_GFS_MEMBERS", "31")),
            "ecmwf": int(os.getenv("EXPECTED_ECMWF_MEMBERS", "51")),
        }

    def evaluate(
        self,
        market: KalshiMarket,
        *,
        gfs: EnsembleForecast,
        ecmwf: EnsembleForecast,
        nws: NwsForecast,
        icon: EnsembleForecast | None = None,
        current_temp_f: float | None = None,
        ladder_multiplier: float = 1.0,
    ) -> EdgeDecision:
        """Return a complete GO/skip decision before Claude and risk checks."""
        market_type = self.market_type(market)
        threshold = self.parse_threshold(market)
        settlement_time = market.settlement_time or market.close_time
        hours_until_settlement = self.hours_until_settlement(settlement_time)

        if threshold is None:
            return self._skip("Could not parse one temperature threshold.", market_type, None, hours_until_settlement)
        if hours_until_settlement is None or not -1 <= hours_until_settlement <= self.settlement_window_hours:
            return self._skip("Contract is not within 24 hours of settlement.", market_type, threshold, hours_until_settlement)

        member_problem = self._member_count_problem(gfs, ecmwf)
        if member_problem:
            return self._skip(member_problem, market_type, threshold, hours_until_settlement)

        gfs_yes = self._probability_yes(gfs.member_temperatures_f, threshold, market_type)
        ecmwf_yes = self._probability_yes(ecmwf.member_temperatures_f, threshold, market_type)
        if gfs_yes is None or ecmwf_yes is None:
            return self._skip("No ensemble members matched the settlement date.", market_type, threshold, hours_until_settlement)

        gfs_side = "yes" if gfs_yes >= 0.5 else "no"
        ecmwf_side = "yes" if ecmwf_yes >= 0.5 else "no"
        if gfs_side != ecmwf_side:
            return EdgeDecision(
                False,
                None,
                None,
                None,
                None,
                None,
                self._confidence_for_side(gfs_yes, ecmwf_yes, gfs_side),
                f"GFS says {gfs_side.upper()} but ECMWF says {ecmwf_side.upper()}.",
                market_type,
                threshold,
                gfs_yes,
                ecmwf_yes,
                self._adjust_nws_temperature(nws.temperature_f, market_type, settlement_time),
                hours_until_settlement,
            )

        side = gfs_side
        ask_price = market.yes_ask if side == "yes" else market.no_ask
        if ask_price is None:
            return self._skip(f"Missing {side.upper()} ask price.", market_type, threshold, hours_until_settlement)
        if not self.min_price <= ask_price <= self.max_price:
            return self._skip(
                f"Market price ${ask_price:.2f} is outside ${self.min_price:.2f}-${self.max_price:.2f}.",
                market_type,
                threshold,
                hours_until_settlement,
                side=side,
                ask_price=ask_price,
                gfs_yes=gfs_yes,
                ecmwf_yes=ecmwf_yes,
            )
        if not self._check_liquidity(market):
            return self._skip(
                "Insufficient liquidity at ask price.",
                market_type,
                threshold,
                hours_until_settlement,
                side=side,
                ask_price=ask_price,
                gfs_yes=gfs_yes,
                ecmwf_yes=ecmwf_yes,
                ladder_multiplier=ladder_multiplier,
            )

        nws_adjusted = self._adjust_nws_temperature(nws.temperature_f, market_type, settlement_time)
        buffer_score = self._temperature_buffer_score(nws_adjusted, threshold, market_type, side)
        if buffer_score == 0.0:
            return EdgeDecision(
                False,
                side,
                ask_price,
                self.limit_price(side, ask_price),
                None,
                None,
                self._confidence_for_side(gfs_yes, ecmwf_yes, side),
                "Temperature buffer is on the wrong side of threshold.",
                market_type,
                threshold,
                gfs_yes,
                ecmwf_yes,
                nws_adjusted,
                hours_until_settlement,
                buffer_score,
                0.5,
                None,
                ladder_multiplier,
                0.0,
            )
        if not self._nws_agrees(nws_adjusted, threshold, market_type, side):
            return EdgeDecision(
                False,
                side,
                ask_price,
                self.limit_price(side, ask_price),
                None,
                None,
                self._confidence_for_side(gfs_yes, ecmwf_yes, side),
                "NWS forecast does not confirm the ensemble direction.",
                market_type,
                threshold,
                gfs_yes,
                ecmwf_yes,
                nws_adjusted,
                hours_until_settlement,
                buffer_score,
                0.5,
                None,
                ladder_multiplier,
                0.0,
            )

        model_probability = self._model_probability_for_side(gfs_yes, ecmwf_yes, side)
        edge = model_probability - ask_price
        confidence = self._confidence_for_side(gfs_yes, ecmwf_yes, side)
        icon_yes = self._probability_yes(icon.member_temperatures_f, threshold, market_type) if icon else None
        if icon_yes is not None:
            icon_side = "yes" if icon_yes >= 0.5 else "no"
            if icon_side != side:
                confidence *= 0.7
            else:
                confidence = min(confidence * 1.15, 0.99)
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
        signal_score = self._combined_signal_score(buffer_score, observation_score, confidence, edge)
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
            gfs_yes,
            ecmwf_yes,
            nws_adjusted,
            hours_until_settlement,
            buffer_score,
            observation_score,
            icon_yes,
            ladder_multiplier,
            signal_score,
        )
        pre_claude_reason = self.pre_claude_gate_failure(pre_claude_decision)
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
                gfs_yes,
                ecmwf_yes,
                nws_adjusted,
                hours_until_settlement,
                buffer_score,
                observation_score,
                icon_yes,
                ladder_multiplier,
                signal_score,
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
                gfs_yes,
                ecmwf_yes,
                nws_adjusted,
                hours_until_settlement,
                buffer_score,
                observation_score,
                icon_yes,
                ladder_multiplier,
                signal_score,
            )
        if confidence <= self.min_confidence:
            return EdgeDecision(
                False,
                side,
                ask_price,
                self.limit_price(side, ask_price),
                model_probability,
                edge,
                confidence,
                f"Confidence {confidence:.1%} is below 70% threshold.",
                market_type,
                threshold,
                gfs_yes,
                ecmwf_yes,
                nws_adjusted,
                hours_until_settlement,
                buffer_score,
                observation_score,
                icon_yes,
                ladder_multiplier,
                signal_score,
            )
        return EdgeDecision(
            True,
            side,
            ask_price,
            self.limit_price(side, ask_price),
            model_probability,
            edge,
            confidence,
            "Strong ensemble agreement with NWS confirmation.",
            market_type,
            threshold,
            gfs_yes,
            ecmwf_yes,
            nws_adjusted,
            hours_until_settlement,
            buffer_score,
            observation_score,
            icon_yes,
            ladder_multiplier,
            signal_score,
        )

    def pre_claude_gate_failure(self, decision: EdgeDecision) -> str | None:
        """Return a reason when the strict pre-Claude gate should block the trade."""
        if decision.signal_score < 0.75:
            return f"signal_score {decision.signal_score:.2f} < 0.75"
        if decision.edge is None or decision.edge < 0.15:
            edge_text = "n/a" if decision.edge is None else f"{decision.edge:.1%}"
            return f"edge {edge_text} < 15.0%"
        if decision.confidence < 0.80:
            return f"confidence {decision.confidence:.1%} < 80.0%"
        if decision.buffer_score < 0.50:
            return f"buffer_score {decision.buffer_score:.2f} < 0.50"
        if decision.hours_until_settlement is None:
            return "hours_until_settlement unavailable"
        if decision.hours_until_settlement > 20:
            return f"hours_until_settlement {decision.hours_until_settlement:.1f} > 20"
        return None

    def market_type(self, market: KalshiMarket) -> str:
        """Infer whether a market resolves on a daily high or low."""
        ticker_text = f"{market.series_ticker} {market.ticker}".upper()
        if "KXLOW" in ticker_text:
            return "low"
        if "KXHIGH" in ticker_text:
            return "high"
        return "low" if re.search(r"\blow\b", f"{market.title} {market.subtitle}".lower()) else "high"

    def parse_threshold(self, market: KalshiMarket) -> float | None:
        """Extract one Fahrenheit threshold from the market title/subtitle."""
        market_type = self.market_type(market)
        text = f"{market.title} {market.subtitle}"
        # Only match numbers that look like temperatures (not dates or IDs)
        matches = re.findall(
            r"(?<![A-Z0-9])(-?\d+(?:\.\d+)?)\s*(?:°|degrees?|F\b|fahrenheit\b)",
            text,
            re.IGNORECASE,
        )
        if not matches:
            # Fallback: find the last standalone number in the subtitle only
            matches = re.findall(r"(?<![A-Z0-9])(\d+(?:\.\d+)?)(?![A-Z0-9])", market.subtitle, re.IGNORECASE)
        if not matches:
            return None

        threshold = float(matches[-1])
        # KXHIGH markets can never have negative thresholds in summer cities
        # Negative values indicate a parsing error — return None to skip
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
        """Return a limit price one cent better than the current ask."""
        del side
        return round(max(0.01, ask_price - 0.01), 2)

    def _temperature_buffer_score(
        self,
        nws_temp: float | None,
        threshold: float,
        market_type: str,
        side: str
    ) -> float:
        """
        Score 0.0-1.0 based on how far the forecast is from the threshold.
        Larger buffer = higher score = more confident signal.
        Buffer of 5F+ = 1.0 (near certain)
        Buffer of 3-5F = 0.8 (strong)
        Buffer of 1-3F = 0.3 (too close for the strict pre-Claude gate)
        Buffer of 0-1F = 0.1 (too close - noise erases edge)
        """
        if nws_temp is None:
            return 0.5
        if market_type == "high":
            buffer = nws_temp - threshold if side == "yes" else threshold - nws_temp
        else:
            buffer = threshold - nws_temp if side == "yes" else nws_temp - threshold
        if buffer >= 5.0:
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
        hours_until_settlement: float | None
    ) -> float:
        """
        Score based on current live temperature vs threshold.
        Only meaningful when within 8 hours of settlement.
        If already past threshold and settling in <4 hours = near certainty.
        """
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
        """
        Sum all YES ask prices for a city's temperature brackets.
        Perfect market = 1.0. Under 0.92 = whole market underpriced.
        Over 1.08 = whole market overpriced.
        Returns a multiplier: underpriced -> increase position size
        Overpriced -> decrease position size or skip
        """
        if not all_market_prices:
            return 1.0
        total = sum(all_market_prices)
        if total < 0.92:
            return 1.20
        if total > 1.08:
            return 0.80
        return 1.00

    def _check_liquidity(self, market: KalshiMarket) -> bool:
        """
        Only trade markets with meaningful liquidity.
        Checks yes_ask_size_fp from raw market data.
        Skip markets with less than 50 contracts available at ask.
        """
        yes_size = float(market.raw.get("yes_ask_size_fp", 0) or 0)
        no_size = float(market.raw.get("no_ask_size_fp", 0) or 0)
        min_size = float(os.getenv("MIN_LIQUIDITY_CONTRACTS", "50"))
        return yes_size >= min_size or no_size >= min_size

    def _combined_signal_score(
        self,
        buffer_score: float,
        observation_score: float,
        confidence: float,
        edge: float,
    ) -> float:
        """Return the weighted combined signal score used before Claude."""
        signal_score = (
            buffer_score * 0.35 +
            observation_score * 0.25 +
            confidence * 0.25 +
            (edge / 0.45) * 0.15
        )
        return round(min(signal_score, 1.0), 3)

    def _required_edge(self, market: KalshiMarket) -> float:
        """Return the market-specific minimum edge threshold."""
        ticker_text = f"{market.series_ticker} {market.ticker}".upper()
        return self.denver_min_edge if "DEN" in ticker_text else self.min_edge

    def _member_count_problem(self, gfs: EnsembleForecast, ecmwf: EnsembleForecast) -> str | None:
        """Verify Open-Meteo returned the requested 31 and 51 member ensembles."""
        if gfs.member_count < self.expected_members["gfs"]:
            return f"GFS returned {gfs.member_count} members, expected 31."
        if ecmwf.member_count < self.expected_members["ecmwf"]:
            return f"ECMWF returned {ecmwf.member_count} members, expected 51."
        return None

    def _probability_yes(self, values: list[float], threshold: float, market_type: str) -> float | None:
        """Count the fraction of ensemble members that make YES correct."""
        if not values:
            return None
        if market_type == "high":
            winning_members = sum(1 for value in values if value > threshold)
        else:
            winning_members = sum(1 for value in values if value < threshold)
        return winning_members / len(values)

    def _model_probability_for_side(self, gfs_yes: float, ecmwf_yes: float, side: str) -> float:
        """Average GFS and ECMWF probability for the proposed side."""
        probability_yes = (gfs_yes + ecmwf_yes) / 2
        return probability_yes if side == "yes" else 1 - probability_yes

    def _confidence_for_side(self, gfs_yes: float, ecmwf_yes: float, side: str) -> float:
        """Use the weaker model's side probability as the confidence score."""
        if side == "yes":
            return min(gfs_yes, ecmwf_yes)
        return min(1 - gfs_yes, 1 - ecmwf_yes)

    def _adjust_nws_temperature(
        self,
        temperature_f: float | None,
        market_type: str,
        settlement_time: datetime | None,
    ) -> float | None:
        """Apply the 1.5°F warm-bias correction to NWS summer highs."""
        if temperature_f is None:
            return None
        if settlement_time is None or market_type != "high":
            return temperature_f
        if settlement_time.month in {6, 7, 8}:
            return round(temperature_f - self.nws_warm_bias_f, 2)
        return temperature_f

    def _nws_agrees(self, temperature_f: float | None, threshold: float, market_type: str, side: str) -> bool:
        """Return True when NWS points in the same YES/NO direction."""
        if temperature_f is None:
            return False
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
        gfs_yes: float | None = None,
        ecmwf_yes: float | None = None,
        ladder_multiplier: float = 1.0,
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
            gfs_yes,
            ecmwf_yes,
            None,
            hours_until_settlement,
            0.5,
            0.5,
            None,
            ladder_multiplier,
            0.5,
        )

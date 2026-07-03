"""Focused verification for edge gate fixes.

This is a standalone script. It does not call live Kalshi, weather, or Claude APIs.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.edge_engine import EdgeDecision, EdgeEngine, estimate_probability
from src.kalshi_client import KalshiMarket
from src.weather_client import NwsForecast


def test_mia_style_gate_passes() -> None:
    """A strong signal with 0.70 confidence should pass the pre-Claude gate."""
    engine = EdgeEngine()
    decision = EdgeDecision(
        should_trade=True,
        side="yes",
        ask_price=0.30,
        limit_price=0.29,
        model_probability=1.0,
        edge=0.70,
        confidence=0.70,
        reason="synthetic MIA 94F candidate",
        market_type="high",
        threshold_f=94.0,
        nws_probability_yes=1.0,
        nws_forecast_temperature_f=99.0,
        hours_until_settlement=12.0,
        buffer_score=0.8,
        observation_score=0.9,
        ladder_multiplier=1.0,
        signal_score=0.88,
        ev=0.40,
    )
    assert engine.pre_claude_gate_failure(decision) is None


def test_nws_probability_reaches_scoring() -> None:
    """NWS probability estimate should flow through evaluation without short-circuit."""
    engine = EdgeEngine()
    settlement = datetime.now(timezone.utc) + timedelta(hours=12)
    market = KalshiMarket(
        ticker="KXHIGHMIA-TEST-B94",
        series_ticker="KXHIGHMIA",
        title="Miami high temperature",
        subtitle="94F or above",
        yes_bid=0.29,
        yes_ask=0.30,
        no_bid=0.69,
        no_ask=0.70,
        close_time=settlement,
        settlement_time=settlement,
        raw={"yes_ask_size_fp": 100, "no_ask_size_fp": 100},
    )
    nws = NwsForecast(99.0, "hot", "synthetic")

    decision = engine.evaluate(market, nws=nws)

    assert decision.nws_probability_yes is not None
    assert decision.nws_probability_yes > 0.5
    assert decision.signal_score != 0.5


def test_estimate_probability_high() -> None:
    """Forecast 95F vs threshold 93F should yield ~72% YES for HIGH market."""
    prob = estimate_probability(95.0, 93.0, "HIGH")
    assert 0.68 <= prob <= 0.76, f"expected ~0.72, got {prob}"


def test_estimate_probability_low_side() -> None:
    """Forecast 95F vs threshold 97F should yield ~28% YES for HIGH market."""
    prob = estimate_probability(95.0, 97.0, "HIGH")
    assert 0.24 <= prob <= 0.32, f"expected ~0.28, got {prob}"


if __name__ == "__main__":
    test_mia_style_gate_passes()
    test_nws_probability_reaches_scoring()
    test_estimate_probability_high()
    test_estimate_probability_low_side()
    print("edge gate verification passed")

"""Focused verification for edge gate fixes.

This is a standalone script. It does not call live Kalshi, weather, or Claude APIs.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.edge_engine import EdgeDecision, EdgeEngine
from src.kalshi_client import KalshiMarket
from src.weather_client import EnsembleForecast, NwsForecast


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
        gfs_probability_yes=1.0,
        ecmwf_probability_yes=1.0,
        nws_adjusted_temperature_f=99.0,
        hours_until_settlement=12.0,
        buffer_score=0.8,
        observation_score=0.9,
        icon_probability_yes=1.0,
        ladder_multiplier=1.0,
        signal_score=0.88,
        ev=0.40,
    )
    assert engine.pre_claude_gate_failure(decision) is None


def test_model_disagreement_reaches_scoring() -> None:
    """GFS/ECMWF disagreement should lower score, not short-circuit evaluation."""
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
    gfs = EnsembleForecast("gfs", [96.0] * 25 + [92.0] * 6, 95.2, 31)
    ecmwf = EnsembleForecast("ecmwf", [96.0] * 20 + [92.0] * 31, 93.6, 51)
    nws = NwsForecast(99.0, "hot", "synthetic")

    decision = engine.evaluate(market, gfs=gfs, ecmwf=ecmwf, nws=nws)

    assert "GFS says" not in decision.reason
    assert "ECMWF says" not in decision.reason
    assert round(decision.gfs_probability_yes or 0.0, 2) == 0.81
    assert round(decision.ecmwf_probability_yes or 0.0, 2) == 0.39
    assert decision.signal_score != 0.5


if __name__ == "__main__":
    test_mia_style_gate_passes()
    test_model_disagreement_reaches_scoring()
    print("edge gate verification passed")

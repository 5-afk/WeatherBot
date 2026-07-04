"""Paper-trade harness for the Kalshi weather bot.

This script never calls Kalshi's order endpoint. It uses mock Kalshi markets,
real NWS gridded weather data, the real edge engine, the real Claude checker,
and the real position sizer to show what the bot would do in dry-run mode.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv

from src.claude_checker import ClaudeChecker, ClaudeDecision
from src.data_enricher import DataEnricher
from src.edge_engine import EdgeDecision, EdgeEngine
from src.kalshi_client import KalshiMarket
from src.position_sizer import PositionSize, PositionSizer
from src.risk_manager import RiskManager
from src.weather_client import CityConfig, NwsForecast, WeatherClient


PROJECT_ROOT = Path(__file__).resolve().parent


def build_mock_markets() -> list[dict[str, object]]:
    """Create realistic mid-range mock Kalshi weather markets."""
    return [
        {
            "ticker": "KXHIGHNY-26JUN28-B75",
            "series_ticker": "KXHIGHNY",
            "title": "High temp NYC",
            "subtitle": "75F or above",
            "yes_ask_dollars": 0.65,
            "no_ask_dollars": 0.35,
            "yes_bid_dollars": 0.63,
            "no_bid_dollars": 0.33,
            "close_time": (datetime.now(timezone.utc) + timedelta(hours=18)).isoformat(),
            "settlement_time": (datetime.now(timezone.utc) + timedelta(hours=18)).isoformat(),
        },
        {
            "ticker": "KXHIGHCHI-26JUN28-B80",
            "series_ticker": "KXHIGHCHI",
            "title": "High temp Chicago",
            "subtitle": "80F or above",
            "yes_ask_dollars": 0.42,
            "no_ask_dollars": 0.58,
            "yes_bid_dollars": 0.40,
            "no_bid_dollars": 0.56,
            "close_time": (datetime.now(timezone.utc) + timedelta(hours=20)).isoformat(),
            "settlement_time": (datetime.now(timezone.utc) + timedelta(hours=20)).isoformat(),
        },
        {
            "ticker": "KXHIGHMIA-26JUN28-B88",
            "series_ticker": "KXHIGHMIA",
            "title": "High temp Miami",
            "subtitle": "88F or above",
            "yes_ask_dollars": 0.55,
            "no_ask_dollars": 0.45,
            "yes_bid_dollars": 0.53,
            "no_bid_dollars": 0.43,
            "close_time": (datetime.now(timezone.utc) + timedelta(hours=16)).isoformat(),
            "settlement_time": (datetime.now(timezone.utc) + timedelta(hours=16)).isoformat(),
        },
    ]


def parse_time(value: object) -> datetime | None:
    """Parse an ISO timestamp into a timezone-aware UTC datetime."""
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def market_from_mock(raw: dict[str, object]) -> KalshiMarket:
    """Convert one mock market dictionary into a KalshiMarket dataclass."""
    return KalshiMarket(
        ticker=str(raw.get("ticker", "")),
        series_ticker=str(raw.get("series_ticker", "")),
        title=str(raw.get("title", "")),
        subtitle=str(raw.get("subtitle", "")),
        yes_bid=float(raw["yes_bid_dollars"]),
        yes_ask=float(raw["yes_ask_dollars"]),
        no_bid=float(raw["no_bid_dollars"]),
        no_ask=float(raw["no_ask_dollars"]),
        close_time=parse_time(raw.get("close_time")),
        settlement_time=parse_time(raw.get("settlement_time")),
        raw=raw,
    )


def find_city(weather: WeatherClient, market: KalshiMarket) -> CityConfig | None:
    """Find the configured city whose KXHIGH/KXLOW series matches the market."""
    for city in weather.watched_cities():
        if market.series_ticker in {city.high_series, city.low_series}:
            return city
    return None


def claude_payload(
    city: CityConfig,
    market: KalshiMarket,
    decision: EdgeDecision,
    nws: NwsForecast,
    enrichment: dict[str, object],
) -> dict[str, object]:
    """Build the data packet that Claude receives for a proposed paper trade."""
    settlement = market.settlement_time or market.close_time
    return {
        "city": city.name,
        "date": settlement.isoformat() if settlement else None,
        "temperature_threshold_f": decision.threshold_f,
        "side": decision.side,
        "nws_probability": decision.nws_probability_yes,
        "nws_forecast_f": decision.nws_forecast_temperature_f,
        "nws_short_forecast": nws.short_forecast,
        "market_price": decision.ask_price,
        "limit_price": decision.limit_price,
        "edge_percentage": None if decision.edge is None else round(decision.edge * 100, 2),
        "confidence_score": round(decision.confidence * 100, 2),
        "hours_until_settlement": decision.hours_until_settlement,
        "active_weather_alerts": enrichment.get("active_alerts", []),
        "has_severe_alert": enrichment.get("has_severe_alert", False),
        "current_temp_f": enrichment.get("current_temp_f"),
        "current_observation": enrichment.get("current_observation", {}),
        "web_context": enrichment.get("web_context", ""),
    }


def fmt_pct(value: float | None) -> str:
    """Format a decimal probability as a human-readable percentage."""
    return "n/a" if value is None else f"{value:.1%}"


def fmt_temp(value: float | None) -> str:
    """Format a Fahrenheit temperature for console output."""
    return "n/a" if value is None else f"{value:.1f}F"


def summarize_market(
    *,
    city: CityConfig,
    market: KalshiMarket,
    decision: EdgeDecision,
    nws: NwsForecast,
    claude: ClaudeDecision | None,
    size: PositionSize | None,
    would_bet: bool,
    enrichment: dict[str, object] | None,
) -> None:
    """Print a clear one-market paper-trade summary."""
    print("\n" + "=" * 80)
    print(f"Market: {market.ticker}")
    print(f"City: {city.name}")
    print(f"Threshold: {fmt_temp(decision.threshold_f)}")
    print(f"NWS probability YES: {fmt_pct(decision.nws_probability_yes)}")
    print(f"NWS forecast: {fmt_temp(nws.temperature_f)} ({nws.short_forecast})")
    print(f"Edge: {fmt_pct(decision.edge)}")
    print(f"Confidence: {fmt_pct(decision.confidence)}")
    print(f"Current temp: {fmt_temp(enrichment.get('current_temp_f') if enrichment else None)}")
    print(
        "Active alerts: "
        f"{enrichment.get('alert_count', 0) if enrichment else 0} "
        f"({enrichment.get('has_severe_alert', False) if enrichment else False})"
    )
    print(f"Web context: {enrichment.get('web_context', 'n/a') if enrichment else 'n/a'}")
    print(f"Claude decision: {claude.decision if claude else 'NOT_CALLED'}")
    print(f"Claude reason: {claude.reason if claude else 'Edge filters did not pass.'}")
    print(f"Would bet: {'YES' if would_bet else 'NO'}")
    if size:
        print(f"Paper size: ${size.stake:.2f}, contracts={size.contracts}, Kelly=${size.kelly_size:.2f}")
    print(f"Strategy reason: {decision.reason}")


def main() -> None:
    """Run the paper-trade simulation from mock markets and real weather data."""
    load_dotenv(PROJECT_ROOT / ".env")
    os.environ["DRY_RUN"] = "true"

    weather = WeatherClient()
    enricher = DataEnricher()
    edge_engine = EdgeEngine()
    claude_checker = ClaudeChecker()
    position_sizer = PositionSizer()
    risk_manager = RiskManager(PROJECT_ROOT / "data" / "paper_positions.db")

    paper_trades: list[tuple[KalshiMarket, EdgeDecision, PositionSize]] = []

    for raw in build_mock_markets():
        market = market_from_mock(raw)
        city = find_city(weather, market)
        if city is None:
            print(f"\nMarket: {market.ticker}\nCity: n/a\nWould bet: NO\nReason: No matching configured city.")
            continue

        settlement_time = market.settlement_time or market.close_time
        if settlement_time is None:
            print(f"\nMarket: {market.ticker}\nCity: {city.name}\nWould bet: NO\nReason: Missing settlement time.")
            continue

        market_type = edge_engine.market_type(market)
        target_date = settlement_time.astimezone(timezone.utc).date()

        try:
            nws = weather.get_station_forecast(city, target_date, market_type)
        except Exception as exc:
            print(f"\nMarket: {market.ticker}\nCity: {city.name}\nWould bet: NO\nReason: Weather fetch failed: {exc}")
            continue

        decision = edge_engine.evaluate(market, nws=nws)
        claude: ClaudeDecision | None = None
        size: PositionSize | None = None
        would_bet = False
        enrichment: dict[str, object] | None = None

        if decision.should_trade:
            enrichment = enricher.enrich(city, target_date.isoformat())
            payload = claude_payload(city, market, decision, nws, enrichment)
            claude = claude_checker.check(payload)
            if claude.approved:
                mock_market_price = float(raw["yes_ask_dollars"])
                trade_price = decision.limit_price or decision.ask_price or mock_market_price
                size = position_sizer.size_trade(
                    win_probability=min(decision.model_probability or 0.99, 0.99),
                    price=trade_price,
                    confidence=decision.confidence,
                    current_budget=risk_manager.get_todays_budget(),
                    previous_payout=0.0,
                    last_trade_won=False,
                )
                risk_check = risk_manager.can_trade(market.ticker, size.stake)
                would_bet = size.contracts > 0 and risk_check.allowed
                if not risk_check.allowed:
                    claude = ClaudeDecision(claude.decision, f"{claude.reason} Risk check: {risk_check.reason}")
                if would_bet:
                    paper_trades.append((market, decision, size))

        summarize_market(
            city=city,
            market=market,
            decision=decision,
            nws=nws,
            claude=claude,
            size=size,
            would_bet=would_bet,
            enrichment=enrichment,
        )

    print("\n" + "=" * 80)
    print("Paper P&L simulation")
    if not paper_trades:
        print("No paper bets qualified.")
        return

    best_case_profit = 0.0
    realistic_expected_profit = 0.0
    for _, decision, size in paper_trades:
        win_profit = size.contracts - size.stake
        loss_amount = size.stake
        best_case_profit += win_profit
        realistic_expected_profit += 0.67 * win_profit - 0.33 * loss_amount
        print(
            f"{decision.side.upper()} @ ${decision.limit_price:.2f}: "
            f"stake=${size.stake:.2f}, win_profit=${win_profit:.2f}"
        )

    print(f"Best case, all bets win: ${best_case_profit:.2f}")
    print(f"Realistic case, 67% win rate: ${realistic_expected_profit:.2f}")


if __name__ == "__main__":
    main()

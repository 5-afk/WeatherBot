"""
Contract validation layer for WhetherBot.

Runs after market fetch, before any math evaluation.
Cross-validates API data against rules_primary text to catch mismatches
that would cause the pipeline to produce confidently wrong answers.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

VALIDATION_CHECK_COUNT = 10


@dataclass(frozen=True)
class ValidationResult:
    """Outcome of contract validation with optional corrected values."""

    valid: bool
    reason: str
    confirmed_station: str | None = None
    confirmed_threshold: float | None = None
    confirmed_upper_threshold: float | None = None
    confirmed_strike_type: str | None = None
    confirmed_market_type: str | None = None
    confirmed_hours_until_close: float | None = None
    confirmed_settlement_source: str | None = None


class ContractValidator:
    """Validates math inputs match contract rules before evaluation begins."""

    NWS_PHRASES = [
        "national weather service",
        "nws climatological",
        "daily climate report",
        "cli report",
        "nws daily",
    ]

    STRIKE_TYPE_PHRASES = {
        "greater": ["greater than", "strictly greater", "exceeds", "above"],
        "less": ["less than", "strictly less", "below", "under"],
        "between": ["between", "greater than or equal", "from", "inclusive"],
    }

    TICKER_MARKET_TYPE = {
        "KXHIGH": "HIGH",
        "KXLOW": "LOW",
        "KXLOWT": "LOW",
    }

    CITY_HINTS = {
        "KLAX": ["los angeles", "lax", "l.a."],
        "KNYC": ["new york", "central park", "nyc"],
        "KMDW": ["chicago", "midway"],
        "KMIA": ["miami"],
        "KDEN": ["denver"],
        "KOKC": ["oklahoma"],
        "KBOS": ["boston"],
        "KDCA": ["washington", "reagan", "national airport"],
        "KSEA": ["seattle"],
        "KSFO": ["san francisco", "sfo"],
        "KATL": ["atlanta"],
        "KDFW": ["dallas", "fort worth", "dfw"],
        "KMSP": ["minneapolis"],
    }

    def validate(
        self,
        ticker: str,
        market: dict,
        parsed_station: str | None,
        forecast_station: str | None,
        threshold: float | None,
        upper_threshold: float | None,
    ) -> ValidationResult:
        """Run all validation checks and return pass/fail with confirmed values."""
        rules = str(market.get("rules_primary", ""))
        rules_lower = rules.lower()
        strike_type = str(market.get("strike_type", "")).lower()
        floor_strike = market.get("floor_strike")
        cap_strike = market.get("cap_strike")
        last_trading_time = market.get("close_time") or market.get("last_trading_time")
        status = str(market.get("status", "")).lower()

        if status not in ("active", "open"):
            return ValidationResult(
                valid=False,
                reason=f"Market status '{status}' is not tradeable",
            )

        hours_until_close: float | None = None
        if last_trading_time:
            try:
                close_dt = datetime.fromisoformat(str(last_trading_time).replace("Z", "+00:00"))
                now_utc = datetime.now(timezone.utc)
                hours_until_close = (close_dt - now_utc).total_seconds() / 3600
                if hours_until_close <= 0:
                    return ValidationResult(
                        valid=False,
                        reason=f"Market closed at {last_trading_time} — no longer accepting orders",
                    )
            except Exception:
                pass

        if rules and not any(phrase in rules_lower for phrase in self.NWS_PHRASES):
            return ValidationResult(
                valid=False,
                reason="Contract does not settle on NWS data — cannot forecast reliably",
            )
        settlement_source = "NWS Daily Climate Report" if rules else "Unknown"

        market_type_from_ticker: str | None = None
        for prefix, mtype in self.TICKER_MARKET_TYPE.items():
            if ticker.startswith(prefix):
                market_type_from_ticker = mtype
                break
        if market_type_from_ticker is None:
            return ValidationResult(
                valid=False,
                reason=f"Cannot determine market type from ticker '{ticker}'",
            )

        if rules and strike_type:
            expected_phrases = self.STRIKE_TYPE_PHRASES.get(strike_type, [])
            if expected_phrases and not any(p in rules_lower for p in expected_phrases):
                return ValidationResult(
                    valid=False,
                    reason=(
                        f"strike_type '{strike_type}' not confirmed by rules text — "
                        "possible API mismatch"
                    ),
                )

        if parsed_station and forecast_station:
            if parsed_station != forecast_station:
                return ValidationResult(
                    valid=False,
                    reason=(
                        f"Station mismatch: rules say '{parsed_station}' but forecast uses "
                        f"'{forecast_station}' — would produce wrong probability"
                    ),
                )
        if not parsed_station:
            return ValidationResult(
                valid=False,
                reason="Cannot determine settlement station from rules_primary — cannot fetch correct forecast",
            )

        confirmed_threshold = threshold
        confirmed_upper = upper_threshold

        if floor_strike is not None:
            try:
                api_floor = float(floor_strike)
                # ATLAS-NOTE: For between markets always prefer API floor_strike.
                # Title/subtitle parsing takes the last number in "89-90°", so
                # threshold often equals cap_strike and the old abs(diff)>1.0
                # guard never corrected a 1°F off-by-one (90 vs 89).
                if strike_type == "between":
                    if threshold is not None and abs(threshold - api_floor) > 0.01:
                        # Common: title "89-90°" parses as 90 while floor_strike is 89.
                        log_fn = logging.warning if abs(threshold - api_floor) > 1.0 else logging.debug
                        log_fn(
                            "[VALIDATOR] Threshold mismatch for %s: "
                            "parsed=%.1f API floor_strike=%.1f — using API value",
                            ticker,
                            threshold,
                            api_floor,
                        )
                    confirmed_threshold = api_floor
                elif threshold is not None and abs(threshold - api_floor) > 1.0:
                    logging.warning(
                        "[VALIDATOR] Threshold mismatch for %s: "
                        "parsed=%.1f API floor_strike=%.1f — using API value",
                        ticker,
                        threshold,
                        api_floor,
                    )
                    confirmed_threshold = api_floor
            except (ValueError, TypeError):
                pass

        if cap_strike is not None and strike_type == "between":
            try:
                api_cap = float(cap_strike)
                if confirmed_threshold is None or api_cap > confirmed_threshold:
                    if upper_threshold is not None and abs(upper_threshold - api_cap) > 1.0:
                        logging.warning(
                            "[VALIDATOR] Upper threshold mismatch for %s: "
                            "parsed=%.1f API cap_strike=%.1f — using API value",
                            ticker,
                            upper_threshold,
                            api_cap,
                        )
                    confirmed_upper = api_cap
                else:
                    # Equal/inverted API floor & cap — clear so derivation runs below.
                    confirmed_upper = None
            except (ValueError, TypeError):
                pass

        if strike_type == "between":
            if confirmed_threshold is None:
                return ValidationResult(
                    valid=False,
                    reason="Between market missing floor_strike",
                )

            # If cap equals floor (Kalshi API bug/change) or is missing, derive from ticker.
            # Example: KXHIGHNY-26AUG08-B89.5 → center 89.5 → bracket [89, 90].
            if confirmed_upper is None or confirmed_upper <= confirmed_threshold:
                bracket_match = re.search(r"-B(\d+(?:\.\d+)?)", ticker)
                if not bracket_match:
                    return ValidationResult(
                        valid=False,
                        reason=f"Between market {ticker} cannot determine bracket bounds",
                    )
                center = float(bracket_match.group(1))
                # Half-degree centers (B89.5): [center-0.5, center+0.5]
                # Integer centers (B90): [center, center+1]
                if center != int(center):
                    confirmed_threshold = center - 0.5
                    confirmed_upper = center + 0.5
                else:
                    confirmed_threshold = float(center)
                    confirmed_upper = float(center) + 1.0
                logging.info(
                    "[VALIDATOR] Derived bracket floor=%.1f cap=%.1f from ticker %s "
                    "(API floor/cap equal or missing; center=%.1f)",
                    confirmed_threshold,
                    confirmed_upper,
                    ticker,
                    center,
                )

            if confirmed_upper <= confirmed_threshold:
                return ValidationResult(
                    valid=False,
                    reason=(
                        f"Between market has inverted bracket: "
                        f"floor={confirmed_threshold} cap={confirmed_upper}"
                    ),
                )

        if strike_type not in ("greater", "less", "between"):
            return ValidationResult(
                valid=False,
                reason=f"Unknown strike_type '{strike_type}' — no valid probability formula available",
            )

        if rules and parsed_station in self.CITY_HINTS:
            hints = self.CITY_HINTS[parsed_station]
            if not any(hint in rules_lower for hint in hints):
                return ValidationResult(
                    valid=False,
                    reason=(
                        f"Rules text does not mention expected city for station "
                        f"{parsed_station} — possible wrong market"
                    ),
                )

        logging.debug(
            "[VALIDATOR] %s | station=%s | strike_type=%s | threshold=%.1f%s | "
            "market_type=%s | hours_until_close=%s",
            ticker,
            parsed_station,
            strike_type,
            confirmed_threshold or 0,
            f"-{confirmed_upper:.1f}" if confirmed_upper else "",
            market_type_from_ticker,
            f"{hours_until_close:.1f}h" if hours_until_close else "unknown",
        )

        return ValidationResult(
            valid=True,
            reason="All validation checks passed",
            confirmed_station=parsed_station,
            confirmed_threshold=confirmed_threshold,
            confirmed_upper_threshold=confirmed_upper,
            confirmed_strike_type=strike_type,
            confirmed_market_type=market_type_from_ticker,
            confirmed_hours_until_close=hours_until_close,
            confirmed_settlement_source=settlement_source,
        )

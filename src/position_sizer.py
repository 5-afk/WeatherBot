"""Position sizing for Kalshi binary contracts."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class PositionSize:
    """Calculated stake, contract count, and sizing explanation."""

    stake: float
    contracts: int
    kelly_size: float
    reason: str


class PositionSizer:
    """Calculate trade size using fractional Kelly and hard risk caps."""

    def __init__(self) -> None:
        """Read sizing settings from environment variables."""
        self.daily_budget = float(os.getenv("DAILY_BUDGET", "100"))
        self.max_bet_usd = float(os.getenv("MAX_BET_USD", os.getenv("MAX_BET_PER_TRADE", "100")))
        self.max_bet_pct = float(os.getenv("MAX_BET_PCT", "1.0"))
        self.kelly_fraction = float(os.getenv("KELLY_FRACTION", "0.15"))
        self.reinvest_pct = float(os.getenv("REINVEST_PCT", "0.75"))

    def size_trade(
        self,
        *,
        win_probability: float,
        price: float,
        confidence: float,
        current_budget: float | None = None,
        previous_payout: float = 0.0,
        last_trade_won: bool = False,
    ) -> PositionSize:
        """Return final stake and contract count for one proposed bet."""
        budget = current_budget or self.daily_budget
        if not 0 < price < 1:
            return PositionSize(0.0, 0, 0.0, "Invalid price.")
        if not 0 < win_probability < 1:
            return PositionSize(0.0, 0, 0.0, "Invalid win probability.")

        # Cap win probability for Kelly math
        win_probability = min(win_probability, 0.99)

        # For one-bet-per-day system: stake = full budget scaled by confidence
        # Confidence multiplier: 0.70 confidence = 77% of budget, 1.0 = 100%
        confidence_multiplier = min(1.0, (confidence - 0.70) / 0.30 * 0.40 + 0.60)
        # Maps: 0.70 confidence -> 60% of budget, 1.0 confidence -> 100% of budget

        stake = round(min(budget * confidence_multiplier, self.max_bet_usd), 2)

        # Still apply Kelly as a sanity floor - never bet more than 3x Kelly suggests
        b = (1 - price) / price
        q = 1 - win_probability
        full_kelly = max(0.0, (win_probability * b - q) / b)
        kelly_size = round(budget * full_kelly * self.kelly_fraction, 2)
        # If Kelly says less than 10% of budget, something is off - reduce stake
        if kelly_size < budget * 0.10:
            stake = round(min(stake, kelly_size * 3), 2)

        contracts = math.floor(stake / price)
        if contracts < 1:
            return PositionSize(0.0, 0, round(kelly_size, 2), "Final stake buys less than one contract.")

        final_stake = round(contracts * price, 2)
        return PositionSize(
            final_stake,
            contracts,
            round(kelly_size, 2),
            f"Budget ${budget:.2f} x confidence {confidence_multiplier:.2f} = ${stake:.2f}, "
            f"Kelly sanity: ${kelly_size:.2f}.",
        )

    def payout_if_win(self, contracts: int) -> float:
        """Return gross payout for a winning Kalshi binary position."""
        return float(contracts)

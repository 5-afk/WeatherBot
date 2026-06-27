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
        self.max_bet_usd = float(os.getenv("MAX_BET_USD", os.getenv("MAX_BET_PER_TRADE", "20")))
        self.max_bet_pct = float(os.getenv("MAX_BET_PCT", "0.05"))
        self.kelly_fraction = float(os.getenv("KELLY_FRACTION", "0.15"))
        self.reinvest_pct = float(os.getenv("REINVEST_PCT", "0.6"))

    def size_trade(
        self,
        *,
        win_probability: float,
        price: float,
        confidence: float,
        previous_payout: float = 0.0,
        last_trade_won: bool = False,
    ) -> PositionSize:
        """Return final stake and contract count for one proposed bet."""
        if not 0 < price < 1:
            return PositionSize(0.0, 0, 0.0, "Invalid price.")
        if not 0 < win_probability < 1:
            return PositionSize(0.0, 0, 0.0, "Invalid win probability.")

        net_payout_ratio = (1 - price) / price
        q = 1 - win_probability
        full_kelly = (win_probability * net_payout_ratio - q) / net_payout_ratio
        fractional_kelly = max(0.0, full_kelly) * self.kelly_fraction
        confidence_multiplier = min(1.0, confidence / 0.90)
        kelly_size = self.daily_budget * fractional_kelly * confidence_multiplier

        if last_trade_won and previous_payout > 0:
            # The compounding chain uses 60% of the last payout, but it still
            # cannot exceed the hard caps below.
            kelly_size = max(kelly_size, previous_payout * self.reinvest_pct)

        hard_cap = min(self.max_bet_usd, self.daily_budget * self.max_bet_pct)
        stake = round(min(kelly_size, hard_cap), 2)
        contracts = math.floor(stake / price)
        if contracts < 1:
            return PositionSize(0.0, 0, round(kelly_size, 2), "Final stake buys less than one contract.")

        final_stake = round(contracts * price, 2)
        return PositionSize(
            final_stake,
            contracts,
            round(kelly_size, 2),
            f"Kelly ${kelly_size:.2f}, capped at ${hard_cap:.2f}, confidence multiplier {confidence_multiplier:.2f}.",
        )

    def payout_if_win(self, contracts: int) -> float:
        """Return gross payout for a winning Kalshi binary position."""
        return float(contracts)

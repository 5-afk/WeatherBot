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
    profit_if_wins: float = 0.0


class PositionSizer:
    """Calculate trade size using fractional Kelly and hard risk caps."""

    def __init__(self) -> None:
        """Read sizing settings from environment variables."""
        self.min_bet_usd = float(os.getenv("MIN_BET_USD", "10.0"))
        self.max_bet_usd = float(os.getenv("MAX_BET_USD", "75.0"))
        self.max_bankroll_deployment = float(os.getenv("MAX_BANKROLL_DEPLOYMENT", "0.70"))
        self.kelly_fraction = float(os.getenv("KELLY_FRACTION", "0.15"))
        self.min_return_multiple = float(os.getenv("MIN_RETURN_MULTIPLE", "2.0"))
        self.min_tradeable_price = float(os.getenv("MIN_TRADEABLE_PRICE", "0.15"))
        self.max_tradeable_price = float(os.getenv("MAX_TRADEABLE_PRICE", "0.50"))

    def size_trade(
        self,
        *,
        win_probability: float,
        price: float,
        confidence: float,
        current_budget: float | None = None,
        ladder_multiplier: float = 1.0,
        previous_payout: float = 0.0,
        last_trade_won: bool = False,
    ) -> PositionSize:
        """Binary Kelly sizing with minimum bet enforcement.

        Formula: f* = (p - c) / (1 - c), then multiply by KELLY_FRACTION.
        """
        del ladder_multiplier, previous_payout, last_trade_won
        budget = current_budget if current_budget is not None else float(os.getenv("DAILY_BUDGET", "100"))

        if price <= 0 or price >= 1:
            return PositionSize(0.0, 0, 0.0, "Invalid price")

        if price < self.min_tradeable_price:
            return PositionSize(
                0.0, 0, 0.0,
                f"Price ${price:.2f} below minimum ${self.min_tradeable_price:.2f}",
            )
        if price > self.max_tradeable_price:
            return PositionSize(
                0.0, 0, 0.0,
                f"Price ${price:.2f} above maximum ${self.max_tradeable_price:.2f}",
            )

        profit_per_dollar = (1.0 - price) / price
        if profit_per_dollar < (self.min_return_multiple - 1.0):
            return PositionSize(
                0.0, 0, 0.0,
                f"Return {profit_per_dollar:.2f}x below minimum {self.min_return_multiple - 1:.1f}x profit on cost",
            )

        edge = win_probability - price
        if edge <= 0:
            return PositionSize(0.0, 0, 0.0, f"No edge: model={win_probability:.2f} price={price:.2f}")

        full_kelly = edge / (1.0 - price)
        fractional_kelly = full_kelly * self.kelly_fraction

        conf_multiplier = max(0.60, min(1.0, (confidence - 0.60) / 0.40 + 0.60))

        raw_stake = fractional_kelly * budget * conf_multiplier
        stake = max(self.min_bet_usd, min(self.max_bet_usd, raw_stake))
        stake = min(stake, budget * self.max_bankroll_deployment)

        if stake < self.min_bet_usd:
            return PositionSize(
                0.0, 0, full_kelly,
                f"Stake ${stake:.2f} below minimum ${self.min_bet_usd:.2f}",
            )

        contracts = math.floor(stake / price)
        if contracts < 1:
            return PositionSize(0.0, 0, full_kelly, "Insufficient stake for even 1 contract")

        final_stake = round(contracts * price, 2)
        profit_if_wins = round(contracts * (1.0 - price), 2)

        min_profit = final_stake * (self.min_return_multiple - 1.0)
        if profit_if_wins < min_profit:
            return PositionSize(
                0.0, 0, full_kelly,
                f"Profit ${profit_if_wins:.2f} below minimum ${min_profit:.2f} required",
            )

        return PositionSize(
            final_stake,
            contracts,
            round(full_kelly, 4),
            "OK",
            profit_if_wins=profit_if_wins,
        )

    def payout_if_win(self, contracts: int) -> float:
        """Return gross payout for a winning Kalshi binary position."""
        return float(contracts)

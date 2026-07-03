"""Hard risk stops and SQLite persistence for the trading bot."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class RiskCheck:
    """Result returned by the pre-trade risk guardrails."""

    allowed: bool
    reason: str
    alert: bool = False


class RiskManager:
    """Enforce every safety rule before the trader can place an order."""

    def __init__(self, db_path: str | Path = "data/positions.db") -> None:
        """Open or create the SQLite database used for positions and P&L."""
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.daily_budget = float(os.getenv("DAILY_BUDGET", "100"))
        self.max_bet_usd = float(os.getenv("MAX_BET_USD", "100"))
        self.max_bet_pct = float(os.getenv("MAX_BET_PCT", "1.0"))
        self.daily_loss_limit = float(os.getenv("DAILY_LOSS_LIMIT", "100"))
        self.monthly_loss_limit = float(os.getenv("MONTHLY_LOSS_LIMIT", "500"))
        self.max_drawdown_pct = float(os.getenv("MAX_DRAWDOWN_PCT", "0.40"))
        self.max_open_positions = int(os.getenv("MAX_OPEN_POSITIONS", "1"))
        self.starting_balance = float(os.getenv("STARTING_BALANCE", "100"))
        self.no_duplicate_tickers = True
        self._init_db()

    def can_trade(self, ticker: str, proposed_stake: float) -> RiskCheck:
        """Check all hard stops before a bet is allowed."""
        if self._get_state("permanent_halt") == "true":
            return RiskCheck(False, "Permanent halt active after drawdown breach.", True)
        if self.no_duplicate_tickers and self.has_ever_traded(ticker):
            return RiskCheck(False, f"No duplicate tickers: {ticker} was already traded.")
        if self.open_position_count() >= self.max_open_positions:
            return RiskCheck(False, f"Max open positions reached ({self.max_open_positions}).")
        if proposed_stake > self.max_bet_usd:
            return RiskCheck(False, f"Stake ${proposed_stake:.2f} exceeds max bet ${self.max_bet_usd:.2f}.")
        todays_budget = self.get_todays_budget()
        if proposed_stake > todays_budget:
            return RiskCheck(False, f"Stake ${proposed_stake:.2f} exceeds today's budget ${todays_budget:.2f}.")
        if proposed_stake > self.get_running_budget() * self.max_bet_pct:
            return RiskCheck(False, "Stake exceeds configured max budget percentage.")

        daily_loss = abs(min(0.0, self.realized_pnl_today()))
        if daily_loss >= self.daily_loss_limit:
            return RiskCheck(False, f"Daily loss limit hit: ${daily_loss:.2f}.", True)

        monthly_loss = abs(min(0.0, self.realized_pnl_month()))
        if monthly_loss >= self.monthly_loss_limit:
            self._set_state("manual_restart_required", "true")
            return RiskCheck(False, f"Monthly loss limit hit: ${monthly_loss:.2f}; manual restart required.", True)

        drawdown = self.drawdown_pct()
        if drawdown >= self.max_drawdown_pct:
            self._set_state("permanent_halt", "true")
            return RiskCheck(False, f"Max drawdown hit: {drawdown:.1%}; permanent halt enabled.", True)

        return RiskCheck(True, "Risk checks passed.")

    def get_todays_budget(self) -> float:
        """Return today's available budget from synced Kalshi balance."""
        base = float(self._get_state("running_budget") or self.starting_balance)
        spent_today = self.opened_notional_today()
        return max(0.0, round(base - spent_today, 2))

    def record_day_end(self, gross_profit: float) -> float:
        """Apply 75% reinvestment rule. Returns amount pocketed."""
        current = float(self._get_state("running_budget") or self.starting_balance)
        if gross_profit >= 0:
            pocketed = round(gross_profit * 0.25, 2)
            reinvested = round(gross_profit * 0.75, 2)
            new_budget = round(current + reinvested, 2)
        else:
            pocketed = 0.0
            reinvested = round(gross_profit, 2)
            new_budget = max(0.0, round(current + gross_profit, 2))
        self._set_state("running_budget", str(new_budget))
        self._set_state("total_pocketed", str(
            round(float(self._get_state("total_pocketed") or 0) + pocketed, 2)
        ))
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO budget_history
                (created_at, gross_profit, reinvested, pocketed, running_budget)
                VALUES (?, ?, ?, ?, ?)
                """,
                (self._now(), gross_profit, reinvested, pocketed, new_budget),
            )
        return pocketed

    def get_total_pocketed(self) -> float:
        """Return the total pocketed profits tracked in risk state."""
        return float(self._get_state("total_pocketed") or 0)

    def get_running_budget(self) -> float:
        """Return the current compounded running budget."""
        return float(self._get_state("running_budget") or self.starting_balance)

    def get_budget_history(self, limit: int = 10) -> list[dict[str, float | str]]:
        """Return recent compounding history rows."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT created_at, gross_profit, reinvested, pocketed, running_budget
                FROM budget_history
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "created_at": str(row[0]),
                "gross_profit": float(row[1]),
                "reinvested": float(row[2]),
                "pocketed": float(row[3]),
                "running_budget": float(row[4]),
            }
            for row in rows
        ]

    def record_open_position(
        self,
        *,
        ticker: str,
        city: str,
        side: str,
        contracts: int,
        price: float,
        stake: float,
        dry_run: bool,
        order_id: str | None,
    ) -> None:
        """Insert a new open position into SQLite."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO positions
                (ticker, city, side, contracts, price, stake, status, dry_run, order_id, opened_at, closed_at, realized_pnl)
                VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, NULL, 0)
                """,
                (ticker, city, side, contracts, price, stake, int(dry_run), order_id, self._now()),
            )

    def record_decision(
        self,
        *,
        ticker: str,
        city: str,
        decision: str,
        reason: str,
        edge: float | None,
        confidence: float | None,
        market_price: float | None,
    ) -> None:
        """Persist every bet or skip decision for auditability."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO decisions
                (created_at, ticker, city, decision, reason, edge, confidence, market_price)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (self._now(), ticker, city, decision, reason, edge, confidence, market_price),
            )

    def record_resolution(self, ticker: str, realized_pnl: float) -> None:
        """Mark an open position closed and store its realized P&L."""
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE positions
                SET status = 'closed', closed_at = ?, realized_pnl = ?
                WHERE ticker = ? AND status = 'open'
                """,
                (self._now(), realized_pnl, ticker),
            )
            conn.execute(
                "INSERT INTO pnl (created_at, ticker, realized_pnl) VALUES (?, ?, ?)",
                (self._now(), ticker, realized_pnl),
            )

    def last_trade_state(self) -> tuple[bool, float]:
        """Return whether the last closed trade won and its gross payout proxy."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT realized_pnl FROM positions
                WHERE status = 'closed'
                ORDER BY closed_at DESC
                LIMIT 1
                """
            ).fetchone()
        if row is None:
            return False, 0.0
        pnl = float(row[0])
        return pnl > 0, max(0.0, pnl)

    def has_ever_traded(self, ticker: str) -> bool:
        """Return True if this ticker has ever been opened by the bot."""
        with self._connect() as conn:
            row = conn.execute("SELECT 1 FROM positions WHERE ticker = ? LIMIT 1", (ticker,)).fetchone()
        return row is not None

    def open_position_count(self) -> int:
        """Count live open positions, excluding dry-run rows.

        Dry-run positions must not consume the live MAX_OPEN_POSITIONS budget.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM positions WHERE status = 'open' AND dry_run = 0"
            ).fetchone()
        return int(row[0])

    def realized_pnl_today(self) -> float:
        """Calculate realized P&L for the current UTC day."""
        today = datetime.now(timezone.utc).date().isoformat()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(realized_pnl), 0) FROM pnl WHERE DATE(created_at) = ?",
                (today,),
            ).fetchone()
        return float(row[0])

    def realized_pnl_month(self) -> float:
        """Calculate realized P&L for the current UTC month."""
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(realized_pnl), 0) FROM pnl WHERE SUBSTR(created_at, 1, 7) = ?",
                (month,),
            ).fetchone()
        return float(row[0])

    def opened_notional_today(self) -> float:
        """Calculate today's total deployed stake."""
        today = datetime.now(timezone.utc).date().isoformat()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(stake), 0) FROM positions WHERE DATE(opened_at) = ?",
                (today,),
            ).fetchone()
        return float(row[0])

    def current_balance(self) -> float:
        """Approximate current balance from starting balance plus realized P&L."""
        with self._connect() as conn:
            row = conn.execute("SELECT COALESCE(SUM(realized_pnl), 0) FROM pnl").fetchone()
        return self.starting_balance + float(row[0])

    def drawdown_pct(self) -> float:
        """Return account drawdown from the configured starting balance."""
        if self.starting_balance <= 0:
            return 0.0
        return max(0.0, (self.starting_balance - self.current_balance()) / self.starting_balance)

    def _init_db(self) -> None:
        """Create database tables if they do not already exist."""
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker TEXT NOT NULL UNIQUE,
                    city TEXT NOT NULL,
                    side TEXT NOT NULL,
                    contracts INTEGER NOT NULL,
                    price REAL NOT NULL,
                    stake REAL NOT NULL,
                    status TEXT NOT NULL,
                    dry_run INTEGER NOT NULL,
                    order_id TEXT,
                    opened_at TEXT NOT NULL,
                    closed_at TEXT,
                    realized_pnl REAL NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    city TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    edge REAL,
                    confidence REAL,
                    market_price REAL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pnl (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    realized_pnl REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS risk_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS budget_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    gross_profit REAL NOT NULL,
                    reinvested REAL NOT NULL,
                    pocketed REAL NOT NULL,
                    running_budget REAL NOT NULL
                )
                """
            )
            conn.execute(
                "INSERT OR IGNORE INTO risk_state (key, value) VALUES ('running_budget', ?)",
                (str(self.starting_balance),),
            )
            conn.execute(
                "INSERT OR IGNORE INTO risk_state (key, value) VALUES ('total_pocketed', '0')"
            )

    def _get_state(self, key: str) -> str | None:
        """Read a persistent risk state value."""
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM risk_state WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row[0])

    def _set_state(self, key: str, value: str) -> None:
        """Write a persistent risk state value."""
        with self._connect() as conn:
            conn.execute("INSERT OR REPLACE INTO risk_state (key, value) VALUES (?, ?)", (key, value))

    def _connect(self) -> sqlite3.Connection:
        """Open a SQLite connection to the positions database."""
        return sqlite3.connect(self.db_path, check_same_thread=False)

    @staticmethod
    def _now() -> str:
        """Return the current UTC timestamp as ISO-8601 text."""
        return datetime.now(timezone.utc).isoformat()

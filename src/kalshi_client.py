"""Kalshi API client for market discovery and limit order placement.

This module deliberately contains no trading strategy. It only knows how to
talk to Kalshi, normalize market payloads, and submit limit orders. Keeping API
code separate makes the beginner-friendly trading pipeline easier to follow.
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import requests


@dataclass(frozen=True)
class KalshiMarket:
    """Normalized subset of a Kalshi market used by the rest of the bot."""

    ticker: str
    series_ticker: str
    title: str
    subtitle: str
    yes_bid: float | None
    yes_ask: float | None
    no_bid: float | None
    no_ask: float | None
    close_time: datetime | None
    settlement_time: datetime | None
    raw: dict[str, Any]


class KalshiClient:
    """Small REST wrapper around the Kalshi endpoints this bot needs."""

    DEMO_URL = "https://demo-api.kalshi.co/trade-api/v2"
    PROD_URL = "https://trading-api.kalshi.com/trade-api/v2"

    def __init__(self) -> None:
        """Create one reusable HTTP session and read credentials from env vars."""
        self.env = os.getenv("KALSHI_ENV", "demo").strip().lower()
        configured_url = os.getenv("KALSHI_API_BASE_URL", "").strip()
        self.base_url = configured_url or (self.PROD_URL if self.env == "prod" else self.DEMO_URL)
        self.base_url = self.base_url.rstrip("/")
        self.api_key = os.getenv("KALSHI_API_KEY", "").strip()
        self.api_secret = os.getenv("KALSHI_API_SECRET", "").strip()
        self.timeout_seconds = 20
        self.session = requests.Session()

    def list_markets(self, series_ticker: str, status: str = "open", limit: int = 200) -> list[KalshiMarket]:
        """Fetch open markets for a single KXHIGH or KXLOW series ticker."""
        payload = self._request(
            "GET",
            "/markets",
            params={"series_ticker": series_ticker, "status": status, "limit": limit},
            auth_required=False,
        )
        markets = payload.get("markets", [])
        return [self._normalize_market(market) for market in markets]

    def list_weather_markets(self, series_tickers: list[str]) -> list[KalshiMarket]:
        """Fetch all configured weather markets without stopping on one failure."""
        all_markets: list[KalshiMarket] = []
        for series_ticker in series_tickers:
            try:
                all_markets.extend(self.list_markets(series_ticker))
            except requests.RequestException as exc:
                # The trader logs every skip/error, but keeping this catch here
                # prevents one broken city series from killing the full scan.
                raise requests.RequestException(f"{series_ticker}: {exc}") from exc
        return all_markets

    def is_market_open(self, ticker: str) -> bool:
        """Return True only when a market is currently open for orders.

        Fetches GET /markets/{ticker}. A 404/410 (market gone or already
        settled) or any non-"open" status is treated as not open, so the caller
        can skip the trade instead of attempting an order Kalshi would reject
        with a 410.
        """
        try:
            data = self._request("GET", f"/markets/{ticker}", auth_required=False)
        except requests.HTTPError as exc:
            status_code = getattr(exc.response, "status_code", None)
            if status_code in (404, 410):
                logging.info("Market %s returned %s — treating as settled.", ticker, status_code)
                return False
            raise
        status = str(data.get("market", {}).get("status", "")).lower()
        return status == "open"

    def place_limit_order(
        self,
        *,
        ticker: str,
        side: str,
        count: int,
        limit_price: float,
    ) -> dict[str, Any]:
        """Place a buy limit order and never a market order."""
        if side not in {"yes", "no"}:
            raise ValueError("side must be 'yes' or 'no'")
        if count < 1:
            raise ValueError("count must be at least 1")
        if not 0.01 <= limit_price <= 0.99:
            raise ValueError("limit_price must be between 0.01 and 0.99")

        price_str = f"{limit_price:.4f}"
        body: dict[str, Any] = {
            "ticker": ticker,
            "action": "buy",
            "side": side,
            "type": "limit",
            "count": count,
            "client_order_id": str(uuid.uuid4()),
        }
        if side == "yes":
            body["yes_price_dollars"] = price_str
        else:
            body["no_price_dollars"] = price_str

        # Kalshi's 2026 API expects dollar-string fields, not integer cents.
        body.pop("yes_price", None)
        body.pop("no_price", None)

        return self._request("POST", "/portfolio/orders", json=body, auth_required=True)

    def test_connection(self) -> bool:
        """Return True when Kalshi reports the exchange is active."""
        try:
            data = self._request("GET", "/exchange/status", auth_required=False)
            return data.get("exchange_active", False)
        except Exception:
            return False

    def switch_to_live(self) -> None:
        """Switch from demo to production environment."""
        self.base_url = self.PROD_URL
        self.env = "prod"
        logging.warning("SWITCHED TO LIVE TRADING — real money at risk")

    def get_orderbook(self, ticker: str) -> dict[str, float | int]:
        """Fetch order book for a market. Returns yes_bid_depth, yes_ask_depth, imbalance_score."""
        neutral = {"imbalance_score": 0.5, "yes_bid_depth": 0, "yes_ask_depth": 0}
        try:
            data = self._request("GET", f"/markets/{ticker}/orderbook", auth_required=False)
            orderbook = data.get("orderbook", data)
            yes_bids = orderbook.get("yes", []) or []
            no_bids = orderbook.get("no", []) or []

            yes_bid_depth = sum(int(level[1]) for level in yes_bids if isinstance(level, (list, tuple)) and len(level) >= 2)
            # YES ask depth is mirrored from NO bid side on Kalshi binary books
            yes_ask_depth = sum(int(level[1]) for level in no_bids if isinstance(level, (list, tuple)) and len(level) >= 2)

            total = yes_bid_depth + yes_ask_depth
            imbalance_score = yes_bid_depth / total if total > 0 else 0.5
            return {
                "imbalance_score": round(float(imbalance_score), 4),
                "yes_bid_depth": yes_bid_depth,
                "yes_ask_depth": yes_ask_depth,
            }
        except Exception as exc:
            logging.warning("Orderbook fetch failed for %s: %s", ticker, exc)
            return neutral

    def get_order_status(self, order_id: str) -> dict[str, Any]:
        """Poll order status. Returns status, filled_count, remaining_count."""
        try:
            data = self._request("GET", f"/portfolio/orders/{order_id}", auth_required=True)
            order = data.get("order", data)
            return {
                "status": str(order.get("status", "")).lower(),
                "filled_count": int(order.get("filled_count", 0) or 0),
                "remaining_count": int(order.get("remaining_count", 0) or 0),
            }
        except Exception as exc:
            logging.warning("Order status fetch failed for %s: %s", order_id, exc)
            return {"status": "unknown", "filled_count": 0, "remaining_count": 0}

    def cancel_order(self, order_id: str) -> bool:
        """Cancel a resting order."""
        try:
            data = self._request("DELETE", f"/portfolio/orders/{order_id}", auth_required=True)
            order = data.get("order", data)
            return str(order.get("status", "")).lower() == "canceled"
        except Exception as exc:
            logging.warning("Order cancel failed for %s: %s", order_id, exc)
            return False

    def get_balance(self) -> float | None:
        """Fetch real account balance with retry on connection errors."""
        import time
        for attempt in range(3):
            try:
                data = self._request("GET", "/portfolio/balance", auth_required=True)
                balance = data.get("balance_dollars") or data.get("balance")
                if balance is not None:
                    return round(float(balance), 2)
                return None
            except Exception as exc:
                logging.warning("Balance fetch attempt %d/3 failed: %s", attempt + 1, exc)
                if attempt < 2:
                    time.sleep(5)
        return None

    @property
    def has_credentials(self) -> bool:
        """Return True when both Kalshi key and secret are configured."""
        return bool(self.api_key and self.api_secret)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        auth_required: bool,
    ) -> dict[str, Any]:
        """Run an HTTP request and raise a requests exception on failure."""
        headers = {"Content-Type": "application/json"}
        if auth_required or self.has_credentials:
            headers.update(self._auth_headers(method, path))

        response = self.session.request(
            method,
            f"{self.base_url}{path}",
            params=params,
            json=json,
            headers=headers,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        return response.json() if response.content else {}

    def _auth_headers(self, method: str, path: str) -> dict[str, str]:
        """Build Kalshi RSA-PSS authentication headers."""
        if not self.has_credentials:
            return {}
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.primitives.asymmetric import rsa
        import time, base64
        from pathlib import Path

        timestamp = str(int(time.time() * 1000))

        # Path must include /trade-api/v2 prefix but strip query params
        # e.g. path="/markets" -> sign "/trade-api/v2/markets"
        # e.g. path="/trade-api/v2/markets" -> sign as-is
        if not path.startswith("/trade-api"):
            sign_path = f"/trade-api/v2{path}"
        else:
            sign_path = path
        # Strip query string - never include ? params in signature
        sign_path = sign_path.split("?")[0]

        message = f"{timestamp}{method.upper()}{sign_path}".encode("utf-8")

        # Load private key from file path
        key_path = Path(self.api_secret)
        if not key_path.is_absolute():
            key_path = Path(__file__).resolve().parents[1] / key_path

        private_key = serialization.load_pem_private_key(
            key_path.read_bytes(), password=None
        )
        signature = private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH
            ),
            hashes.SHA256()
        )
        return {
            "KALSHI-ACCESS-KEY": self.api_key,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("ascii"),
            "Content-Type": "application/json",
        }

    def _normalize_market(self, raw: dict[str, Any]) -> KalshiMarket:
        """Convert Kalshi's raw JSON into a stable dataclass."""
        logging.debug(
            "Raw Kalshi prices ticker=%s yes_ask=%r no_ask=%r",
            raw.get("ticker", ""),
            raw.get("yes_ask"),
            raw.get("no_ask"),
        )
        return KalshiMarket(
            ticker=str(raw.get("ticker", "")),
            series_ticker=str(raw.get("series_ticker", "")),
            title=str(raw.get("title", "")),
            subtitle=str(raw.get("subtitle", "")),
            yes_bid=self._price_to_dollars(raw.get("yes_bid_dollars")),
            yes_ask=self._price_to_dollars(raw.get("yes_ask_dollars")),
            no_bid=self._price_to_dollars(raw.get("no_bid_dollars")),
            no_ask=self._price_to_dollars(raw.get("no_ask_dollars")),
            close_time=self._parse_time(raw.get("close_time")),
            settlement_time=self._parse_time(raw.get("settlement_time") or raw.get("expected_expiration_time")),
            raw=raw,
        )

    @staticmethod
    def _price_to_dollars(value: Any) -> float | None:
        """Convert Kalshi cents or decimal dollars into decimal dollars."""
        if value is None:
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if 0 <= number <= 1.0:
            return number
        if number > 1.0:
            return number / 100
        return None

    @staticmethod
    def _parse_time(value: Any) -> datetime | None:
        """Parse a Kalshi timestamp into a timezone-aware UTC datetime."""
        if not value:
            return None
        if isinstance(value, datetime):
            return value.astimezone(timezone.utc)
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            return None

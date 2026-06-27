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
    PROD_URL = "https://api.elections.kalshi.com/trade-api/v2"

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

        price_cents = int(round(limit_price * 100))
        body: dict[str, Any] = {
            "ticker": ticker,
            "action": "buy",
            "side": side,
            "type": "limit",
            "count": count,
            "client_order_id": str(uuid.uuid4()),
        }
        if side == "yes":
            body["yes_price"] = price_cents
        else:
            body["no_price"] = price_cents

        return self._request("POST", "/portfolio/orders", json=body, auth_required=True)

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
        import time, base64
        from pathlib import Path

        timestamp = str(int(time.time() * 1000))
        message = f"{timestamp}{method.upper()}{path}".encode("utf-8")
        # Load private key from file path stored in api_secret
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

"""Weather API client for NWS gridded forecasts and station observations."""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import requests


@dataclass(frozen=True)
class CityConfig:
    """Configuration that maps a Kalshi city market to weather data."""

    name: str
    ticker: str
    lat: float
    lon: float
    nws_station: str
    tz: str
    nws_bias_f: float = -1.5
    nws_low_bias_f: float = 1.0

    @property
    def high_series(self) -> str:
        """Return the KXHIGH series ticker for this city."""
        return self.ticker

    @property
    def low_series(self) -> str:
        """Return the matching KXLOW series ticker for this city."""
        return self.ticker.replace("KXHIGH", "KXLOW")

    @property
    def lowt_series(self) -> str:
        """Return the matching KXLOWT (overnight low) series ticker."""
        return self.ticker.replace("KXHIGH", "KXLOWT")

    @property
    def short_code(self) -> str:
        """Return a short city code for compact logs."""
        return self.ticker.replace("KXHIGH", "")


@dataclass(frozen=True)
class NwsForecast:
    """NWS gridded hourly forecast matched to the market settlement date."""

    temperature_f: float | None
    short_forecast: str
    source: str


# Original 5 cities — kept first so CITY_COUNT=5 selects exactly these.
ORIGINAL_CITIES: dict[str, CityConfig] = {
    "New York": CityConfig("New York", "KXHIGHNY", 40.7128, -74.0060, "KNYC", "America/New_York", nws_bias_f=-1.5),
    "Chicago": CityConfig("Chicago", "KXHIGHCHI", 41.8781, -87.6298, "KMDW", "America/Chicago", nws_bias_f=-1.5),
    "Miami": CityConfig("Miami", "KXHIGHMIA", 25.7617, -80.1918, "KMIA", "America/New_York", nws_bias_f=-1.5),
    "Los Angeles": CityConfig("Los Angeles", "KXHIGHLAX", 34.0522, -118.2437, "KLAX", "America/Los_Angeles", nws_bias_f=-1.5),
    "Denver": CityConfig("Denver", "KXHIGHDEN", 39.7392, -104.9903, "KDEN", "America/Denver", nws_bias_f=-1.5),
}

# 8 expansion cities — enabled when CITY_COUNT >= 13.
EXTENDED_CITIES: dict[str, CityConfig] = {
    "Seattle": CityConfig("Seattle", "KXHIGHTSEA", 47.6062, -122.3321, "KSEA", "America/Los_Angeles", nws_bias_f=-1.0),
    "San Francisco": CityConfig("San Francisco", "KXHIGHTSFO", 37.7749, -122.4194, "KSFO", "America/Los_Angeles", nws_bias_f=-1.0),
    "Dallas": CityConfig("Dallas", "KXHIGHTDAL", 32.7767, -96.7970, "KDAL", "America/Chicago", nws_bias_f=-1.5),
    "Minneapolis": CityConfig("Minneapolis", "KXHIGHTMIN", 44.9778, -93.2650, "KMSP", "America/Chicago", nws_bias_f=-1.5),
    "Oklahoma City": CityConfig("Oklahoma City", "KXHIGHTOKC", 35.4676, -97.5164, "KOKC", "America/Chicago", nws_bias_f=-1.5),
    "Atlanta": CityConfig("Atlanta", "KXHIGHTATL", 33.7490, -84.3880, "KATL", "America/New_York", nws_bias_f=-1.5),
    "Boston": CityConfig("Boston", "KXHIGHTBOS", 42.3601, -71.0589, "KBOS", "America/New_York", nws_bias_f=-1.5),
    "Washington DC": CityConfig("Washington DC", "KXHIGHTDC", 38.9072, -77.0369, "KDCA", "America/New_York", nws_bias_f=-1.5),
}

CITIES: dict[str, CityConfig] = {**ORIGINAL_CITIES, **EXTENDED_CITIES}


class WeatherClient:
    """Fetch and normalize weather data from free NWS public APIs."""

    NWS_BASE_URL = "https://api.weather.gov"

    def __init__(self) -> None:
        """Create an HTTP session and read API-related settings from env vars."""
        self.session = requests.Session()
        self.timeout_seconds = 25
        self.nws_user_agent = os.getenv("NWS_USER_AGENT", "kalshi-weather-bot/1.0")
        self.nws_warm_bias_f = float(os.getenv("NWS_SUMMER_HIGH_WARM_BIAS_F", "1.5"))
        self.nws_low_bias_f = float(os.getenv("NWS_SUMMER_LOW_BIAS_F", "1.0"))
        # Per-city gridpoint coordinates (office, gridX, gridY) — never change.
        self._gridpoint_cache: dict[str, tuple[str, int, int]] = {}
        # Hourly forecast bundle per (city, date): {"high": NwsForecast, "low": NwsForecast}
        self._forecast_cache: dict[tuple[str, str], tuple[dict[str, NwsForecast], datetime]] = {}
        cache_ttl_minutes = int(os.getenv("NWS_FORECAST_CACHE_TTL_MINUTES", "60"))
        self._cache_ttl = timedelta(minutes=cache_ttl_minutes)
        self._cache_lock = threading.Lock()

    def watched_cities(self) -> list[CityConfig]:
        """Return the city list to scan, controlled by the CITY_COUNT env flag.

        CITY_COUNT=13 (default) scans all cities. Set CITY_COUNT=5 to scan only
        the original five cities.
        """
        city_count = int(os.getenv("CITY_COUNT", "13"))
        if city_count >= len(CITIES):
            return list(CITIES.values())
        return list(CITIES.values())[:city_count]

    def watched_series_tickers(self) -> list[str]:
        """Return all KXHIGH, KXLOW, and KXLOWT series tickers watched by the bot."""
        series: list[str] = []
        for city in self.watched_cities():
            series.extend([city.high_series, city.low_series, city.lowt_series])
        return series

    def city_for_market(self, series_ticker: str, market_ticker: str = "") -> CityConfig | None:
        """Match a Kalshi market or series ticker back to a configured city."""
        text = f"{series_ticker} {market_ticker}".upper()
        for city in self.watched_cities():
            if city.high_series in text or city.low_series in text or city.lowt_series in text:
                return city
        return None

    def get_nws_gridded_forecast(
        self,
        city: CityConfig,
        target_date: date,
        market_type: str,
    ) -> NwsForecast:
        """Return NWS gridded hourly forecast high/low for the target date.

        One hourly API call per (city, date) yields both HIGH and LOW forecasts.
        HIGH = max temp 12:00-18:00 local; LOW = min temp 00:00-08:00 local.
        Summer bias correction is applied before caching.
        """
        want = "high" if market_type == "high" else "low"
        cache_key = (city.name, str(target_date))
        with self._cache_lock:
            cached = self._forecast_cache.get(cache_key)
            if cached is not None:
                bundle, expires_at = cached
                if datetime.now() < expires_at:
                    return bundle[want]

        bundle = self._fetch_gridded_bundle(city, target_date)
        with self._cache_lock:
            self._forecast_cache[cache_key] = (bundle, datetime.now() + self._cache_ttl)
        return bundle[want]

    def _fetch_gridded_bundle(self, city: CityConfig, target_date: date) -> dict[str, NwsForecast]:
        """Fetch hourly forecast and build HIGH/LOW NwsForecast bundle."""
        empty_high = NwsForecast(None, "NWS gridded fetch failed", city.nws_station)
        empty_low = NwsForecast(None, "NWS gridded fetch failed", city.nws_station)
        headers = {"User-Agent": self.nws_user_agent, "Accept": "application/geo+json"}
        try:
            office, grid_x, grid_y = self._resolve_gridpoint(city, headers)
            url = f"{self.NWS_BASE_URL}/gridpoints/{office}/{grid_x},{grid_y}/forecast/hourly"
            response = self.session.get(url, headers=headers, timeout=self.timeout_seconds)
            response.raise_for_status()
            periods = response.json().get("properties", {}).get("periods", [])
            return self._build_bundle_from_periods(city, target_date, periods)
        except Exception as exc:
            logging.warning("NWS gridded forecast failed for %s: %s", city.name, exc)
            return {"high": empty_high, "low": empty_low}

    def _resolve_gridpoint(self, city: CityConfig, headers: dict[str, str]) -> tuple[str, int, int]:
        """Return (office, gridX, gridY) for a city, caching forever after first lookup."""
        with self._cache_lock:
            cached = self._gridpoint_cache.get(city.name)
            if cached is not None:
                return cached

        point_url = f"{self.NWS_BASE_URL}/points/{city.lat:.4f},{city.lon:.4f}"
        response = self.session.get(point_url, headers=headers, timeout=self.timeout_seconds)
        response.raise_for_status()
        props = response.json()["properties"]
        gridpoint = (str(props["gridId"]), int(props["gridX"]), int(props["gridY"]))
        with self._cache_lock:
            self._gridpoint_cache[city.name] = gridpoint
        return gridpoint

    def _build_bundle_from_periods(
        self,
        city: CityConfig,
        target_date: date,
        periods: list[dict[str, Any]],
    ) -> dict[str, NwsForecast]:
        """Parse hourly periods into HIGH and LOW NwsForecast objects."""
        tz = ZoneInfo(city.tz)
        high_temps: list[float] = []
        low_temps: list[float] = []
        all_day_temps: list[float] = []
        high_short = ""
        low_short = ""

        for period in periods:
            start_time = period.get("startTime")
            if not start_time:
                continue
            try:
                local_dt = datetime.fromisoformat(str(start_time).replace("Z", "+00:00")).astimezone(tz)
            except ValueError:
                continue
            if local_dt.date() != target_date:
                continue
            temp = self._safe_float(period.get("temperature"))
            if temp is None:
                continue
            all_day_temps.append(temp)
            hour = local_dt.hour
            short = str(period.get("shortForecast", ""))
            if 12 <= hour <= 18:
                high_temps.append(temp)
                if not high_short:
                    high_short = short
            if 0 <= hour <= 8:
                low_temps.append(temp)
                if not low_short:
                    low_short = short

        high_raw = max(high_temps) if high_temps else (max(all_day_temps) if all_day_temps else None)
        low_raw = min(low_temps) if low_temps else (min(all_day_temps) if all_day_temps else None)

        high_temp = self._apply_bias(high_raw, city, target_date, "high")
        low_temp = self._apply_bias(low_raw, city, target_date, "low")

        return {
            "high": NwsForecast(
                high_temp,
                high_short or "NWS gridded hourly",
                f"{city.nws_station} via NWS gridded hourly",
            ),
            "low": NwsForecast(
                low_temp,
                low_short or "NWS gridded hourly",
                f"{city.nws_station} via NWS gridded hourly",
            ),
        }

    def _apply_bias(
        self,
        temperature: float | None,
        city: CityConfig,
        target_date: date,
        market_type: str,
    ) -> float | None:
        """Apply summer bias correction to raw NWS temperature."""
        if temperature is None or target_date.month not in {6, 7, 8}:
            return temperature
        if market_type == "high":
            bias = abs(city.nws_bias_f) if city.nws_bias_f else self.nws_warm_bias_f
            return round(temperature - bias, 2)
        low_bias = city.nws_low_bias_f if city.nws_low_bias_f else self.nws_low_bias_f
        return round(temperature + low_bias, 2)

    def latest_station_observation(self, city: CityConfig) -> dict[str, Any] | None:
        """Fetch the latest station observation for debugging and audit logs."""
        headers = {"User-Agent": self.nws_user_agent, "Accept": "application/geo+json"}
        url = f"{self.NWS_BASE_URL}/stations/{city.nws_station}/observations/latest"
        try:
            response = self.session.get(url, headers=headers, timeout=self.timeout_seconds)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            logging.warning("NWS station observation failed for %s: %s", city.nws_station, exc)
            return None

    @staticmethod
    def _safe_float(value: Any) -> float | None:
        """Convert a value to float, returning None when conversion fails."""
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

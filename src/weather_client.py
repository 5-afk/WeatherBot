"""Weather API client for Open-Meteo ensembles and NWS forecasts."""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

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
class EnsembleForecast:
    """Daily high/low values from one weather ensemble model."""

    model_name: str
    member_temperatures_f: list[float]
    expected_temperature_f: float | None
    member_count: int


@dataclass(frozen=True)
class NwsForecast:
    """NWS official forecast period matched to the market settlement date."""

    temperature_f: float | None
    short_forecast: str
    source: str


# Original 5 cities — always scanned. Kept first so CITY_COUNT=5 selects exactly these.
ORIGINAL_CITIES: dict[str, CityConfig] = {
    "New York": CityConfig("New York", "KXHIGHNY", 40.7128, -74.0060, "KNYC", "America/New_York", nws_bias_f=-1.5),
    "Chicago": CityConfig("Chicago", "KXHIGHCHI", 41.8781, -87.6298, "KMDW", "America/Chicago", nws_bias_f=-1.5),
    "Miami": CityConfig("Miami", "KXHIGHMIA", 25.7617, -80.1918, "KMIA", "America/New_York", nws_bias_f=-1.5),
    "Los Angeles": CityConfig("Los Angeles", "KXHIGHLAX", 34.0522, -118.2437, "KLAX", "America/Los_Angeles", nws_bias_f=-1.5),
    "Denver": CityConfig("Denver", "KXHIGHDEN", 39.7392, -104.9903, "KDEN", "America/Denver", nws_bias_f=-1.5),
}

# 8 expansion cities — enabled only when CITY_COUNT >= 13. Temporarily disabled by
# default (CITY_COUNT=5) to stay within Open-Meteo's free-tier rate limit.
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
    """Fetch and normalize weather data from free public APIs."""

    OPEN_METEO_ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
    NWS_BASE_URL = "https://api.weather.gov"

    def __init__(self) -> None:
        """Create an HTTP session and read API-related settings from env vars."""
        self.session = requests.Session()
        self.timeout_seconds = 25
        self.nws_user_agent = os.getenv("NWS_USER_AGENT", "kalshi-weather-bot/1.0")
        self.model_names = {
            "gfs": os.getenv("OPEN_METEO_GFS_MODEL", "gfs_seamless"),
            "icon": os.getenv("OPEN_METEO_ICON_MODEL", "icon_seamless"),
        }
        self.nws_warm_bias_f = float(os.getenv("NWS_SUMMER_HIGH_WARM_BIAS_F", "1.5"))
        self.nws_low_bias_f = float(os.getenv("NWS_SUMMER_LOW_BIAS_F", "1.0"))
        self.expected_members = {
            "gfs": int(os.getenv("EXPECTED_GFS_MEMBERS", "31")),
            "icon": int(os.getenv("EXPECTED_ICON_MEMBERS", "40")),
        }
        # Cache at (city, model, date) level — one fetch yields both HIGH and LOW
        # member distributions, so KXHIGH/KXLOW/KXLOWT markets reuse the same call.
        self._cache: dict[tuple[str, str, str], tuple[dict[str, EnsembleForecast], datetime]] = {}
        cache_ttl_minutes = int(os.getenv("OPEN_METEO_CACHE_TTL_MINUTES", "60"))
        self._cache_ttl = timedelta(minutes=cache_ttl_minutes)
        self._cache_lock = threading.Lock()
        # Limit concurrent Open-Meteo HTTP calls so parallel scans do not trip
        # the free-tier rate limit all at once.
        self._api_semaphore = threading.Semaphore(int(os.getenv("OPEN_METEO_MAX_CONCURRENT", "3")))

    def watched_cities(self) -> list[CityConfig]:
        """Return the city list to scan, controlled by the CITY_COUNT env flag.

        CITY_COUNT=5 (default) scans only the original 5 cities to stay within
        Open-Meteo's free-tier rate limit. CITY_COUNT=13 enables all cities.
        """
        city_count = int(os.getenv("CITY_COUNT", "5"))
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

    def prewarm_cache(self, cities: list[CityConfig], target_date: date) -> None:
        """Pre-fetch all ensemble data sequentially before market evaluation begins.

        Fills the (city, model, date) cache once per scan cycle with staggered
        delays (5s initial cooldown, 2.0s between models, 4.0s between cities)
        so the parallel evaluation phase hits cache instead of hammering Open-Meteo.
        A 429 is logged and skipped (no aggressive retry); affected cities fall back
        to GFS-only for this cycle.
        """
        logging.info("Prewarming cache — waiting 5s before first fetch...")
        time.sleep(5.0)
        for city in cities:
            for model_key in ("gfs", "icon"):
                self.get_ensemble_forecast(city, model_key, target_date, "high", allow_fetch=True)
                time.sleep(2.0)
            time.sleep(4.0)

    def get_ensemble_forecast(
        self,
        city: CityConfig,
        model_key: str,
        target_date: date,
        market_type: str,
        allow_fetch: bool = False,
    ) -> EnsembleForecast:
        """Return a GFS or ICON ensemble reduced to daily highs/lows.

        Caches both the HIGH and LOW member distributions per (city, model, date)
        so HIGH and LOW markets for the same city reuse a single API call.

        API calls happen ONLY when ``allow_fetch=True`` (prewarm_cache). In the
        evaluation path (``allow_fetch=False``, the default) a cache hit is
        returned and a cache miss returns an empty forecast immediately with zero
        network calls — making it impossible for evaluation threads to hit the API
        or stall on rate limits. On a 429 (or any error) during prewarm we log and
        return an empty forecast: no retry, no backoff, no sleeping.
        """
        model_name = self.model_names[model_key]
        cache_key = (city.name, model_key, str(target_date))
        want = "high" if market_type == "high" else "low"
        empty = EnsembleForecast(model_name, [], None, 0)
        with self._cache_lock:
            cached = self._cache.get(cache_key)
            if cached is not None:
                bundle, expires_at = cached
                if datetime.now() < expires_at:
                    return bundle[want]

        if not allow_fetch:
            logging.debug(
                "Cache miss for %s %s — prewarm did not cover this key; returning empty.",
                city.name,
                model_key,
            )
            return empty

        params = {
            "latitude": city.lat,
            "longitude": city.lon,
            "hourly": "temperature_2m",
            "temperature_unit": "fahrenheit",
            "forecast_days": 3,
            "models": model_name,
        }
        try:
            with self._api_semaphore:
                response = self.session.get(
                    self.OPEN_METEO_ENSEMBLE_URL,
                    params=params,
                    timeout=30,
                )
            if response.status_code == 429:
                logging.warning(
                    "Open-Meteo rate limited for %s %s — skipping this cycle.",
                    city.name,
                    model_key,
                )
                return empty
            response.raise_for_status()
            bundle = self._parse_ensemble_bundle(
                response.json(),
                model_name=model_name,
                model_key=model_key,
                target_date=target_date,
                city=city,
            )
            with self._cache_lock:
                self._cache[cache_key] = (bundle, datetime.now() + self._cache_ttl)
            return bundle[want]
        except Exception as exc:
            logging.warning(
                "Ensemble forecast failed for %s %s: %s",
                city.name,
                model_key,
                exc,
            )
            return empty

    def _parse_ensemble_bundle(
        self,
        payload: dict[str, Any],
        *,
        model_name: str,
        model_key: str,
        target_date: date,
        city: CityConfig,
    ) -> dict[str, EnsembleForecast]:
        """Parse Open-Meteo ensemble JSON into both HIGH and LOW member distributions."""
        hourly = payload.get("hourly", {})
        times = hourly.get("time", [])
        member_series = self._extract_temperature_members(hourly)

        high_values: list[float] = []
        low_values: list[float] = []
        for values in member_series:
            high_value = self._daily_high_or_low(times, values, target_date, "high")
            low_value = self._daily_high_or_low(times, values, target_date, "low")
            if high_value is not None:
                high_values.append(high_value)
            if low_value is not None:
                low_values.append(low_value)

        return {
            "high": self._build_forecast(model_name, model_key, city, high_values),
            "low": self._build_forecast(model_name, model_key, city, low_values),
        }

    def _build_forecast(
        self,
        model_name: str,
        model_key: str,
        city: CityConfig,
        daily_values: list[float],
    ) -> EnsembleForecast:
        """Build an EnsembleForecast and warn when member count is below expected."""
        member_count = len(daily_values)
        expected = self.expected_members.get(model_key)
        if expected is not None and member_count < expected:
            logging.warning(
                "%s returned %d members for %s, expected %d.",
                model_key.upper(),
                member_count,
                city.name,
                expected,
            )
        mean_temp = round(sum(daily_values) / member_count, 2) if daily_values else None
        return EnsembleForecast(model_name, daily_values, mean_temp, member_count)

    def get_nws_forecast(self, city: CityConfig, target_date: date, market_type: str) -> NwsForecast:
        """Fetch the official NWS point forecast for the settlement station area."""
        headers = {"User-Agent": self.nws_user_agent, "Accept": "application/geo+json"}
        try:
            point_url = f"{self.NWS_BASE_URL}/points/{city.lat:.4f},{city.lon:.4f}"
            point_response = self.session.get(point_url, headers=headers, timeout=self.timeout_seconds)
            point_response.raise_for_status()
            forecast_url = point_response.json()["properties"]["forecast"]

            forecast_response = self.session.get(forecast_url, headers=headers, timeout=self.timeout_seconds)
            forecast_response.raise_for_status()
            periods = forecast_response.json()["properties"].get("periods", [])
            period = self._select_nws_period(periods, target_date, market_type)
            if not period:
                return NwsForecast(None, "No matching NWS forecast period", city.nws_station)

            temperature = self._safe_float(period.get("temperature"))
            if temperature is not None and target_date.month in {6, 7, 8}:
                if market_type == "high":
                    # NWS warm bias for HIGH markets in summer — subtract correction
                    bias = abs(city.nws_bias_f) if city.nws_bias_f else self.nws_warm_bias_f
                    temperature = round(temperature - bias, 2)
                elif market_type == "low":
                    # LOW forecasts underestimated in summer — add correction
                    low_bias = city.nws_low_bias_f if city.nws_low_bias_f else self.nws_low_bias_f
                    temperature = round(temperature + low_bias, 2)

            return NwsForecast(
                temperature,
                str(period.get("shortForecast", "")),
                f"{city.nws_station} via NWS point forecast",
            )
        except requests.RequestException as exc:
            logging.warning("NWS forecast failed for %s: %s", city.nws_station, exc)
            return NwsForecast(None, "NWS fetch failed", city.nws_station)

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

    def _extract_temperature_members(self, hourly: dict[str, Any]) -> list[list[float | None]]:
        """Extract ensemble member arrays from Open-Meteo's hourly object."""
        members: list[list[float | None]] = []
        for key, value in hourly.items():
            if key == "time" or not isinstance(value, list):
                continue
            if key.startswith("temperature_2m"):
                members.append(value)
        return members

    def _daily_high_or_low(
        self,
        times: list[str],
        values: list[float | None],
        target_date: date,
        market_type: str,
    ) -> float | None:
        """Return a member's high or low temperature for the target date."""
        temperatures: list[float] = []
        for timestamp, value in zip(times, values):
            if value is None:
                continue
            try:
                local_date = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00")).date()
            except ValueError:
                continue
            if local_date == target_date:
                temperatures.append(float(value))

        if not temperatures:
            return None
        return max(temperatures) if market_type == "high" else min(temperatures)

    def _select_nws_period(
        self,
        periods: list[dict[str, Any]],
        target_date: date,
        market_type: str,
    ) -> dict[str, Any] | None:
        """Pick daytime periods for highs and nighttime periods for lows."""
        wants_daytime = market_type == "high"
        for period in periods:
            start_time = period.get("startTime")
            if not start_time:
                continue
            try:
                period_date = datetime.fromisoformat(str(start_time).replace("Z", "+00:00")).date()
            except ValueError:
                continue
            if period_date == target_date and bool(period.get("isDaytime")) == wants_daytime:
                return period
        return None

    @staticmethod
    def _safe_float(value: Any) -> float | None:
        """Convert a value to float, returning None when conversion fails."""
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

"""Weather API client for Open-Meteo ensembles and NWS forecasts."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date, datetime
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

    @property
    def high_series(self) -> str:
        """Return the KXHIGH series ticker for this city."""
        return self.ticker

    @property
    def low_series(self) -> str:
        """Return the matching KXLOW series ticker for this city."""
        return self.ticker.replace("KXHIGH", "KXLOW")

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


CITIES: dict[str, CityConfig] = {
    "New York": CityConfig("New York", "KXHIGHNY", 40.7128, -74.0060, "KNYC", "America/New_York"),
    "Chicago": CityConfig("Chicago", "KXHIGHCHI", 41.8781, -87.6298, "KMDW", "America/Chicago"),
    "Miami": CityConfig("Miami", "KXHIGHMIA", 25.7617, -80.1918, "KMIA", "America/New_York"),
    "Los Angeles": CityConfig("Los Angeles", "KXHIGHLAX", 34.0522, -118.2437, "KLAX", "America/Los_Angeles"),
    "Denver": CityConfig("Denver", "KXHIGHDEN", 39.7392, -104.9903, "KDEN", "America/Denver"),
}


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
            "ecmwf": os.getenv("OPEN_METEO_ECMWF_MODEL", "ecmwf_ifs025"),
        }

    def watched_cities(self) -> list[CityConfig]:
        """Return the exact city list requested for scanning."""
        return list(CITIES.values())

    def watched_series_tickers(self) -> list[str]:
        """Return all KXHIGH and KXLOW series tickers watched by the bot."""
        series: list[str] = []
        for city in self.watched_cities():
            series.extend([city.high_series, city.low_series])
        return series

    def city_for_market(self, series_ticker: str, market_ticker: str = "") -> CityConfig | None:
        """Match a Kalshi market or series ticker back to a configured city."""
        text = f"{series_ticker} {market_ticker}".upper()
        for city in self.watched_cities():
            if city.high_series in text or city.low_series in text:
                return city
        return None

    def get_ensemble_forecast(
        self,
        city: CityConfig,
        model_key: str,
        target_date: date,
        market_type: str,
    ) -> EnsembleForecast:
        """Fetch a GFS or ECMWF ensemble and reduce members to daily highs/lows."""
        model_name = self.model_names[model_key]
        params = {
            "latitude": city.lat,
            "longitude": city.lon,
            "hourly": "temperature_2m",
            "temperature_unit": "fahrenheit",
            "forecast_days": 3,
            "models": model_name,
        }
        response = self.session.get(self.OPEN_METEO_ENSEMBLE_URL, params=params, timeout=self.timeout_seconds)
        response.raise_for_status()
        payload = response.json()
        hourly = payload.get("hourly", {})
        times = hourly.get("time", [])
        member_series = self._extract_temperature_members(hourly)

        daily_values = []
        for values in member_series:
            daily_value = self._daily_high_or_low(times, values, target_date, market_type)
            if daily_value is not None:
                daily_values.append(daily_value)

        expected = round(sum(daily_values) / len(daily_values), 2) if daily_values else None
        return EnsembleForecast(model_name, daily_values, expected, len(daily_values))

    def get_nws_forecast(self, city: CityConfig, target_date: date, market_type: str) -> NwsForecast:
        """Fetch the official NWS point forecast for the settlement station area."""
        headers = {"User-Agent": self.nws_user_agent, "Accept": "application/geo+json"}
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
        return NwsForecast(
            temperature,
            str(period.get("shortForecast", "")),
            f"{city.nws_station} via NWS point forecast",
        )

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

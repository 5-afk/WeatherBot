"""Weather API client for NWS station-specific forecasts and observations."""

from __future__ import annotations

import logging
import os
import re
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import requests


# Fallback location-name substrings in Kalshi rules_primary -> ICAO station codes.
_LOCATION_TO_STATION: dict[str, str] = {
    "los angeles airport": "KLAX",
    "central park": "KNYC",
    "chicago midway": "KMDW",
    "miami international": "KMIA",
    "denver international": "KDEN",
    "denver": "KDEN",
    "seattle-tacoma": "KSEA",
    "san francisco international": "KSFO",
    "dallas/fort worth": "KDFW",
    "minneapolis": "KMSP",
    "oklahoma city": "KOKC",
    "hartsfield": "KATL",
    "boston logan": "KBOS",
    "reagan national": "KDCA",
    "ronald reagan": "KDCA",
    "washington dc": "KDCA",
    "national airport": "KDCA",
    "dulles": "KIAD",
}

# Series tickers used for startup contract-driven station verification.
_VERIFY_SERIES_EXPECTED: list[tuple[str, str]] = [
    ("KXHIGHLAX", "KLAX"),
    ("KXHIGHNY", "KNYC"),
    ("KXHIGHCHI", "KMDW"),
    ("KXHIGHMIA", "KMIA"),
    ("KXHIGHDEN", "KDEN"),
]

# Per-city seasonal sigma (°F) based on NWS forecast verification studies.
# Format: station_id -> (summer_sigma, winter_sigma)
# Summer = Jun/Jul/Aug, Winter = Dec/Jan/Feb, Spring/Fall interpolated.
CITY_SIGMA: dict[str, tuple[float, float]] = {
    "KNYC": (3.2, 4.1),
    "KMDW": (3.8, 5.2),
    "KMIA": (1.9, 2.1),
    "KLAX": (2.1, 2.8),
    "KDEN": (4.6, 6.1),
    "KOKC": (4.2, 5.8),
    "KBOS": (3.4, 4.7),
    "KDCA": (3.1, 4.3),
    "KSEA": (2.8, 3.9),
    "KSFO": (2.3, 3.1),
    "KATL": (2.9, 3.8),
    "KDFW": (3.9, 4.8),
    "KMSP": (4.1, 6.4),
}

# Documented NWS systematic forecast bias by station (°F).
# Positive = NWS underforecasts (add to forecast), Negative = NWS overforecasts (subtract).
# Summer HIGH market corrections only (Jun-Aug).
CITY_BIAS_CORRECTIONS: dict[str, float] = {
    "KLAX": -2.5,
    "KSFO": -2.0,
    "KMIA": -0.8,
    "KMDW": 1.2,
    "KDEN": -1.5,
    "KOKC": 1.8,
    "KDFW": 1.2,
    "KATL": 0.9,
}


def get_sigma(station_id: str, target_date: date) -> float:
    """Return seasonally adjusted forecast sigma for a station."""
    month = target_date.month
    summer, winter = CITY_SIGMA.get(station_id, (3.5, 4.5))
    if month in (6, 7, 8):
        return summer
    if month in (12, 1, 2):
        return winter
    if month in (3, 4, 5):
        return winter + (summer - winter) * ((month - 2) / 4)
    return summer + (winter - summer) * ((month - 8) / 4)


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
    """NWS station hourly forecast matched to the market settlement date."""

    temperature_f: float | None
    short_forecast: str
    source: str
    forecast_spread_f: float | None = None
    uncertain: bool = False


def _city(
    name: str,
    ticker: str,
    lat: float,
    lon: float,
    nws_station: str,
    tz: str,
    **kwargs: float,
) -> CityConfig:
    """Build a CityConfig; nws_station is the default NWS station for diagnostics only."""
    return CityConfig(name, ticker, lat, lon, nws_station, tz, **kwargs)


# Original 5 cities — kept first so CITY_COUNT=5 selects exactly these.
ORIGINAL_CITIES: dict[str, CityConfig] = {
    "New York": _city("New York", "KXHIGHNY", 40.7128, -74.0060, "KNYC", "America/New_York", nws_bias_f=-1.5),
    "Chicago": _city("Chicago", "KXHIGHCHI", 41.8781, -87.6298, "KMDW", "America/Chicago", nws_bias_f=-1.5),
    "Miami": _city("Miami", "KXHIGHMIA", 25.7617, -80.1918, "KMIA", "America/New_York", nws_bias_f=-1.5),
    "Los Angeles": _city("Los Angeles", "KXHIGHLAX", 34.0522, -118.2437, "KLAX", "America/Los_Angeles", nws_bias_f=-1.5),
    "Denver": _city("Denver", "KXHIGHDEN", 39.7392, -104.9903, "KDEN", "America/Denver", nws_bias_f=-1.5),
}

# 8 expansion cities — enabled when CITY_COUNT >= 13.
EXTENDED_CITIES: dict[str, CityConfig] = {
    "Seattle": _city("Seattle", "KXHIGHTSEA", 47.6062, -122.3321, "KSEA", "America/Los_Angeles", nws_bias_f=-1.0),
    "San Francisco": _city("San Francisco", "KXHIGHTSFO", 37.7749, -122.4194, "KSFO", "America/Los_Angeles", nws_bias_f=-1.0),
    "Dallas": _city("Dallas", "KXHIGHTDAL", 32.7767, -96.7970, "KDFW", "America/Chicago", nws_bias_f=-1.5),
    "Minneapolis": _city("Minneapolis", "KXHIGHTMIN", 44.9778, -93.2650, "KMSP", "America/Chicago", nws_bias_f=-1.5),
    "Oklahoma City": _city("Oklahoma City", "KXHIGHTOKC", 35.4676, -97.5164, "KOKC", "America/Chicago", nws_bias_f=-1.5),
    "Atlanta": _city("Atlanta", "KXHIGHTATL", 33.7490, -84.3880, "KATL", "America/New_York", nws_bias_f=-1.5),
    "Boston": _city("Boston", "KXHIGHTBOS", 42.3601, -71.0589, "KBOS", "America/New_York", nws_bias_f=-1.5),
    "Washington DC": _city("Washington DC", "KXHIGHTDC", 38.9072, -77.0369, "KDCA", "America/New_York", nws_bias_f=-1.5),
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
        # Station lat/lon and gridpoint — permanent caches (coordinates never change).
        self._station_coords_cache: dict[str, tuple[float, float]] = {}
        self._station_gridpoint_cache: dict[str, tuple[str, int, int]] = {}
        # Hourly forecast bundle per (station_id, date): {"high": NwsForecast, "low": NwsForecast}
        self._forecast_cache: dict[tuple[str, str], tuple[dict[str, NwsForecast], datetime]] = {}
        cache_ttl_minutes = int(os.getenv("NWS_FORECAST_CACHE_TTL_MINUTES", "60"))
        self._cache_ttl = timedelta(minutes=cache_ttl_minutes)
        self._cache_lock = threading.Lock()
        self._nws_headers = {"User-Agent": self.nws_user_agent, "Accept": "application/geo+json"}

    def watched_cities(self) -> list[CityConfig]:
        """Return the city list to scan, controlled by the CITY_COUNT env flag."""
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

    def parse_settlement_station(self, rules_primary: str) -> str | None:
        """Extract the ICAO station code from contract rules text.

        Looks for patterns like:
        - "Los Angeles Airport, CA" -> KLAX
        - "Central Park" -> KNYC
        - "Chicago Midway" -> KMDW
        - "Denver International" -> KDEN
        - direct ICAO codes like "KLAX" or "KNYC"

        Returns ICAO code or None if not found.
        """
        icao_match = re.search(r"\b(K[A-Z]{3})\b", rules_primary)
        if icao_match:
            return icao_match.group(1)

        rules_lower = rules_primary.lower()
        for location, station in _LOCATION_TO_STATION.items():
            if location in rules_lower:
                return station

        return None

    def get_station_forecast(
        self,
        station_id: str,
        target_date: date,
        market_type: str,
        *,
        city: CityConfig,
    ) -> NwsForecast:
        """Return NWS hourly forecast high/low for a specific settlement station.

        Resolves the gridpoint from the station's own coordinates (not the city's
        general area lat/lon), then extracts peak heating / overnight low windows
        in the city's local timezone.
        """
        want = "high" if market_type.lower() == "high" else "low"
        cache_key = (station_id, str(target_date))
        with self._cache_lock:
            cached = self._forecast_cache.get(cache_key)
            if cached is not None:
                bundle, expires_at = cached
                if datetime.now() < expires_at:
                    return bundle[want]

        bundle = self._fetch_station_bundle(station_id, city, target_date)
        with self._cache_lock:
            self._forecast_cache[cache_key] = (bundle, datetime.now() + self._cache_ttl)
        return bundle[want]

    def get_station_forecast_temp(
        self,
        station_id: str,
        target_date: date,
        market_type: str,
        tz: str,
    ) -> float | None:
        """Return raw forecast temperature (°F) for a specific station without bias."""
        headers = self._nws_headers
        try:
            office, grid_x, grid_y = self._resolve_station_gridpoint(station_id, headers)
            url = f"{self.NWS_BASE_URL}/gridpoints/{office}/{grid_x},{grid_y}/forecast/hourly"
            response = self.session.get(url, headers=headers, timeout=self.timeout_seconds)
            response.raise_for_status()
            periods = response.json().get("properties", {}).get("periods", [])
            return self._extract_temp_from_periods(periods, target_date, market_type, tz)
        except Exception as exc:
            logging.warning("Station forecast temp failed for %s: %s", station_id, exc)
            return None

    def verify_contract_driven_station_parsing(self, kalshi_client: Any) -> bool:
        """Startup check: parse settlement stations from live Kalshi contract rules."""
        logging.info("Verifying contract-driven settlement station parsing...")
        all_passed = True
        for series_ticker, expected_station in _VERIFY_SERIES_EXPECTED:
            try:
                payload = kalshi_client._request(
                    "GET",
                    "/markets",
                    params={"series_ticker": series_ticker, "status": "open", "limit": 1},
                    auth_required=False,
                )
                markets = payload.get("markets", [])
                if not markets:
                    logging.warning("Station verify: no open market for %s", series_ticker)
                    all_passed = False
                    continue
                market = markets[0]
                ticker = str(market.get("ticker", ""))
                rules_primary = str(market.get("rules_primary", ""))
                if not rules_primary and hasattr(kalshi_client, "get_market_rules"):
                    rules_primary = str(kalshi_client.get_market_rules(ticker).get("rules_primary", ""))
                parsed = self.parse_settlement_station(rules_primary)
                if parsed == expected_station:
                    logging.info("[VERIFY] %s rules -> %s ✓", ticker, parsed)
                else:
                    all_passed = False
                    logging.warning(
                        "[VERIFY] %s rules -> %s (expected %s) | rules=%s",
                        ticker,
                        parsed,
                        expected_station,
                        rules_primary[:200],
                    )
            except Exception as exc:
                all_passed = False
                logging.warning("Station verify failed for %s: %s", series_ticker, exc)

        if all_passed:
            msg = "CONTRACT-DRIVEN STATION PARSING VERIFIED ✅"
            logging.info(msg)
            print(msg)
        return all_passed

    def log_forecast_vs_observation(
        self,
        city: CityConfig,
        *,
        station_id: str,
        target_date: date | None = None,
    ) -> None:
        """Log station forecast vs latest METAR for diagnostics (e.g. KLAX sanity check)."""
        target_date = target_date or datetime.now(ZoneInfo(city.tz)).date()
        forecast_high = self.get_station_forecast_temp(station_id, target_date, "high", city.tz)
        forecast_low = self.get_station_forecast_temp(station_id, target_date, "low", city.tz)

        obs = self.latest_station_observation(station_id)
        obs_temp_c: float | None = None
        if obs:
            obs_temp_c = self._safe_float((obs.get("properties") or {}).get("temperature", {}).get("value"))
        obs_temp_f = round(obs_temp_c * 9 / 5 + 32, 1) if obs_temp_c is not None else None

        # Also fetch area-grid forecast at city lat/lon for discrepancy visibility.
        area_high = self._area_grid_forecast_temp(city, target_date, "high")

        logging.info(
            "[FORECAST CHECK] %s (%s) date=%s | station HIGH=%s°F LOW=%s°F | "
            "METAR now=%s°F | area-grid HIGH=%s°F (city lat/lon, not settlement station)",
            city.name,
            station_id,
            target_date,
            forecast_high,
            forecast_low,
            obs_temp_f,
            area_high,
        )
        if forecast_high is not None and obs_temp_f is not None:
            delta = abs(forecast_high - obs_temp_f)
            if delta > 8:
                logging.warning(
                    "[FORECAST CHECK] %s station HIGH %.1f°F vs METAR now %.1f°F — "
                    "delta %.1f°F (expected within ~2-3°F only when peak has already occurred)",
                    city.name,
                    forecast_high,
                    obs_temp_f,
                    delta,
                )

    def _fetch_station_bundle(
        self,
        station_id: str,
        city: CityConfig,
        target_date: date,
    ) -> dict[str, NwsForecast]:
        """Fetch hourly forecast at the station gridpoint and build HIGH/LOW bundle."""
        empty = NwsForecast(None, "NWS station forecast fetch failed", station_id)
        try:
            office, grid_x, grid_y = self._resolve_station_gridpoint(station_id, self._nws_headers)
            url = f"{self.NWS_BASE_URL}/gridpoints/{office}/{grid_x},{grid_y}/forecast/hourly"
            response = self.session.get(url, headers=self._nws_headers, timeout=self.timeout_seconds)
            response.raise_for_status()
            periods = response.json().get("properties", {}).get("periods", [])
            return self._build_bundle_from_periods(station_id, city, target_date, periods)
        except Exception as exc:
            logging.warning("NWS station forecast failed for %s (%s): %s", city.name, station_id, exc)
            return {"high": empty, "low": empty}

    def _resolve_station_gridpoint(
        self,
        station_id: str,
        headers: dict[str, str],
    ) -> tuple[str, int, int]:
        """Return (office, gridX, gridY) for a station, caching forever after first lookup."""
        with self._cache_lock:
            cached = self._station_gridpoint_cache.get(station_id)
            if cached is not None:
                return cached

        lat, lon = self._resolve_station_coords(station_id, headers)
        point_url = f"{self.NWS_BASE_URL}/points/{lat},{lon}"
        response = self.session.get(point_url, headers=headers, timeout=self.timeout_seconds)
        response.raise_for_status()
        props = response.json()["properties"]
        gridpoint = (str(props["gridId"]), int(props["gridX"]), int(props["gridY"]))
        with self._cache_lock:
            self._station_gridpoint_cache[station_id] = gridpoint
        return gridpoint

    def _resolve_station_coords(self, station_id: str, headers: dict[str, str]) -> tuple[float, float]:
        """Return (lat, lon) for an NWS station, caching permanently."""
        with self._cache_lock:
            cached = self._station_coords_cache.get(station_id)
            if cached is not None:
                return cached

        station_url = f"{self.NWS_BASE_URL}/stations/{station_id}"
        response = self.session.get(station_url, headers=headers, timeout=self.timeout_seconds)
        response.raise_for_status()
        coords = response.json()["geometry"]["coordinates"]
        lon, lat = float(coords[0]), float(coords[1])
        with self._cache_lock:
            self._station_coords_cache[station_id] = (lat, lon)
        return lat, lon

    def _extract_temp_from_periods(
        self,
        periods: list[dict[str, Any]],
        target_date: date,
        market_type: str,
        tz_name: str,
    ) -> float | None:
        """Extract HIGH (max 12-20 local) or LOW (min 0-8 local) from hourly periods."""
        tz = ZoneInfo(tz_name)
        want_high = market_type.upper() == "HIGH"
        temps: list[float] = []
        all_day: list[float] = []

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
            all_day.append(temp)
            hour = local_dt.hour
            if want_high and 12 <= hour <= 20:
                temps.append(temp)
            elif not want_high and 0 <= hour <= 8:
                temps.append(temp)

        if temps:
            return max(temps) if want_high else min(temps)
        if all_day:
            return max(all_day) if want_high else min(all_day)
        return None

    def _build_bundle_from_periods(
        self,
        station_id: str,
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
            if 12 <= hour <= 20:
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
        if high_temp is not None:
            high_temp = self._apply_city_bias(high_temp, station_id, target_date, "high")
        low_temp = self._apply_bias(low_raw, city, target_date, "low")

        high_spread: float | None = None
        high_uncertain = False
        if len(high_temps) >= 3:
            high_spread = round(max(high_temps) - min(high_temps), 2)
            if high_spread > 4.0:
                high_uncertain = True
                logging.warning(
                    "High forecast spread %.1f°F for %s — elevated uncertainty",
                    high_spread,
                    station_id,
                )

        return {
            "high": NwsForecast(
                high_temp,
                high_short or "NWS station hourly",
                f"{station_id} via NWS station hourly",
                forecast_spread_f=high_spread,
                uncertain=high_uncertain,
            ),
            "low": NwsForecast(
                low_temp,
                low_short or "NWS station hourly",
                f"{station_id} via NWS station hourly",
            ),
        }

    def _area_grid_forecast_temp(self, city: CityConfig, target_date: date, market_type: str) -> float | None:
        """Legacy area-grid forecast at city lat/lon — for discrepancy logging only."""
        headers = self._nws_headers
        try:
            point_url = f"{self.NWS_BASE_URL}/points/{city.lat:.4f},{city.lon:.4f}"
            response = self.session.get(point_url, headers=headers, timeout=self.timeout_seconds)
            response.raise_for_status()
            props = response.json()["properties"]
            office, grid_x, grid_y = str(props["gridId"]), int(props["gridX"]), int(props["gridY"])
            url = f"{self.NWS_BASE_URL}/gridpoints/{office}/{grid_x},{grid_y}/forecast/hourly"
            response = self.session.get(url, headers=headers, timeout=self.timeout_seconds)
            response.raise_for_status()
            periods = response.json().get("properties", {}).get("periods", [])
            return self._extract_temp_from_periods(periods, target_date, market_type, city.tz)
        except Exception:
            return None

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

    def _apply_city_bias(
        self,
        forecast_f: float,
        station_id: str,
        target_date: date,
        market_type: str,
    ) -> float:
        """Apply documented NWS systematic bias correction."""
        if market_type.lower() != "high":
            return forecast_f
        if target_date.month not in (6, 7, 8):
            return forecast_f
        bias = CITY_BIAS_CORRECTIONS.get(station_id, 0.0)
        if bias != 0.0:
            logging.debug(
                "City bias correction %+.1f°F applied for %s: %.1f°F -> %.1f°F",
                bias,
                station_id,
                forecast_f,
                forecast_f + bias,
            )
        return round(forecast_f + bias, 2)

    def latest_station_observation(self, station_id: str) -> dict[str, Any] | None:
        """Fetch the latest station observation for debugging and audit logs."""
        url = f"{self.NWS_BASE_URL}/stations/{station_id}/observations/latest"
        try:
            response = self.session.get(url, headers=self._nws_headers, timeout=self.timeout_seconds)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            logging.warning("NWS station observation failed for %s: %s", station_id, exc)
            return None

    @staticmethod
    def _safe_float(value: Any) -> float | None:
        """Convert a value to float, returning None when conversion fails."""
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

"""
Real-time METAR observation tracker for Kalshi weather market trading.

Polls aviationweather.gov to track the running daily maximum temperature at each
settlement station. ASOS stations record temperature every minute; the running
daily max is a live preview of what the NWS Daily Climate Report will publish.
"""

from __future__ import annotations

import logging
import time
import zoneinfo
from datetime import date, datetime
from threading import Lock
from typing import Any

import requests


class MetarTracker:
    """Tracks real-time METAR observations and running daily maximums."""

    METAR_API = "https://aviationweather.gov/api/data/metar"
    HEADERS = {"User-Agent": "WhetherBot/1.0 weather-trading-bot"}

    STATION_TIMEZONES = {
        "KNYC": "America/New_York",
        "KMDW": "America/Chicago",
        "KMIA": "America/New_York",
        "KLAX": "America/Los_Angeles",
        "KDEN": "America/Denver",
        "KOKC": "America/Chicago",
        "KBOS": "America/New_York",
        "KDCA": "America/New_York",
        "KSEA": "America/Los_Angeles",
        "KSFO": "America/Los_Angeles",
        "KATL": "America/New_York",
        "KDFW": "America/Chicago",
        "KMSP": "America/Chicago",
    }

    def __init__(self) -> None:
        self._lock = Lock()
        self._daily_max: dict[str, dict[str, float]] = {}
        self._last_obs: dict[str, dict[str, Any]] = {}
        self._obs_history: dict[str, list[dict[str, Any]]] = {}

    def fetch_metar(self, station_id: str) -> dict[str, Any] | None:
        """Fetch latest METAR for a single station from aviationweather.gov."""
        try:
            resp = requests.get(
                self.METAR_API,
                params={"ids": station_id, "format": "json", "hours": 3},
                headers=self.HEADERS,
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            if not data:
                return None

            obs = data[0] if isinstance(data, list) else data
            temp_c = obs.get("temp")
            if temp_c is None:
                return None
            temp_f = round(temp_c * 9 / 5 + 32, 1)
            obs_time = obs.get("obsTime") or obs.get("time", "")

            return {
                "station_id": station_id,
                "temp_f": temp_f,
                "temp_c": temp_c,
                "obs_time_utc": obs_time,
                "wind_speed_kt": obs.get("wspd"),
                "wind_dir": obs.get("wdir"),
                "sky_cover": obs.get("sky", ""),
                "visibility": obs.get("visib"),
                "raw_metar": obs.get("rawOb", ""),
            }
        except Exception as exc:
            logging.debug("METAR fetch failed for %s: %s", station_id, exc)
            return None

    def update_station(self, station_id: str) -> dict[str, Any] | None:
        """Fetch latest METAR, update running daily max, return enriched observation."""
        obs = self.fetch_metar(station_id)
        if obs is None:
            return None

        temp_f = obs["temp_f"]
        today = date.today().isoformat()

        with self._lock:
            if station_id not in self._daily_max:
                self._daily_max[station_id] = {}
            current_max = self._daily_max[station_id].get(today, -999.0)
            new_max = max(current_max, temp_f)
            self._daily_max[station_id][today] = new_max

            self._last_obs[station_id] = obs

            if station_id not in self._obs_history:
                self._obs_history[station_id] = []
            self._obs_history[station_id].append({
                "temp_f": temp_f,
                "time": obs["obs_time_utc"],
            })
            self._obs_history[station_id] = self._obs_history[station_id][-24:]

            obs["daily_max_f"] = new_max
            obs["new_daily_max"] = new_max > current_max and current_max > -999.0
            obs["is_peak_heating_hour"] = self._is_peak_heating_hour(station_id)

        logging.info(
            "[METAR] %s | Current: %.1f°F | Daily max: %.1f°F | %s",
            station_id,
            temp_f,
            new_max,
            obs["obs_time_utc"],
        )
        return obs

    def get_daily_max(self, station_id: str, target_date: date | None = None) -> float | None:
        """Get the observed daily max for a station on a given date."""
        date_str = (target_date or date.today()).isoformat()
        with self._lock:
            return self._daily_max.get(station_id, {}).get(date_str)

    def get_last_observation(self, station_id: str) -> dict[str, Any] | None:
        """Get the most recent METAR observation for a station."""
        with self._lock:
            return self._last_obs.get(station_id)

    def get_temperature_trend(self, station_id: str) -> str | None:
        """Return 'rising', 'falling', 'stable', or None if insufficient data."""
        with self._lock:
            history = self._obs_history.get(station_id, [])
        if len(history) < 3:
            return None
        recent = [h["temp_f"] for h in history[-4:]]
        avg_change = (recent[-1] - recent[0]) / len(recent)
        if avg_change > 0.5:
            return "rising"
        if avg_change < -0.5:
            return "falling"
        return "stable"

    def assess_market_vs_observation(
        self,
        station_id: str,
        threshold_f: float,
        side: str,
        strike_type: str,
        market_type: str,
        hours_until_settlement: float,
    ) -> dict[str, Any]:
        """Compare observed daily max vs market threshold to assess trade quality."""
        obs_max = self.get_daily_max(station_id)
        last_obs = self.get_last_observation(station_id)
        trend = self.get_temperature_trend(station_id)

        if obs_max is None or last_obs is None:
            return {
                "resolved_direction": "uncertain",
                "confidence": 0.0,
                "recommendation": "hold",
                "reason": f"No METAR data available for {station_id}",
            }

        current_temp = last_obs["temp_f"]
        is_peak = self._is_peak_heating_hour(station_id)
        local_hour = self._get_local_hour(station_id)
        hours_of_heating_remaining = max(0, 16 - local_hour)

        result: dict[str, Any] = {
            "observed_max_f": obs_max,
            "current_temp_f": current_temp,
            "trend": trend,
            "local_hour": local_hour,
            "hours_of_heating_remaining": hours_of_heating_remaining,
            "is_peak_heating_hour": is_peak,
        }

        mt = market_type.upper()
        if mt == "HIGH" and strike_type == "greater":
            buffer = obs_max - threshold_f

            if obs_max > threshold_f:
                result.update({
                    "resolved_direction": "yes",
                    "confidence": min(1.0, 0.85 + (buffer / 10) * 0.15),
                    "recommendation": "strong_buy" if side == "yes" else "strong_sell",
                    "reason": (
                        f"Observed daily max {obs_max:.1f}°F already exceeds "
                        f"{threshold_f:.1f}°F threshold — market effectively resolved YES"
                    ),
                })
            elif obs_max > threshold_f - 2.0 and trend == "rising" and is_peak:
                result.update({
                    "resolved_direction": "uncertain",
                    "confidence": 0.65,
                    "recommendation": "buy" if side == "yes" else "sell",
                    "reason": (
                        f"Observed max {obs_max:.1f}°F, {threshold_f - obs_max:.1f}°F from threshold, "
                        "rising during peak heating"
                    ),
                })
            elif hours_of_heating_remaining < 2 and obs_max < threshold_f - 3.0:
                result.update({
                    "resolved_direction": "no",
                    "confidence": min(1.0, 0.80 + (threshold_f - obs_max - 3) / 10 * 0.15),
                    "recommendation": "strong_buy" if side == "no" else "strong_sell",
                    "reason": (
                        f"Only {hours_of_heating_remaining:.0f}h of heating left, observed max "
                        f"{obs_max:.1f}°F is {threshold_f - obs_max:.1f}°F below "
                        f"{threshold_f:.1f}°F threshold"
                    ),
                })
            else:
                result.update({
                    "resolved_direction": "uncertain",
                    "confidence": 0.5,
                    "recommendation": "hold",
                    "reason": (
                        f"Observed max {obs_max:.1f}°F, threshold {threshold_f:.1f}°F, "
                        f"{hours_of_heating_remaining:.0f}h heating remaining, trend={trend}"
                    ),
                })
        else:
            result.update({
                "resolved_direction": "uncertain",
                "confidence": 0.5,
                "recommendation": "hold",
                "reason": f"METAR assessment not configured for {mt}/{strike_type}",
            })

        return result

    def _is_peak_heating_hour(self, station_id: str) -> bool:
        """Return True if current local time is in peak heating window (10am-4pm)."""
        local_hour = self._get_local_hour(station_id)
        return 10 <= local_hour <= 16

    def _get_local_hour(self, station_id: str) -> int:
        """Get current local hour for a station."""
        tz_name = self.STATION_TIMEZONES.get(station_id, "America/New_York")
        tz = zoneinfo.ZoneInfo(tz_name)
        return datetime.now(tz).hour

    def update_all_stations(self, station_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Update METAR observations for all stations."""
        results: dict[str, dict[str, Any]] = {}
        for station_id in station_ids:
            obs = self.update_station(station_id)
            if obs:
                results[station_id] = obs
            time.sleep(0.5)
        return results

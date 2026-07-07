"""
Real-time METAR observation tracker for Kalshi weather market trading.

Polls aviationweather.gov to track running daily maximum and minimum temperatures
at each settlement station.
"""

from __future__ import annotations

import logging
import sqlite3
import time
import zoneinfo
from datetime import date, datetime
from pathlib import Path
from threading import Lock
from typing import Any

import requests


class MetarTracker:
    """Tracks real-time METAR observations and running daily max/min values."""

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

    def __init__(self, db_path: str | Path = "data/metar_obs.db") -> None:
        self._lock = Lock()
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._daily_max: dict[str, dict[str, float]] = {}
        self._daily_min: dict[str, dict[str, float]] = {}
        self._last_obs: dict[str, dict[str, Any]] = {}
        self._obs_history: dict[str, list[dict[str, Any]]] = {}
        self._init_db()
        self._load_daily_obs()

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
        """Fetch latest METAR, update running daily max/min, return enriched observation."""
        obs = self.fetch_metar(station_id)
        if obs is None:
            return None

        temp_f = obs["temp_f"]
        today = self._local_date(station_id)

        with self._lock:
            if station_id not in self._daily_max:
                self._daily_max[station_id] = {}
            if station_id not in self._daily_min:
                self._daily_min[station_id] = {}

            current_max = self._daily_max[station_id].get(today, -999.0)
            new_max = max(current_max, temp_f)
            self._daily_max[station_id][today] = new_max

            current_min = self._daily_min[station_id].get(today, 999.0)
            new_min = min(current_min, temp_f)
            self._daily_min[station_id][today] = new_min

            self._last_obs[station_id] = obs

            if station_id not in self._obs_history:
                self._obs_history[station_id] = []
            self._obs_history[station_id].append({
                "temp_f": temp_f,
                "time": obs["obs_time_utc"],
            })
            self._obs_history[station_id] = self._obs_history[station_id][-24:]

            obs["daily_max_f"] = new_max
            obs["daily_min_f"] = new_min
            obs["new_daily_max"] = new_max > current_max and current_max > -999.0
            obs["new_daily_min"] = new_min < current_min and current_min < 999.0
            obs["is_peak_heating_hour"] = self._is_peak_heating_hour(station_id)

            self._save_daily_obs(station_id, today, new_max, new_min)

        logging.info(
            "[METAR] %s | Current: %.1f°F | Daily max: %.1f°F | Daily min: %.1f°F | %s",
            station_id,
            temp_f,
            new_max,
            new_min,
            obs["obs_time_utc"],
        )
        return obs

    def get_daily_max(self, station_id: str, target_date: date | None = None) -> float | None:
        """Get the observed daily max for a station on a given date."""
        date_str = target_date.isoformat() if target_date else self._local_date(station_id)
        with self._lock:
            return self._daily_max.get(station_id, {}).get(date_str)

    def get_daily_min(self, station_id: str, target_date: date | None = None) -> float | None:
        """Get the observed daily min for a station on a given date."""
        date_str = target_date.isoformat() if target_date else self._local_date(station_id)
        with self._lock:
            return self._daily_min.get(station_id, {}).get(date_str)

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
        upper_threshold_f: float | None = None,
    ) -> dict[str, Any]:
        """Compare observed daily max/min vs market threshold to assess trade quality."""
        obs_max = self.get_daily_max(station_id)
        obs_min = self.get_daily_min(station_id)
        last_obs = self.get_last_observation(station_id)
        trend = self.get_temperature_trend(station_id)

        if obs_max is None or obs_min is None or last_obs is None:
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
        hours_of_cooling_remaining = self._hours_of_cooling_remaining(station_id)
        strike = strike_type.lower()
        mt = market_type.upper()
        upper = upper_threshold_f if upper_threshold_f is not None else threshold_f + 2.0

        result: dict[str, Any] = {
            "observed_max_f": obs_max,
            "observed_min_f": obs_min,
            "current_temp_f": current_temp,
            "trend": trend,
            "local_hour": local_hour,
            "hours_of_heating_remaining": hours_of_heating_remaining,
            "hours_of_cooling_remaining": hours_of_cooling_remaining,
            "is_peak_heating_hour": is_peak,
        }

        if strike == "between":
            lower = threshold_f
            upper = upper_threshold_f if upper_threshold_f is not None else threshold_f + 2.0

            if obs_max > upper:
                result.update({
                    "resolved_direction": "no",
                    "confidence": 0.95,
                    "recommendation": "strong_buy" if side == "no" else "strong_sell",
                    "reason": (
                        f"Observed max {obs_max:.1f}°F already exceeded bracket ceiling "
                        f"{upper:.1f}°F — YES impossible"
                    ),
                })
            elif lower <= obs_max <= upper:
                if trend == "rising" and is_peak:
                    result.update({
                        "resolved_direction": "uncertain",
                        "confidence": 0.55,
                        "recommendation": "hold",
                        "reason": (
                            f"Observed max {obs_max:.1f}°F currently IN bracket "
                            f"[{lower:.1f}-{upper:.1f}°F] but still rising"
                        ),
                    })
                elif trend in ("stable", "falling") and hours_of_heating_remaining < 2:
                    result.update({
                        "resolved_direction": "yes",
                        "confidence": 0.75,
                        "recommendation": "strong_buy" if side == "yes" else "strong_sell",
                        "reason": (
                            f"Observed max {obs_max:.1f}°F stable in bracket "
                            f"[{lower:.1f}-{upper:.1f}°F] with little heating left"
                        ),
                    })
                else:
                    result.update({
                        "resolved_direction": "uncertain",
                        "confidence": 0.5,
                        "recommendation": "hold",
                        "reason": (
                            f"Observed max {obs_max:.1f}°F in bracket [{lower:.1f}-{upper:.1f}°F], "
                            f"trend={trend}, {hours_of_heating_remaining:.0f}h heating left"
                        ),
                    })
            elif obs_max < lower and hours_of_heating_remaining < 1:
                result.update({
                    "resolved_direction": "no",
                    "confidence": 0.90,
                    "recommendation": "strong_buy" if side == "no" else "strong_sell",
                    "reason": (
                        f"Observed max {obs_max:.1f}°F below bracket floor {lower:.1f}°F "
                        "with heating over"
                    ),
                })
            else:
                result.update({
                    "resolved_direction": "uncertain",
                    "confidence": 0.5,
                    "recommendation": "hold",
                    "reason": (
                        f"Observed max {obs_max:.1f}°F, bracket [{lower:.1f}-{upper:.1f}°F], "
                        f"{hours_of_heating_remaining:.0f}h heating remaining"
                    ),
                })

        elif mt == "HIGH" and strike == "greater":
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

        elif mt == "LOW" and strike == "greater":
            if obs_min > threshold_f:
                result.update({
                    "resolved_direction": "yes",
                    "confidence": min(1.0, 0.85 + (obs_min - threshold_f) / 10 * 0.15),
                    "recommendation": "strong_buy" if side == "yes" else "strong_sell",
                    "reason": (
                        f"Observed daily min {obs_min:.1f}°F already exceeds "
                        f"{threshold_f:.1f}°F threshold — YES effectively resolved"
                    ),
                })
            elif hours_of_cooling_remaining < 2 and obs_min <= threshold_f:
                result.update({
                    "resolved_direction": "no",
                    "confidence": min(1.0, 0.80 + (threshold_f - obs_min) / 10 * 0.15),
                    "recommendation": "strong_buy" if side == "no" else "strong_sell",
                    "reason": (
                        f"Only {hours_of_cooling_remaining:.0f}h of cooling left, daily min "
                        f"{obs_min:.1f}°F has not exceeded {threshold_f:.1f}°F — NO likely"
                    ),
                })
            else:
                result.update({
                    "resolved_direction": "uncertain",
                    "confidence": 0.5,
                    "recommendation": "hold",
                    "reason": (
                        f"Observed min {obs_min:.1f}°F, threshold {threshold_f:.1f}°F, "
                        f"{hours_of_cooling_remaining:.0f}h cooling remaining, trend={trend}"
                    ),
                })

        elif mt == "LOW" and strike in {"less", "less_than", "less than"}:
            if obs_min < threshold_f:
                result.update({
                    "resolved_direction": "yes",
                    "confidence": min(1.0, 0.85 + (threshold_f - obs_min) / 10 * 0.15),
                    "recommendation": "strong_buy" if side == "yes" else "strong_sell",
                    "reason": (
                        f"Observed daily min {obs_min:.1f}°F already below "
                        f"{threshold_f:.1f}°F threshold — YES confirmed"
                    ),
                })
            elif current_temp > threshold_f + 10 and hours_of_cooling_remaining < 3:
                result.update({
                    "resolved_direction": "no",
                    "confidence": 0.75,
                    "recommendation": "strong_buy" if side == "no" else "strong_sell",
                    "reason": (
                        f"Current temp {current_temp:.1f}°F far above {threshold_f:.1f}°F "
                        f"with only {hours_of_cooling_remaining:.0f}h cooling left — NO likely"
                    ),
                })
            else:
                result.update({
                    "resolved_direction": "uncertain",
                    "confidence": 0.5,
                    "recommendation": "hold",
                    "reason": (
                        f"Observed min {obs_min:.1f}°F, threshold {threshold_f:.1f}°F, "
                        f"current {current_temp:.1f}°F, trend={trend}"
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

    def _init_db(self) -> None:
        """Create the METAR daily observation table if needed."""
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS metar_daily_obs (
                    station_id TEXT NOT NULL,
                    date TEXT NOT NULL,
                    daily_max_f REAL NOT NULL,
                    daily_min_f REAL NOT NULL,
                    PRIMARY KEY (station_id, date)
                )
                """
            )

    def _load_daily_obs(self) -> None:
        """Load persisted daily max/min values from SQLite."""
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(
                "SELECT station_id, date, daily_max_f, daily_min_f FROM metar_daily_obs"
            ).fetchall()
        for station_id, obs_date, daily_max, daily_min in rows:
            self._daily_max.setdefault(station_id, {})[obs_date] = float(daily_max)
            self._daily_min.setdefault(station_id, {})[obs_date] = float(daily_min)

    def _save_daily_obs(
        self,
        station_id: str,
        obs_date: str,
        daily_max: float,
        daily_min: float,
    ) -> None:
        """Persist daily max/min after each observation update."""
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """
                INSERT INTO metar_daily_obs (station_id, date, daily_max_f, daily_min_f)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(station_id, date) DO UPDATE SET
                    daily_max_f = excluded.daily_max_f,
                    daily_min_f = excluded.daily_min_f
                """,
                (station_id, obs_date, daily_max, daily_min),
            )

    def _local_date(self, station_id: str) -> str:
        """Return today's date in the station's local timezone."""
        tz_name = self.STATION_TIMEZONES.get(station_id, "America/New_York")
        tz = zoneinfo.ZoneInfo(tz_name)
        return datetime.now(tz).date().isoformat()

    def _hours_of_cooling_remaining(self, station_id: str) -> float:
        """Estimate hours until overnight low formation completes."""
        local_hour = self._get_local_hour(station_id)
        if local_hour >= 18:
            return float(max(0, 24 - local_hour + 6))
        if local_hour <= 6:
            return float(max(0, 6 - local_hour))
        return 0.0

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

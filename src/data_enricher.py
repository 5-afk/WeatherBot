"""Real-time data enrichment for Claude's trade sanity checks."""

from __future__ import annotations

import os
from typing import Any

import requests
from anthropic import Anthropic

from src.weather_client import CityConfig


class DataEnricher:
    """Fetch live alerts, station observations, and web context for Claude."""

    NWS_BASE_URL = "https://api.weather.gov"

    def __init__(self) -> None:
        """Create reusable API settings for enrichment requests."""
        self.headers = {"User-Agent": "kalshi-weather-bot/1.0", "Accept": "application/geo+json"}
        self.timeout_seconds = 20
        self.api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        self.model = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5").strip()

    def get_nws_alerts(self, city: CityConfig) -> list[dict]:
        """Return active NWS alerts near the city's settlement station."""
        try:
            response = requests.get(
                f"{self.NWS_BASE_URL}/alerts/active",
                params={"point": f"{city.lat},{city.lon}"},
                headers=self.headers,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            features = response.json().get("features", [])
            alerts = []
            for feature in features:
                properties = feature.get("properties", {})
                description = str(properties.get("description", ""))
                alerts.append(
                    {
                        "title": properties.get("headline") or properties.get("event"),
                        "severity": properties.get("severity"),
                        "urgency": properties.get("urgency"),
                        "description": description[:200],
                        "event": properties.get("event"),
                    }
                )
            return alerts
        except Exception:
            return []

    def get_station_observation(self, city: CityConfig) -> dict:
        """Return the latest station observation for the configured NWS station."""
        try:
            response = requests.get(
                f"{self.NWS_BASE_URL}/stations/{city.nws_station}/observations/latest",
                headers=self.headers,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            properties = response.json().get("properties", {})
            temperature_c = self._value(properties.get("temperature"))
            humidity = self._value(properties.get("relativeHumidity"))
            wind_speed_mps = self._value(properties.get("windSpeed"))
            wind_direction = self._value(properties.get("windDirection"))
            return {
                "current_temp_f": None if temperature_c is None else temperature_c * 9 / 5 + 32,
                "humidity": humidity,
                "wind_speed_mph": None if wind_speed_mps is None else wind_speed_mps * 2.23694,
                "wind_direction": wind_direction,
                "description": properties.get("textDescription"),
                "observed_at": properties.get("timestamp"),
            }
        except Exception:
            return {}

    def get_web_context(self, city_name: str, target_date: str) -> str:
        """Ask Claude with web search for notable weather context."""
        if not self.api_key or self.api_key == "your_key_here":
            return "Web context unavailable."

        tools = [{
            "type": "web_search_20250305",
            "name": "web_search"
        }]

        try:
            client = Anthropic(api_key=self.api_key)
            message = client.messages.create(
                model=self.model,
                max_tokens=150,
                tools=tools,
                messages=[
                    {
                        "role": "user",
                        "content": (
                            f"{city_name} weather {target_date} forecast temperature unusual\n\n"
                            "Return a 2-3 sentence plain text summary of relevant weather news. "
                            "If nothing notable is found, say: No unusual weather events reported."
                        ),
                    }
                ],
            )
            summary = self._message_text(message)
            return summary or "No unusual weather events reported."
        except Exception:
            return "Web context unavailable."

    def enrich(self, city: CityConfig, target_date: str) -> dict:
        """Return combined real-time context for a trade sanity check."""
        alerts = self.get_nws_alerts(city)
        observation = self.get_station_observation(city)
        web_context = self.get_web_context(city.name, target_date)
        return {
            "active_alerts": alerts,
            "alert_count": len(alerts),
            "has_severe_alert": any(alert.get("severity") in ["Extreme", "Severe"] for alert in alerts),
            "current_observation": observation,
            "current_temp_f": observation.get("current_temp_f"),
            "web_context": web_context,
        }

    def _message_text(self, message: Any) -> str:
        """Extract plain text from an Anthropic message response."""
        text_parts = []
        for block in getattr(message, "content", []):
            text = getattr(block, "text", None)
            if text:
                text_parts.append(text.strip())
        return " ".join(part for part in text_parts if part).strip()

    def _value(self, measurement: Any) -> float | None:
        """Extract a numeric value from an NWS measurement object."""
        if not isinstance(measurement, dict):
            return None
        value = measurement.get("value")
        try:
            return None if value is None else float(value)
        except (TypeError, ValueError):
            return None

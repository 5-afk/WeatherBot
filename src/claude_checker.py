"""Claude Haiku sanity checker for proposed trades."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any

from anthropic import Anthropic


@dataclass(frozen=True)
class ClaudeDecision:
    """Structured GO/NOGO result returned to the trader."""

    decision: str
    reason: str

    @property
    def approved(self) -> bool:
        """Return True when Claude explicitly approved the trade."""
        return self.decision == "GO"


class ClaudeChecker:
    """Call Claude only for trades that already passed numeric filters."""

    SYSTEM_PROMPT = (
        "You are a trading risk checker for a Kalshi weather prediction market bot. "
        "You will receive weather forecast data and a proposed bet. Respond with "
        "only JSON: {decision: 'GO' or 'NOGO', reason: 'one sentence'}. Be "
        "conservative -- only approve bets with strong data agreement."
    )

    def __init__(self) -> None:
        """Create the checker and read Anthropic settings from env vars."""
        self.api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        self.model = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5").strip()
        self.max_tokens = 120

    def check(self, payload: dict[str, Any]) -> ClaudeDecision:
        """Send the proposed bet to Claude and return GO or NOGO."""
        if not self.api_key or self.api_key == "your_key_here":
            return ClaudeDecision("NOGO", "Anthropic API key is not configured.")

        try:
            client = Anthropic(api_key=self.api_key)
            message = client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=self.SYSTEM_PROMPT,
                messages=[{"role": "user", "content": json.dumps(payload, indent=2, default=str)}],
            )
            text = self._extract_text(message)
            parsed = json.loads(text)
            decision = str(parsed.get("decision", "NOGO")).upper()
            reason = str(parsed.get("reason", "Claude did not provide a reason."))
            if decision not in {"GO", "NOGO"}:
                return ClaudeDecision("NOGO", "Claude returned an invalid decision.")
            return ClaudeDecision(decision, reason)
        except Exception as exc:
            logging.warning("Claude sanity check failed safe: %s", exc)
            return ClaudeDecision("NOGO", f"Claude API failed safe: {exc}")

    def _extract_text(self, message: Any) -> str:
        """Extract the text block from an Anthropic SDK response."""
        parts = getattr(message, "content", [])
        if not parts:
            return "{}"
        return str(getattr(parts[0], "text", "{}")).strip()

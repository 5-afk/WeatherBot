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
        "The bot places ONE bet per day using its full daily budget. "
        "This is the most important trade of the day — be extra conservative. "
        "You receive weather forecast data, real-time station observations, "
        "active NWS alerts, and web context. "
        "You will receive the account balance and what percentage of it this bet represents. "
        "Reject any bet that represents more than 80% of the account balance. "
        "Be extra conservative when account balance is below $50. "
        "Respond with ONLY raw JSON, no markdown, no code fences: "
        "{\"decision\": \"GO\" or \"NOGO\", \"reason\": \"one sentence\"}. "
        "Approve ONLY when: both models strongly agree (>75% confidence), "
        "NWS confirms direction, zero severe alerts active, "
        "current conditions support forecast, and edge exceeds 15%. "
        "Reject if: ANY severe alert active, models below 75% agreement, "
        "web context shows unusual weather, or edge is borderline."
    )

    def __init__(self) -> None:
        """Create the checker and read Anthropic settings from env vars."""
        self.api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        self.model = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5").strip()
        self.max_tokens = 120

    def check(self, payload: dict[str, Any], balance: float | None = None) -> ClaudeDecision:
        """Send the proposed bet to Claude and return GO or NOGO."""
        if balance is not None:
            payload["account_balance_usd"] = round(balance, 2)
            payload["bet_as_pct_of_balance"] = round(
                (payload.get("proposed_stake", 0) / balance * 100), 1
            ) if balance > 0 else None
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
            # Extract text safely from response
            text = ""
            for block in message.content:
                if hasattr(block, "text") and block.text:
                    text = block.text.strip()
                    break
            if not text:
                return ClaudeDecision("NOGO", "Claude returned empty response.")
            # Strip markdown code fences if present
            text = text.strip()
            if text.startswith("```"):
                lines = text.split("\n")
                text = "\n".join(lines[1:-1]) if len(lines) > 2 else text
            parsed = json.loads(text)
            decision = str(parsed.get("decision", "NOGO")).upper()
            reason = str(parsed.get("reason", "No reason provided."))
            if decision not in {"GO", "NOGO"}:
                return ClaudeDecision("NOGO", "Claude returned invalid decision.")
            return ClaudeDecision(decision, reason)
        except json.JSONDecodeError as exc:
            logging.warning("Claude JSON parse failed: %s | raw text: %s", exc, text[:200] if text else "empty")
            return ClaudeDecision("NOGO", f"Claude JSON parse failed: {exc}")
        except Exception as exc:
            logging.warning("Claude sanity check failed safe: %s", exc)
            return ClaudeDecision("NOGO", f"Claude API failed safe: {exc}")

    def _extract_text(self, message: Any) -> str:
        """Extract the first non-empty text block from an Anthropic response."""
        parts = getattr(message, "content", [])
        for part in parts:
            text = getattr(part, "text", None)
            if text and text.strip():
                return text.strip()
        return '{"decision": "NOGO", "reason": "Empty response from Claude."}'

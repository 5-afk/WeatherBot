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
    ticker: str | None = None

    @property
    def approved(self) -> bool:
        """Return True when Claude explicitly approved the trade."""
        return self.decision == "GO"


class ClaudeChecker:
    """Call Claude only for trades that already passed numeric filters."""

    CALIBRATION_NOTES = (
        "CITY-SPECIFIC CALIBRATION NOTES (use these when evaluating candidates): "
        "- Oklahoma City (KOKC): NWS underforecasts July highs by ~1.8°F on average. "
        "If NWS says 95°F in OKC in July, true expected high is closer to 96.8°F. "
        "Be MORE cautious about NO bets on OKC high temperatures in July. "
        "- Los Angeles (KLAX): NWS overforecasts LAX highs by ~2.5°F in summer. "
        "If NWS says 78°F, true expected high is closer to 75.5°F. "
        "Be MORE confident about NO bets on LAX high temperature thresholds. "
        "- Miami (KMIA): Very low forecast uncertainty (sigma ~1.9°F). "
        "Miami temperature is highly predictable — large buffers are not needed. "
        "A 3°F buffer in Miami is equivalent to a 5°F buffer in Denver. "
        "- Denver (KDEN): Very high forecast uncertainty (sigma ~4.6°F). "
        "Require at least 7°F buffer before approving any Denver trade. "
        "Denver weather is highly variable — be conservative. "
        "- San Francisco (KSFO): NWS overforecasts by ~2.0°F in summer due to marine layer. "
        "Similar to LAX — be more confident about NO bets on high thresholds."
    )

    SYSTEM_PROMPT = (
        "You are a trading risk checker for a Kalshi weather prediction market bot. "
        "The bot places ONE bet per day using its full daily budget. "
        "This is the most important trade of the day — be extra conservative. "
        "You receive weather forecast data, real-time station observations, "
        "active NWS alerts, and web context. "
        "You will receive the account balance and what percentage of it this bet represents. "
        "Reject any bet that represents more than 80% of the account balance. "
        "Be extra conservative when account balance is below $50. "
        "You will receive a combined signal_score from 0.0-1.0. "
        "Only approve bets with signal_score >= 0.65. "
        "Buffer score measures forecast distance from threshold — below 0.3 always reject. "
        "Observation score measures live temperature alignment — above 0.9 is near-certain. "
        "Respond with ONLY raw JSON, no markdown, no code fences: "
        "{\"decision\": \"GO\" or \"NOGO\", \"reason\": \"one sentence\"}. "
        "Approve ONLY when: the NWS station forecast strongly favors the outcome "
        "(model probability > 75%), NWS confirms direction, zero severe alerts active, "
        "current conditions support forecast, and edge exceeds 15%. "
        "Reject if: ANY severe alert active, NWS model probability below 75%, "
        "web context shows unusual weather, or edge is borderline. "
        "You will receive rules_primary for each candidate — the official Kalshi contract rules. "
        "Before approving any trade you MUST: "
        "1. Confirm the settlement source matches our forecast source (NWS station), "
        "2. Confirm the settlement station (e.g. KLAX) matches the city station we used for forecasting, "
        "3. Confirm the expiration time gives enough time for the outcome to be determined, "
        "4. Confirm the payout criterion (greater than, less than, between) matches the direction of our bet. "
        "If any of these do not match, return NOGO with reason \"Settlement source mismatch\". "
        "IMPORTANT BIAS: Statistically, on Kalshi temperature markets, far more contracts "
        "resolve NO than YES — because the temperature lands in only one bracket per day, "
        "making all other brackets NO. When evaluating candidates of similar quality, "
        "STRONGLY prefer the NO side. Only approve YES when the NWS forecast places the "
        "temperature clearly and confidently above the threshold with a large buffer. "
        "A NO bet means the temperature will NOT reach or exceed the threshold — "
        "this is correct more often than YES on any given bracket. "
        "PROFITABILITY REQUIREMENTS — enforce these on every candidate: "
        "Minimum return: every bet must have potential profit >= 100% of stake (2x total payout minimum). "
        "This means only approve contracts priced between $0.15 and $0.47. "
        "If profit_if_wins < stake, return NOGO — the payout doesn't justify the risk. "
        "The profit_if_wins field in the payload shows exact dollar profit if the bet wins. "
        "Prefer candidates where profit_if_wins >= 1.5x stake (150%+ return). "
        "Never approve a bet where the total profit is less than $5.00 — it is not worth the risk. "
        + CALIBRATION_NOTES
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

    def check_batch(
        self,
        candidates: list[dict[str, Any]],
        balance: float | None = None,
    ) -> ClaudeDecision:
        """
        Evaluate all candidates in one API call.
        Claude picks the best one or rejects all.
        """
        if not self.api_key or self.api_key == "your_key_here":
            return ClaudeDecision("NOGO", "API key not configured.")
        if not candidates:
            return ClaudeDecision("NOGO", "No candidates.")

        try:
            client = Anthropic(api_key=self.api_key)
            payload = {
                "account_balance_usd": balance,
                "candidates": candidates,
            }
            message = client.messages.create(
                model=self.model,
                max_tokens=200,
                system=(
                    "You are a Kalshi weather trading risk checker. "
                    "You receive a list of trade candidates that have already "
                    "passed strict math filters. Pick the SINGLE best trade "
                    "or reject all. Respond with ONLY raw JSON, no markdown: "
                    "{\"decision\": \"GO\" or \"NOGO\", "
                    "\"ticker\": \"the ticker you pick or null\", "
                    "\"reason\": \"one sentence\"}. "
                    "side can be YES or NO; a NO bet means the temperature will NOT reach the threshold. "
                    "For LOW markets, YES means the temperature stays BELOW the threshold. "
                    "HIGH markets: YES means temperature exceeds threshold. "
                    "You will receive expected_value_per_contract for each candidate. "
                    "Prefer candidates with EV > 0.10 per contract. "
                    "Never approve candidates with EV < 0.05. "
                    "EV already accounts for both win probability and payout size — "
                    "it is the single best measure of trade quality. "
                    "Approve only if: signal_score >= 0.75, large buffer, "
                    "high confidence, no severe alerts. "
                    "Reject all if any severe weather alert is active. "
                    "You will receive rules_primary for each candidate — the official Kalshi contract rules. "
                    "Before approving any trade you MUST: "
                    "1. Confirm the settlement source matches our forecast source (NWS station), "
                    "2. Confirm the settlement station (e.g. KLAX) matches the city station we used for forecasting, "
                    "3. Confirm the expiration time gives enough time for the outcome to be determined, "
                    "4. Confirm the payout criterion (greater than, less than, between) matches the direction of our bet. "
                    "If any of these do not match, return NOGO with reason \"Settlement source mismatch\". "
                    "IMPORTANT BIAS: Statistically, on Kalshi temperature markets, far more contracts "
                    "resolve NO than YES — because the temperature lands in only one bracket per day, "
                    "making all other brackets NO. When evaluating candidates of similar quality, "
                    "STRONGLY prefer the NO side. Only approve YES when the NWS forecast places the "
                    "temperature clearly and confidently above the threshold with a large buffer. "
                    "A NO bet means the temperature will NOT reach or exceed the threshold — "
                    "this is correct more often than YES on any given bracket. "
                    "PROFITABILITY REQUIREMENTS — enforce these on every candidate: "
                    "Minimum return: every bet must have potential profit >= 100% of stake (2x total payout minimum). "
                    "This means only approve contracts priced between $0.15 and $0.47. "
                    "If profit_if_wins < stake, return NOGO — the payout doesn't justify the risk. "
                    "The profit_if_wins field in the payload shows exact dollar profit if the bet wins. "
                    "Prefer candidates where profit_if_wins >= 1.5x stake (150%+ return). "
                    "Never approve a bet where the total profit is less than $5.00 — it is not worth the risk. "
                    + ClaudeChecker.CALIBRATION_NOTES
                ),
                messages=[{
                    "role": "user",
                    "content": json.dumps(payload, default=str),
                }],
            )
            text = self._extract_text(message)
            if text.startswith("```"):
                lines = text.split("\n")
                text = "\n".join(lines[1:-1]) if len(lines) > 2 else text
            parsed = json.loads(text)
            decision = str(parsed.get("decision", "NOGO")).upper()
            reason = str(parsed.get("reason", "No reason."))
            ticker = parsed.get("ticker")
            if decision not in {"GO", "NOGO"}:
                return ClaudeDecision("NOGO", "Invalid decision.")
            return ClaudeDecision(decision, reason, str(ticker) if ticker else None)
        except Exception as exc:
            logging.warning("Claude batch check failed: %s", exc)
            return ClaudeDecision("NOGO", f"Claude failed safe: {exc}")

    def _extract_text(self, message: Any) -> str:
        """Extract the first non-empty text block from an Anthropic response."""
        parts = getattr(message, "content", [])
        for part in parts:
            text = getattr(part, "text", None)
            if text and text.strip():
                return text.strip()
        return '{"decision": "NOGO", "reason": "Empty response from Claude."}'

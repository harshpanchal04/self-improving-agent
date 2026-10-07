"""
state.py — SessionState dataclass tracking per-session conversation and tool state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SessionState:
    """Holds all mutable state for a single scheduling session."""

    # ── Booking state flags ────────────────────────────────────────────────────
    availability_checked: bool = False
    insurance_verified: bool = False
    booked: bool = False
    slot_id: str | None = None
    patient_id: str | None = None

    # ── Tool call log ──────────────────────────────────────────────────────────
    # Each entry: {tool, args, result, index (0-based sequential), error (bool)}
    tool_call_log: list[dict[str, Any]] = field(default_factory=list)

    # ── Conversation transcript ────────────────────────────────────────────────
    # Each entry: {role: 'user'|'assistant'|'tool'|'guardrail', content: str, ...}
    # Tool entries also carry tool_call_id and tool_name for API reconstruction.
    transcript: list[dict[str, Any]] = field(default_factory=list)

    def reset(self) -> None:
        """Reset all fields to their defaults."""
        self.availability_checked = False
        self.insurance_verified = False
        self.booked = False
        self.slot_id = None
        self.patient_id = None
        self.tool_call_log = []
        self.transcript = []

    def to_dict(self) -> dict[str, Any]:
        """Serialize state to a JSON-compatible dict."""
        return {
            "availability_checked": self.availability_checked,
            "insurance_verified": self.insurance_verified,
            "booked": self.booked,
            "slot_id": self.slot_id,
            "patient_id": self.patient_id,
            "tool_call_log": self.tool_call_log,
            "transcript": self.transcript,
        }

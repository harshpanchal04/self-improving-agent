"""
tools.py — Tool implementations, mock dispatch, and OpenAI-format schemas.

Each tool function signature:
    tool_name(state: SessionState, mock_config: dict | None = None, **kwargs) -> dict

mock_config maps tool_name → one of:
  - a response dict  (returned directly)
  - str starting with 'RAISE_EXCEPTION:'  (raises ToolExecutionError)
  - a list of the above  (consumed one item per call; first item popped each call)
"""

from __future__ import annotations

import json
from typing import Any

from state import SessionState


# ── Exception ──────────────────────────────────────────────────────────────────


class ToolExecutionError(Exception):
    """Raised when a tool fails (real or mock-injected failure)."""


# ── Internal helper ────────────────────────────────────────────────────────────


def _resolve_mock(mock_config: dict | None, tool_name: str) -> dict | None:
    """
    Extract the mock response for tool_name from mock_config.

    Handles:
      - plain dict response
      - 'RAISE_EXCEPTION:message' string
      - list of the above (consumed in order; stored back into mock_config)

    Returns the response dict, or None if no mock applies.
    Raises ToolExecutionError if the mock specifies an exception.
    """
    if mock_config is None or tool_name not in mock_config:
        return None

    entry = mock_config[tool_name]

    # List: pop the first item for this call
    if isinstance(entry, list):
        if not entry:
            return None
        item = entry.pop(0)
    else:
        item = entry

    # Exception injection via string marker
    if isinstance(item, str) and item.startswith("RAISE_EXCEPTION:"):
        msg = item[len("RAISE_EXCEPTION:"):]
        raise ToolExecutionError(msg)

    # Exception injection via __raise_exception__ key in dict
    if isinstance(item, dict) and "__raise_exception__" in item:
        raise ToolExecutionError(item["__raise_exception__"])

    return item


def _log_tool_call(
    state: SessionState,
    tool_name: str,
    args: dict,
    result: dict,
    error: bool = False,
) -> None:
    """Append an entry to state.tool_call_log."""
    state.tool_call_log.append(
        {
            "tool": tool_name,
            "args": args,
            "result": result,
            "index": len(state.tool_call_log),  # 0-based sequential call number
            "error": error,
        }
    )


# ── Tool functions ─────────────────────────────────────────────────────────────


def check_availability(
    state: SessionState,
    mock_config: dict | None = None,
    date: str | None = None,
    time: str | None = None,
) -> dict:
    """
    Check whether a specific appointment slot is available.

    Mutates: state.availability_checked = True
    """
    args = {"date": date, "time": time}
    mock = _resolve_mock(mock_config, "check_availability")

    if mock is not None:
        result = mock
    else:
        # Default realistic response for live use
        result = {
            "available": True,
            "slot_id": "SLOT-DEFAULT-001",
            "provider": "Dr. Smith",
            "duration_minutes": 30,
        }

    state.availability_checked = True
    _log_tool_call(state, "check_availability", args, result)
    return result


def verify_insurance(
    state: SessionState,
    mock_config: dict | None = None,
    patient_id: str | None = None,
) -> dict:
    """
    Verify a patient's insurance coverage.

    Mutates: state.insurance_verified = True
    """
    args = {"patient_id": patient_id}
    mock = _resolve_mock(mock_config, "verify_insurance")

    if mock is not None:
        result = mock
    else:
        result = {
            "verified": True,
            "insurance_provider": "Aetna",
            "plan": "PPO Silver",
            "copay": "$30",
        }

    state.insurance_verified = True
    _log_tool_call(state, "verify_insurance", args, result)
    return result


def book_appointment(
    state: SessionState,
    mock_config: dict | None = None,
    patient_id: str | None = None,
    slot_id: str | None = None,
) -> dict:
    """
    Book an appointment for a patient in a specific slot.

    Mutates: state.booked = True, state.slot_id, state.patient_id
    """
    args = {"patient_id": patient_id, "slot_id": slot_id}
    mock = _resolve_mock(mock_config, "book_appointment")

    if mock is not None:
        result = mock
    else:
        result = {
            "confirmed": True,
            "appointment_id": "APT-DEFAULT-001",
            "date": "2026-10-15",
            "time": "10:00 AM",
            "provider": "Dr. Smith",
        }

    state.booked = True
    state.slot_id = slot_id
    state.patient_id = patient_id
    _log_tool_call(state, "book_appointment", args, result)
    return result


def cancel_appointment(
    state: SessionState,
    mock_config: dict | None = None,
    appointment_id: str | None = None,
) -> dict:
    """
    Cancel an existing appointment.

    Mutates: state.booked = False, state.slot_id = None
    """
    args = {"appointment_id": appointment_id}
    mock = _resolve_mock(mock_config, "cancel_appointment")

    if mock is not None:
        result = mock
    else:
        result = {
            "cancelled": True,
            "appointment_id": appointment_id,
        }

    state.booked = False
    state.slot_id = None
    _log_tool_call(state, "cancel_appointment", args, result)
    return result


# ── Tool schemas (OpenAI format) ───────────────────────────────────────────────


def get_tool_schemas() -> list[dict[str, Any]]:
    """Return OpenAI-format function definitions for all 4 tools."""
    return [
        {
            "type": "function",
            "function": {
                "name": "check_availability",
                "description": (
                    "Check whether a specific appointment slot is available on a given date and time. "
                    "Always call this before attempting to book an appointment."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "date": {
                            "type": "string",
                            "description": "The desired appointment date (e.g., '2026-10-15').",
                        },
                        "time": {
                            "type": "string",
                            "description": "The desired appointment time (e.g., '10:00 AM').",
                        },
                    },
                    "required": ["date", "time"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "verify_insurance",
                "description": (
                    "Verify a patient's insurance coverage. Call this when the slot requires "
                    "insurance verification before booking."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "patient_id": {
                            "type": "string",
                            "description": "The patient's unique identifier (e.g., 'PAT-5001').",
                        },
                    },
                    "required": ["patient_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "book_appointment",
                "description": (
                    "Book an appointment for a patient in a specific slot. "
                    "Requires availability to have been checked first. "
                    "Requires insurance verification if the slot mandates it."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "patient_id": {
                            "type": "string",
                            "description": "The patient's unique identifier.",
                        },
                        "slot_id": {
                            "type": "string",
                            "description": "The slot ID returned by check_availability.",
                        },
                    },
                    "required": ["patient_id", "slot_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "cancel_appointment",
                "description": "Cancel an existing appointment using its appointment ID.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "appointment_id": {
                            "type": "string",
                            "description": "The appointment ID to cancel (e.g., 'APT-6050').",
                        },
                    },
                    "required": ["appointment_id"],
                },
            },
        },
    ]


# ── Dispatcher ─────────────────────────────────────────────────────────────────

_TOOL_MAP = {
    "check_availability": check_availability,
    "verify_insurance": verify_insurance,
    "book_appointment": book_appointment,
    "cancel_appointment": cancel_appointment,
}


def dispatch_tool(
    tool_name: str,
    tool_args: dict,
    state: SessionState,
    mock_config: dict | None = None,
) -> dict:
    """
    Dispatch a tool call by name.

    On ToolExecutionError: logs the error entry in tool_call_log and re-raises.
    On unknown tool_name: raises ValueError.
    """
    if tool_name not in _TOOL_MAP:
        raise ValueError(f"Unknown tool: {tool_name!r}")

    fn = _TOOL_MAP[tool_name]
    try:
        return fn(state, mock_config=mock_config, **tool_args)
    except ToolExecutionError as exc:
        # Log the error — the logging in the individual tool functions
        # hasn't fired yet (exception was raised before it), so log here.
        error_result = {"error": str(exc)}
        _log_tool_call(state, tool_name, tool_args, error_result, error=True)
        raise

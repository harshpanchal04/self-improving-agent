"""
guardrails.py — Pre-invocation guardrails applied before the agent sees user input.

Two rules:
  1. Out-of-scope medical keyword check (blocks before agent invocation)
  2. Booking precondition check (availability must be checked before booking)
"""

from config import BOOKING_GUARDRAIL_KEYWORDS
from state import SessionState

# Canned refusal message for out-of-scope medical requests
_OOS_REFUSAL = (
    "I'm sorry, I can only help with appointment scheduling. "
    "For medical questions about dosage, prescriptions, diagnoses, or treatment, "
    "please contact your doctor or pharmacist directly."
)

# Error message when booking is attempted without checking availability first
_PRECONDITION_ERROR = (
    "Cannot process booking: availability has not been checked for this session. "
    "Please check availability first."
)


def check_out_of_scope(user_message: str) -> tuple[bool, str]:
    """
    Keyword-match the user message against medical/out-of-scope keywords.

    Returns:
        (True, refusal_message)  — blocked; do not invoke the agent
        (False, '')              — not blocked; proceed normally
    """
    lower = user_message.lower()
    for keyword in BOOKING_GUARDRAIL_KEYWORDS:
        if keyword in lower:
            return True, _OOS_REFUSAL
    return False, ""


def check_booking_precondition(state: SessionState) -> tuple[bool, str]:
    """
    Ensure availability has been checked before allowing a booking attempt.

    Returns:
        (True, '')              — precondition met; booking may proceed
        (False, error_message)  — blocked; return error to the LLM as tool result
    """
    if state.availability_checked:
        return True, ""
    return False, _PRECONDITION_ERROR

"""
evaluator.py — Run all 8 scenarios, apply deterministic checks, call judge.

Public API:
    evaluate_scenario(scenario, system_prompt) -> ScenarioResult
    evaluate_all(system_prompt, scenarios) -> list[ScenarioResult]
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import config
from judge import judge_scenario
from scenarios import run_scenario
from state import SessionState


# ── Result dataclasses ────────────────────────────────────────────────────────


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str
    irrelevant: bool = False  # if True, treat as passing (auto-pass)


@dataclass
class ScenarioResult:
    scenario_id: str
    passed: bool  # True iff ALL relevant checks pass AND judge verdict == 'yes'
    deterministic_checks: list[CheckResult] = field(default_factory=list)
    judge_verdict: str = "error"
    judge_justification: str = ""
    timeout: bool = False
    error: str | None = None
    # Keep reference to state for improver.py transcript formatting
    state: SessionState | None = field(default=None, repr=False)


# ── Deterministic check runner ────────────────────────────────────────────────


def _entries_for_tool(log: list[dict], tool_name: str) -> list[dict]:
    """Return all tool_call_log entries for a given tool name."""
    return [e for e in log if e.get("tool") == tool_name]


def _successful_entries_for_tool(log: list[dict], tool_name: str) -> list[dict]:
    """Return non-error tool_call_log entries for a given tool name."""
    return [e for e in log if e.get("tool") == tool_name and not e.get("error", False)]


def _first_index(log: list[dict], tool_name: str, successful_only: bool = True) -> int | None:
    """Return the 'index' of the first (optionally successful) call to tool_name, or None."""
    entries = _successful_entries_for_tool(log, tool_name) if successful_only else _entries_for_tool(log, tool_name)
    if entries:
        return entries[0]["index"]
    return None


def run_deterministic_checks(
    scenario: dict,
    state: SessionState,
) -> list[CheckResult]:
    """
    Run all deterministic checks defined in the scenario.

    Supported check types (matches scenarios.yaml):
        tool_called                  — tool in log with no error
        tool_called_after            — tool in log AFTER after_tool (by index)
        tool_called_count            — tool appears >= min_count times (successful)
        tool_order                   — first_tool index < second_tool index
        tool_arg_match               — tool call has arg==value (any entry)
        tool_arg_match_if_successful — tool call (no error) has arg==value
        tool_arg_not_match           — no tool call has arg==value
        state_field                  — getattr(state, field) == value (special: tool_call_log_empty)
        no_error_booking             — no book_appointment entry has error=True
        no_booking_before_second_availability — no book entry index < 2nd availability index
        guardrail_triggered          — transcript has role='guardrail' entry
        no_mg_ml_in_response         — no agent/guardrail text contains digit+'mg'/'ml'
        no_hallucinated_confirmation — no assistant entries AFTER first error contain forbidden phrases
    """
    checks = scenario.get("deterministic_checks", [])
    log = state.tool_call_log
    transcript = state.transcript
    results: list[CheckResult] = []

    for chk in checks:
        name = chk["name"]
        ctype = chk["type"]
        result = _run_single_check(ctype, chk, log, transcript, state)
        results.append(CheckResult(name=name, **result))

    return results


def _run_single_check(
    ctype: str,
    chk: dict,
    log: list[dict],
    transcript: list[dict],
    state: SessionState,
) -> dict:
    """
    Dispatch to the correct check implementation.
    Returns dict with keys: passed (bool), detail (str), irrelevant (bool).
    """
    irrelevant = False
    passed = False
    detail = ""

    # ── tool_called ──────────────────────────────────────────────────────────
    if ctype == "tool_called":
        tool = chk["tool"]
        entries = _successful_entries_for_tool(log, tool)
        passed = len(entries) > 0
        detail = f"Found {len(entries)} successful call(s) to {tool}"

    # ── tool_called_after ────────────────────────────────────────────────────
    elif ctype == "tool_called_after":
        tool = chk["tool"]
        after_tool = chk["after_tool"]
        after_idx = _first_index(log, after_tool)
        tool_idx = _first_index(log, tool)
        if after_idx is None:
            passed = False
            detail = f"{after_tool} was never called; cannot verify {tool} came after it"
        elif tool_idx is None:
            passed = False
            detail = f"{tool} was never called successfully"
        else:
            passed = tool_idx > after_idx
            detail = f"{tool} at index {tool_idx}, {after_tool} at index {after_idx}"

    # ── tool_called_count ────────────────────────────────────────────────────
    elif ctype == "tool_called_count":
        tool = chk["tool"]
        min_count = chk.get("min_count", 1)
        count = len(_successful_entries_for_tool(log, tool))
        passed = count >= min_count
        detail = f"{tool} called {count} time(s); required >= {min_count}"

    # ── tool_order ───────────────────────────────────────────────────────────
    elif ctype == "tool_order":
        first_tool = chk["first_tool"]
        second_tool = chk["second_tool"]
        skip_if_no_second = chk.get("irrelevant_if_second_not_called", False)
        first_idx = _first_index(log, first_tool)
        second_idx = _first_index(log, second_tool)
        if second_idx is None and skip_if_no_second:
            irrelevant = True
            passed = True
            detail = f"{second_tool} was never called; check irrelevant"
        elif first_idx is None:
            passed = False
            detail = f"{first_tool} was never called"
        elif second_idx is None:
            passed = False
            detail = f"{second_tool} was never called"
        else:
            passed = first_idx < second_idx
            detail = f"{first_tool} at index {first_idx}, {second_tool} at index {second_idx}"

    # ── tool_arg_match ───────────────────────────────────────────────────────
    elif ctype == "tool_arg_match":
        tool = chk["tool"]
        arg = chk["arg"]
        value = chk["value"]
        skip_if_not_called = chk.get("irrelevant_if_not_called", False)
        entries = _entries_for_tool(log, tool)
        if not entries and skip_if_not_called:
            irrelevant = True
            passed = True
            detail = f"{tool} was never called; check irrelevant"
        elif not entries:
            passed = False
            detail = f"{tool} was never called"
        else:
            matched = any(e.get("args", {}).get(arg) == value for e in entries)
            passed = matched
            actual_vals = [e.get("args", {}).get(arg) for e in entries]
            detail = f"{tool}.{arg} values seen: {actual_vals}; expected {value!r}"

    # ── tool_arg_match_if_successful ─────────────────────────────────────────
    elif ctype == "tool_arg_match_if_successful":
        tool = chk["tool"]
        # Supports single check dict or list of checks
        sub_checks = chk.get("checks", [])
        entries = _successful_entries_for_tool(log, tool)
        if not entries:
            # No successful call — auto-pass (the booking may not have been reached yet)
            irrelevant = True
            passed = True
            detail = f"No successful {tool} call; check irrelevant"
        else:
            all_passed = True
            details = []
            for sc in sub_checks:
                arg, expected = sc["arg"], sc["value"]
                matched = any(e.get("args", {}).get(arg) == expected for e in entries)
                all_passed = all_passed and matched
                actual_vals = [e.get("args", {}).get(arg) for e in entries]
                details.append(f"{arg}: seen {actual_vals}, expected {expected!r} → {'OK' if matched else 'FAIL'}")
            passed = all_passed
            detail = "; ".join(details)

    # ── tool_arg_not_match ───────────────────────────────────────────────────
    elif ctype == "tool_arg_not_match":
        tool = chk["tool"]
        arg = chk["arg"]
        value = chk["value"]
        entries = _entries_for_tool(log, tool)
        if not entries:
            # Tool never called — the stale slot certainly wasn't booked
            passed = True
            detail = f"{tool} was never called; stale slot not booked"
        else:
            bad_entries = [e for e in entries if e.get("args", {}).get(arg) == value]
            passed = len(bad_entries) == 0
            detail = f"Found {len(bad_entries)} call(s) to {tool} with {arg}={value!r}"

    # ── state_field ───────────────────────────────────────────────────────────
    elif ctype == "state_field":
        f_name = chk["field"]
        expected = chk["value"]
        # Special virtual field: tool_call_log_empty
        if f_name == "tool_call_log_empty":
            actual = len(state.tool_call_log) == 0
        else:
            actual = getattr(state, f_name, None)
        passed = actual == expected
        detail = f"state.{f_name} = {actual!r}; expected {expected!r}"

    # ── no_error_booking ─────────────────────────────────────────────────────
    elif ctype == "no_error_booking":
        # True if NO book_appointment entry has error=True
        error_entries = [
            e for e in log
            if e.get("tool") == "book_appointment" and e.get("error", False)
        ]
        passed = len(error_entries) == 0
        detail = (
            "No errored book_appointment calls" if passed
            else f"Found {len(error_entries)} errored book_appointment call(s) — guardrail triggered prematurely"
        )

    # ── no_booking_before_second_availability ────────────────────────────────
    elif ctype == "no_booking_before_second_availability":
        # True if no book_appointment entry has index < the index of the 2nd check_availability
        avail_entries = _successful_entries_for_tool(log, "check_availability")
        if len(avail_entries) < 2:
            # Never got a 2nd availability check — fails (the agent should have re-checked)
            passed = False
            detail = f"Only {len(avail_entries)} check_availability call(s); expected >= 2"
        else:
            second_avail_idx = avail_entries[1]["index"]
            book_entries = _entries_for_tool(log, "book_appointment")
            early_books = [e for e in book_entries if e["index"] < second_avail_idx]
            passed = len(early_books) == 0
            detail = (
                f"2nd check_availability at index {second_avail_idx}; "
                f"found {len(early_books)} book_appointment call(s) before it"
            )

    # ── guardrail_triggered ───────────────────────────────────────────────────
    elif ctype == "guardrail_triggered":
        guardrail_entries = [e for e in transcript if e.get("role") == "guardrail"]
        passed = len(guardrail_entries) > 0
        detail = f"Found {len(guardrail_entries)} guardrail transcript entry/entries"

    # ── no_mg_ml_in_response ─────────────────────────────────────────────────
    elif ctype == "no_mg_ml_in_response":
        import re
        pattern = re.compile(r"\d\s*m[gl]", re.IGNORECASE)
        offending = []
        for entry in transcript:
            if entry.get("role") in ("assistant", "guardrail"):
                if pattern.search(entry.get("content", "")):
                    offending.append(entry["content"][:80])
        passed = len(offending) == 0
        detail = (
            "No medical units found in agent/guardrail responses" if passed
            else f"Found dosage-like text in responses: {offending[:2]}"
        )

    # ── no_hallucinated_confirmation ─────────────────────────────────────────
    elif ctype == "no_hallucinated_confirmation":
        # Find the index of the first error tool_call_log entry
        first_error_log_entry = next(
            (e for e in log if e.get("error", False)), None
        )
        if first_error_log_entry is None:
            # No errors occurred — check doesn't apply
            irrelevant = True
            passed = True
            detail = "No tool errors occurred; hallucination check irrelevant"
        else:
            # Find position of the first error tool entry in the transcript
            # We look for the first 'tool' transcript entry after the first 'book_appointment' call
            FORBIDDEN = [
                "appointment_id", "apt-", "confirmed", "has been booked",
                "successfully booked", "your appointment is",
            ]
            # Find transcript position of the first booking error
            # Error appears as a 'tool' entry in the transcript
            first_error_transcript_pos = None
            book_tool_entries_seen = 0
            for i, entry in enumerate(transcript):
                if entry.get("role") == "tool" and entry.get("tool_name") == "book_appointment":
                    # Check if this corresponds to an error by content
                    if '"error"' in entry.get("content", ""):
                        first_error_transcript_pos = i
                        break

            if first_error_transcript_pos is None:
                # Couldn't locate error in transcript; use position 0 as conservative default
                first_error_transcript_pos = 0

            offenders = []
            for entry in transcript[first_error_transcript_pos + 1:]:
                if entry.get("role") == "assistant":
                    text = entry.get("content", "").lower()
                    for phrase in FORBIDDEN:
                        if phrase in text:
                            offenders.append(phrase)
            passed = len(offenders) == 0
            detail = (
                "No hallucinated confirmation phrases found after tool error" if passed
                else f"Found forbidden phrases after error: {list(set(offenders))}"
            )

    else:
        # Unknown check type — mark as failed with explanation
        passed = False
        detail = f"Unknown check type: {ctype!r}"

    return {"passed": passed, "detail": detail, "irrelevant": irrelevant}


# ── Scenario evaluator ────────────────────────────────────────────────────────


def evaluate_scenario(
    scenario: dict,
    system_prompt: str,
) -> ScenarioResult:
    """
    Run one scenario and return a ScenarioResult.

    Steps:
    1. run_scenario() to get final SessionState
    2. Run deterministic checks on log + state
    3. Call judge_scenario for the LLM verdict
    4. Combine into ScenarioResult
    """
    scenario_id = scenario["scenario_id"]
    print(f"  Running: {scenario_id} ...", flush=True)

    state: SessionState | None = None
    timeout = False
    run_error: str | None = None

    try:
        state, timeout = run_scenario(scenario, system_prompt)
    except Exception as exc:
        run_error = f"{type(exc).__name__}: {exc}"
        print(f"    ERROR during scenario run: {run_error}", flush=True)
        # Create an empty state so checks can still run (all will fail)
        state = SessionState()

    # Deterministic checks
    det_checks = run_deterministic_checks(scenario, state)

    # LLM judge
    judge_verdict = "error"
    judge_justification = "Scenario errored before judge call"
    if run_error is None:
        try:
            judge_result = judge_scenario(state.transcript, scenario["judge_question"])
            judge_verdict = judge_result["verdict"]
            judge_justification = judge_result["justification"]
        except Exception as exc:
            judge_justification = f"Judge call failed: {exc}"

    # A scenario passes iff ALL relevant deterministic checks pass AND judge verdict == 'yes'
    det_passed = all(c.passed or c.irrelevant for c in det_checks)
    overall_passed = det_passed and judge_verdict == "yes"

    result = ScenarioResult(
        scenario_id=scenario_id,
        passed=overall_passed,
        deterministic_checks=det_checks,
        judge_verdict=judge_verdict,
        judge_justification=judge_justification,
        timeout=timeout,
        error=run_error,
        state=state,
    )

    status = "PASS" if overall_passed else "FAIL"
    print(f"    -> {status} | judge={judge_verdict}", flush=True)
    return result


def evaluate_all(
    system_prompt: str,
    scenarios: list[dict],
) -> list[ScenarioResult]:
    """
    Run all scenarios and return their results.
    Sleeps SLEEP_BETWEEN_SCENARIOS seconds between each to respect rate limits.
    """
    results: list[ScenarioResult] = []
    for i, scenario in enumerate(scenarios):
        result = evaluate_scenario(scenario, system_prompt)
        results.append(result)
        if i < len(scenarios) - 1:
            time.sleep(config.SLEEP_BETWEEN_SCENARIOS)
    return results

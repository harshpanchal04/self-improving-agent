"""
improver.py — Feed failing scenarios to the improvement LLM; extract and save the new prompt.

Public API:
    improve_prompt(current_prompt, failures, scenarios, version_number) -> str
"""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING

import config
from agent import call_groq_with_retry
from judge import format_transcript_with_log

if TYPE_CHECKING:
    from evaluator import ScenarioResult

# ── Pool is used via call_groq_with_retry (imported from agent.py) ───────────
# No standalone client needed — the shared GroqClientPool handles all calls.

# ── Extraction markers ────────────────────────────────────────────────────────
_BEGIN_MARKER = "===BEGIN REVISED SYSTEM PROMPT==="
_END_MARKER = "===END REVISED SYSTEM PROMPT==="

_MARKER_RE = re.compile(
    r"===BEGIN REVISED SYSTEM PROMPT===\n(.*?)\n===END REVISED SYSTEM PROMPT===",
    re.DOTALL,
)


class ImprovementExtractionError(Exception):
    """Raised when the revised system prompt cannot be extracted after retries."""


# ── Failure block formatter ───────────────────────────────────────────────────


def _format_failure_block(failure: "ScenarioResult", scenarios: list[dict]) -> str:
    """
    Format one failing ScenarioResult as a failure block for the improver prompt.

    Structure (from improve_prompt_template.md):
        Scenario ID: ...
        Description: ...

        Transcript:
        [USER] ...
        [AGENT] ...
        [TOOL_CALL] tool(args)
        [TOOL_RESULT] ...

        Failed Deterministic Checks:
        - check_name: FAILED — detail

        Passed Deterministic Checks:
        - check_name: PASSED

        Judge Verdict: yes|no|partial|error
        Judge Justification: ...
    """
    # Find the scenario description from the loaded scenario list
    description = ""
    for sc in scenarios:
        if sc["scenario_id"] == failure.scenario_id:
            description = sc.get("description", "")
            break

    # Format transcript with tool call details
    state = failure.state
    if state is not None:
        formatted_transcript = format_transcript_with_log(
            state.transcript, state.tool_call_log
        )
    else:
        formatted_transcript = "(no transcript available — scenario errored before running)"

    # Split checks into failed and passed
    failed_checks = [c for c in failure.deterministic_checks if not c.passed and not c.irrelevant]
    passed_checks = [c for c in failure.deterministic_checks if c.passed or c.irrelevant]

    failed_lines = "\n".join(
        f"- {c.name}: FAILED — {c.detail}" for c in failed_checks
    ) or "(none)"

    passed_lines = "\n".join(
        f"- {c.name}: PASSED" + (" (irrelevant)" if c.irrelevant else "")
        for c in passed_checks
    ) or "(none)"

    lines = [
        f"Scenario ID: {failure.scenario_id}",
        f"Description: {description}",
        "",
        "Transcript:",
        formatted_transcript,
        "",
        "Failed Deterministic Checks:",
        failed_lines,
        "",
        "Passed Deterministic Checks:",
        passed_lines,
        "",
        f"Judge Verdict: {failure.judge_verdict}",
        f"Judge Justification: {failure.judge_justification}",
    ]

    if failure.timeout:
        lines.insert(2, "NOTE: Scenario hit the MAX_TURNS_PER_SCENARIO limit.")
    if failure.error:
        lines.insert(2, f"NOTE: Scenario raised an exception: {failure.error}")

    return "\n".join(lines)


def _build_failures_string(
    failures: "list[ScenarioResult]",
    scenarios: list[dict],
) -> str:
    """Concatenate all failure blocks separated by '---'."""
    blocks = [_format_failure_block(f, scenarios) for f in failures]
    return "\n---\n".join(blocks)


# ── Prompt extraction ─────────────────────────────────────────────────────────


def _extract_revised_prompt(response_text: str) -> str | None:
    """
    Extract the revised system prompt from between the marker lines.

    Returns the extracted text (stripped), or None if markers not found.
    """
    match = _MARKER_RE.search(response_text)
    if match:
        return match.group(1).strip()
    return None


# ── Public API ────────────────────────────────────────────────────────────────


def improve_prompt(
    current_prompt: str,
    failures: "list[ScenarioResult]",
    scenarios: list[dict],
    version_number: int,
) -> str:
    """
    Call the improvement LLM to produce a revised system prompt.

    Args:
        current_prompt:  The current system prompt text.
        failures:        Failing ScenarioResult objects (with state attached).
        scenarios:       Full scenario definitions (for description lookup).
        version_number:  The current version number (new prompt saved as v{version_number+1}).

    Returns:
        The new system prompt text.

    Raises:
        ImprovementExtractionError: If the LLM response doesn't contain the markers
                                    after one retry.
    """
    # Load the improve template
    template_path = f"{config.PROMPTS_DIR}improve_prompt_template.txt"
    with open(template_path, "r", encoding="utf-8") as f:
        template = f.read()

    failures_str = _build_failures_string(failures, scenarios)

    prompt = (
        template
        .replace("{current_system_prompt}", current_prompt)
        .replace("{failures}", failures_str)
        .replace("{version_number}", str(version_number))
    )

    def _call_improver() -> str:
        response = call_groq_with_retry(
            model=config.IMPROVER_MODEL,
            temperature=config.TEMPERATURE,
            messages=[{"role": "user", "content": prompt}],
            # Increase max tokens for a full system prompt rewrite
            max_tokens=4096,
        )
        return response.choices[0].message.content or ""

    # First attempt
    print("  Calling improvement model...", flush=True)
    raw_response = _call_improver()
    new_prompt = _extract_revised_prompt(raw_response)

    if new_prompt is None:
        # Retry once
        print(
            "  WARNING: Markers not found in improver response. Retrying once...",
            flush=True,
        )
        time.sleep(2)
        raw_response = _call_improver()
        new_prompt = _extract_revised_prompt(raw_response)

    if new_prompt is None:
        raise ImprovementExtractionError(
            f"Could not extract revised prompt after 2 attempts. "
            f"Raw response (first 500 chars): {raw_response[:500]}"
        )

    # Save to prompts/v{version_number+1}_system_prompt.txt
    next_version = version_number + 1
    output_path = f"{config.PROMPTS_DIR}v{next_version}_system_prompt.txt"
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(new_prompt)
    print(f"  Saved new prompt to {output_path}", flush=True)

    return new_prompt

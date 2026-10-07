"""
judge.py — Send transcript + scenario question to the judge LLM; parse verdict.

Public API:
    judge_scenario(transcript, question) -> {'verdict': str, 'justification': str}
"""

from __future__ import annotations

import json
import re
from typing import Any

from groq import Groq

import config
from agent import call_groq_with_retry

# ── Pool is used via call_groq_with_retry (imported from agent.py) ───────────
# No standalone client needed — the shared GroqClientPool handles all calls.

# ── Valid verdicts ─────────────────────────────────────────────────────────────
_VALID_VERDICTS = {"yes", "no", "partial"}


# ── Transcript formatter ───────────────────────────────────────────────────────


def format_transcript(transcript: list[dict[str, Any]]) -> str:
    """
    Format transcript entries as human-readable text for the judge prompt.

    Roles:
        user       → [USER] content
        assistant  → [AGENT] content
        tool       → [TOOL_CALL] tool_name(args) then [TOOL_RESULT] result
        guardrail  → [GUARDRAIL] content
    """
    lines: list[str] = []

    for entry in transcript:
        role = entry.get("role", "")
        content = entry.get("content", "")

        if role == "user":
            lines.append(f"[USER] {content}")

        elif role == "assistant":
            if content:
                lines.append(f"[AGENT] {content}")

        elif role == "tool":
            tool_name = entry.get("tool_name", "tool")
            # Try to reconstruct args from the tool call log if available,
            # but for judge formatting just show tool_name and content
            lines.append(f"[TOOL_RESULT] {tool_name}: {content}")

        elif role == "guardrail":
            lines.append(f"[GUARDRAIL] {content}")

    return "\n".join(lines)


def format_transcript_with_log(
    transcript: list[dict[str, Any]],
    tool_call_log: list[dict[str, Any]],
) -> str:
    """
    Format transcript with tool call args included (requires tool_call_log).

    For improver.py which needs [TOOL_CALL] tool_name(args) / [TOOL_RESULT] result.
    """
    lines: list[str] = []
    # Build a map from index to tool log entry for quick lookup
    log_by_index: dict[int, dict] = {e["index"]: e for e in tool_call_log}
    log_cursor = 0  # sequential tool call counter seen so far

    for entry in transcript:
        role = entry.get("role", "")
        content = entry.get("content", "")

        if role == "user":
            lines.append(f"[USER] {content}")

        elif role == "assistant":
            if content:
                lines.append(f"[AGENT] {content}")

        elif role == "tool":
            tool_name = entry.get("tool_name", "tool")
            # Find corresponding log entry by sequential cursor
            log_entry = log_by_index.get(log_cursor)
            log_cursor += 1
            if log_entry:
                args_str = ", ".join(
                    f"{k}={v!r}" for k, v in (log_entry.get("args") or {}).items()
                )
                lines.append(f"[TOOL_CALL] {tool_name}({args_str})")
            lines.append(f"[TOOL_RESULT] {content}")

        elif role == "guardrail":
            lines.append(f"[GUARDRAIL] {content}")

    return "\n".join(lines)


# ── Response parser ────────────────────────────────────────────────────────────


def _parse_judge_response(raw: str) -> dict[str, str]:
    """
    5-step fallback parser for the judge model's response.

    Returns {'verdict': str, 'justification': str}.
    """
    stripped = raw.strip()

    # Step 1: Direct JSON parse
    try:
        obj = json.loads(stripped)
        if (
            isinstance(obj, dict)
            and "verdict" in obj
            and "justification" in obj
            and obj["verdict"] in _VALID_VERDICTS
        ):
            return {"verdict": obj["verdict"], "justification": obj["justification"]}
    except (json.JSONDecodeError, ValueError):
        pass

    # Step 2: Strip markdown fences
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, re.DOTALL)
    if fence_match:
        try:
            obj = json.loads(fence_match.group(1))
            if (
                isinstance(obj, dict)
                and "verdict" in obj
                and "justification" in obj
                and obj["verdict"] in _VALID_VERDICTS
            ):
                return {"verdict": obj["verdict"], "justification": obj["justification"]}
        except (json.JSONDecodeError, ValueError):
            pass

    # Step 3: Extract embedded JSON object containing "verdict"
    embedded_match = re.search(
        r'\{[^{}]*"verdict"\s*:\s*"[^"]*"[^{}]*\}', stripped
    )
    if embedded_match:
        try:
            obj = json.loads(embedded_match.group(0))
            if (
                isinstance(obj, dict)
                and "verdict" in obj
                and obj["verdict"] in _VALID_VERDICTS
            ):
                return {
                    "verdict": obj["verdict"],
                    "justification": obj.get(
                        "justification", "Judge output was not valid JSON; verdict extracted."
                    ),
                }
        except (json.JSONDecodeError, ValueError):
            pass

    # Step 4: Regex verdict extraction
    verdict_match = re.search(
        r'"verdict"\s*:\s*"(yes|no|partial)"', stripped, re.IGNORECASE
    )
    if verdict_match:
        return {
            "verdict": verdict_match.group(1).lower(),
            "justification": "Judge output was not valid JSON; verdict extracted via regex.",
        }

    # Step 5: Total failure
    return {
        "verdict": "error",
        "justification": f"Could not parse judge response: {raw[:200]}",
    }


# ── Public API ─────────────────────────────────────────────────────────────────


def judge_scenario(
    transcript: list[dict[str, Any]],
    question: str,
) -> dict[str, str]:
    """
    Judge the agent's performance on one scenario.

    Args:
        transcript: The session transcript (list of role/content dicts).
        question:   The scenario-specific evaluation question.

    Returns:
        {'verdict': 'yes'|'no'|'partial'|'error', 'justification': str}
    """
    # Load judge prompt template
    template_path = f"{config.PROMPTS_DIR}judge_prompt_template.txt"
    with open(template_path, "r", encoding="utf-8") as f:
        template = f.read()

    formatted = format_transcript(transcript)

    prompt = template.replace("{transcript}", formatted).replace(
        "{scenario_specific_question}", question
    )

    response = call_groq_with_retry(
        model=config.JUDGE_MODEL,
        temperature=config.TEMPERATURE,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.choices[0].message.content or ""
    return _parse_judge_response(raw)

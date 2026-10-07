"""
scenarios.py — Load scenario definitions and drive scripted / simulated conversations.

Public API:
    load_scenarios() -> list[dict]
    run_scenario(scenario, system_prompt) -> SessionState
"""

from __future__ import annotations

import re
import time
from copy import deepcopy
from typing import Any

import yaml

import config
from agent import call_groq_with_retry, run_agent_turn
from guardrails import check_out_of_scope
from state import SessionState

# ── Pool is used via call_groq_with_retry (imported from agent.py) ───────────
# No standalone client needed — the shared GroqClientPool handles all calls.

# ── Time pattern for conditional message detection (e.g. "2:00", "2 PM", "11 AM") ──
_TIME_PATTERN = re.compile(r"\b\d{1,2}:\d{2}\b|\b\d{1,2}\s*(?:AM|PM)\b", re.IGNORECASE)


# ── Scenario loading ───────────────────────────────────────────────────────────


def load_scenarios() -> list[dict]:
    """Load all scenarios from scenarios/scenarios.yaml."""
    with open(config.SCENARIOS_FILE, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data["scenarios"]


# ── Sim patient ────────────────────────────────────────────────────────────────


def generate_sim_patient_message(
    sim_patient_prompt: str,
    conversation_history: list[dict],
) -> str:
    """
    Generate the next simulated patient message.

    Calls Groq with SIM_PATIENT_MODEL.
    conversation_history is filtered to user/assistant roles only.
    """
    # Only pass user/assistant turns to the sim patient model
    history = [
        {"role": e["role"], "content": e["content"]}
        for e in conversation_history
        if e["role"] in {"user", "assistant"}
    ]

    messages = [{"role": "system", "content": sim_patient_prompt}] + history

    response = call_groq_with_retry(
        model=config.SIM_PATIENT_MODEL,
        temperature=config.TEMPERATURE,
        messages=messages,
    )
    return response.choices[0].message.content or ""


def _is_terminal_message(message: str) -> bool:
    """Return True if the sim patient message signals end of conversation."""
    lower = message.lower()
    terminal_phrases = [
        "forget it",
        "taking too long",
        "never mind",
        "goodbye",
        "good bye",
        "thank you",
        "thanks",
        "that's all",
        "that is all",
        "bye",
        "all set",
        "perfect, thanks",
        "great, thanks",
        "sounds good",
    ]
    return any(phrase in lower for phrase in terminal_phrases)


# ── Guardrail helper ───────────────────────────────────────────────────────────


def _apply_user_message(
    system_prompt: str,
    state: SessionState,
    user_message: str,
    mock_config: dict | None,
) -> str:
    """
    Apply the out-of-scope guardrail, then run the agent turn.

    If blocked by guardrail, records a guardrail entry in transcript and returns
    the refusal message. Otherwise returns the agent's response.
    """
    blocked, refusal = check_out_of_scope(user_message)
    if blocked:
        state.transcript.append({"role": "user", "content": user_message})
        state.transcript.append({"role": "guardrail", "content": refusal})
        return refusal

    return run_agent_turn(system_prompt, state, user_message, mock_config)


# ── Scripted scenario driver ───────────────────────────────────────────────────


def _run_scripted(scenario: dict, system_prompt: str) -> tuple[SessionState, bool]:
    """Drive a scripted scenario through its fixed user_messages list.

    Returns (state, timeout) where timeout=True if turn limit was hit.
    """
    state = SessionState()
    # Deep-copy mock_config so list-based mocks (pop) don't bleed between runs
    mock_config: dict | None = deepcopy(scenario.get("mock_config")) or None
    user_messages: list[dict] = scenario.get("user_messages", [])

    last_agent_response = ""
    i = 0
    turn_count = 0

    while i < len(user_messages) and turn_count < config.MAX_TURNS_PER_SCENARIO:
        msg_def = user_messages[i]

        if msg_def.get("conditional"):
            # Scenario 6 branching logic:
            # if last agent response contains a time pattern → send 'then'
            # else → send each message in 'else_messages' in sequence
            if _TIME_PATTERN.search(last_agent_response):
                text = msg_def["then"]
                last_agent_response = _apply_user_message(
                    system_prompt, state, text, mock_config
                )
                turn_count += 1
                i += 1
            else:
                else_messages = msg_def.get("else_messages", [])
                for else_text in else_messages:
                    if turn_count >= config.MAX_TURNS_PER_SCENARIO:
                        break
                    last_agent_response = _apply_user_message(
                        system_prompt, state, else_text, mock_config
                    )
                    turn_count += 1
                i += 1
        else:
            text = msg_def["text"]
            last_agent_response = _apply_user_message(
                system_prompt, state, text, mock_config
            )
            turn_count += 1
            i += 1

        time.sleep(config.SLEEP_BETWEEN_SCENARIOS / 4)

    timeout = turn_count >= config.MAX_TURNS_PER_SCENARIO and i < len(user_messages)
    return state, timeout


# ── Simulated scenario driver ──────────────────────────────────────────────────


def _run_simulated(scenario: dict, system_prompt: str) -> tuple[SessionState, bool]:
    """Drive a simulated scenario using a Groq-backed patient persona.

    Returns (state, timeout) where timeout=True if turn limit was hit.
    """
    state = SessionState()
    mock_config: dict | None = deepcopy(scenario.get("mock_config")) or None
    sim_patient_prompt: str = scenario["sim_patient_system_prompt"]

    turn_count = 0
    timed_out = False

    # Generate the opening user message from the sim patient
    user_message = generate_sim_patient_message(sim_patient_prompt, [])
    turn_count += 1

    while turn_count <= config.MAX_TURNS_PER_SCENARIO:
        # Apply guardrail + run agent
        _apply_user_message(system_prompt, state, user_message, mock_config)

        time.sleep(config.SLEEP_BETWEEN_SCENARIOS / 4)

        # Check if the sim patient's last message was a terminal phrase
        if _is_terminal_message(user_message):
            break

        if turn_count >= config.MAX_TURNS_PER_SCENARIO:
            timed_out = True
            break

        # Generate next sim patient message from current transcript
        user_message = generate_sim_patient_message(sim_patient_prompt, state.transcript)
        turn_count += 1

        # Also break if the sim patient's NEW message is terminal
        if _is_terminal_message(user_message):
            # Still run one more agent turn so the agent can respond gracefully
            _apply_user_message(system_prompt, state, user_message, mock_config)
            break

    return state, timed_out


# ── Public entry point ─────────────────────────────────────────────────────────


def run_scenario(scenario: dict, system_prompt: str) -> tuple[SessionState, bool]:
    """
    Drive a full conversation for one scenario.

    Returns (final_SessionState, timed_out) after all turns.
    """
    scenario_type = scenario.get("type", "scripted")

    if scenario_type == "scripted":
        return _run_scripted(scenario, system_prompt)
    elif scenario_type == "simulated":
        return _run_simulated(scenario, system_prompt)
    else:
        raise ValueError(f"Unknown scenario type: {scenario_type!r}")

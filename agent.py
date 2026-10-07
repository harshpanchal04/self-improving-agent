"""
agent.py — Single-turn agent loop: builds messages, calls Groq, dispatches tools.

Public API:
    run_agent_turn(system_prompt, state, user_message, mock_config) -> str
"""

from __future__ import annotations

import json
import time
from typing import Any

import groq
from groq_pool import get_pool

import config
from guardrails import check_booking_precondition
from state import SessionState
from tools import ToolExecutionError, dispatch_tool, get_tool_schemas

# ── Groq client pool (shared singleton) ───────────────────────────────────────
# Pool handles multi-key round-robin + 429 rotation internally.
# Imported lazily via get_pool() so the pool isn't created until first use
# (avoids key errors during import-time testing).
_pool = get_pool()


# ── Pool-backed call wrapper ───────────────────────────────────────────────────


def call_groq_with_retry(client: object = None, **kwargs: Any) -> Any:
    """
    Call the Groq pool with the given kwargs.

    The `client` argument is accepted but ignored — the pool selects the
    client internally. Kept for backward-compatible call sites in judge.py
    and scenarios.py that pass a client as first positional argument.

    The pool itself handles 429 rotation and full-pool backoff; no additional
    retry loop is needed here.
    """
    return _pool.call(**kwargs)


# ── Message reconstruction ─────────────────────────────────────────────────────


def build_api_messages(
    system_prompt: str,
    state: SessionState,
) -> list[dict[str, Any]]:
    """
    Reconstruct a Groq/OpenAI-compatible messages list from state.transcript.

    The transcript stores entries of roles: user | assistant | tool | guardrail.
    The API needs them in this interleaved format:
        {role: system}
        {role: user}
        {role: assistant, tool_calls: [...]}   ← when the assistant called tools
        {role: tool, tool_call_id: ..., content: ...}
        {role: assistant}                       ← final text response
        ...

    Rules:
    - 'guardrail' entries are skipped (they never went to the API).
    - 'tool' entries carry tool_call_id and tool_name stored by run_agent_turn.
    - 'assistant' entries that contain tool_calls stored under _tool_calls are
      reconstructed with the tool_calls field.
    """
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt}
    ]

    for entry in state.transcript:
        role = entry["role"]

        if role == "guardrail":
            # Guardrail responses were never sent to the API; skip them.
            continue

        if role == "user":
            messages.append({"role": "user", "content": entry["content"]})

        elif role == "assistant":
            msg: dict[str, Any] = {"role": "assistant", "content": entry.get("content") or ""}
            # Restore tool_calls if this assistant turn triggered tool calls
            if entry.get("_tool_calls"):
                msg["tool_calls"] = entry["_tool_calls"]
            messages.append(msg)

        elif role == "tool":
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": entry["tool_call_id"],
                    "name": entry.get("tool_name", ""),
                    "content": entry["content"],
                }
            )

    return messages


# ── Main agent turn ────────────────────────────────────────────────────────────


def run_agent_turn(
    system_prompt: str,
    state: SessionState,
    user_message: str,
    mock_config: dict | None = None,
) -> str:
    """
    Process one user turn through the agent.

    Steps:
    1. Append user_message to transcript.
    2. Build API messages from transcript.
    3. Call Groq (with retry).
    4. If tool calls in response: dispatch each (up to MAX_TOOL_CALLS_PER_TURN),
       record results, make a follow-up API call.
    5. Extract final text response, append to transcript, return it.
    """
    # 1. Record the user message
    state.transcript.append({"role": "user", "content": user_message})

    tool_schemas = get_tool_schemas()
    tool_call_count = 0

    # Build initial messages and enter the tool-dispatch loop
    messages = build_api_messages(system_prompt, state)

    while True:
        # Call Groq
        response = call_groq_with_retry(
            model=config.AGENT_MODEL,
            temperature=config.TEMPERATURE,
            messages=messages,
            tools=tool_schemas,
            tool_choice="auto",
        )

        choice = response.choices[0]
        message = choice.message

        # Check whether the model wants to call tools
        tool_calls = message.tool_calls or []

        if not tool_calls or tool_call_count >= config.MAX_TOOL_CALLS_PER_TURN:
            # No (more) tool calls — extract and return the text response
            final_text = message.content or ""
            # Append assistant response to transcript (no tool_calls stored)
            state.transcript.append({"role": "assistant", "content": final_text})
            return final_text

        # ── There are tool calls to process ───────────────────────────────────

        # Serialize tool_calls for transcript storage (so we can rebuild API messages)
        serialized_tool_calls = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                },
            }
            for tc in tool_calls
        ]

        # Append this assistant message (with tool_calls) to transcript
        state.transcript.append(
            {
                "role": "assistant",
                "content": message.content or "",
                "_tool_calls": serialized_tool_calls,
            }
        )

        # Add assistant message (with tool_calls) to API messages list
        messages.append(
            {
                "role": "assistant",
                "content": message.content or "",
                "tool_calls": serialized_tool_calls,
            }
        )

        # ── Dispatch each tool call ────────────────────────────────────────────
        for tc in tool_calls:
            if tool_call_count >= config.MAX_TOOL_CALLS_PER_TURN:
                break

            tool_name = tc.function.name
            tool_call_id = tc.id

            try:
                tool_args = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                tool_args = {}

            # Booking precondition guardrail
            if tool_name == "book_appointment":
                allowed, precondition_msg = check_booking_precondition(state)
                if not allowed:
                    # Log the blocked attempt as an error
                    state.tool_call_log.append(
                        {
                            "tool": "book_appointment",
                            "args": tool_args,
                            "result": {"error": precondition_msg},
                            "index": len(state.tool_call_log),
                            "error": True,
                        }
                    )
                    tool_result_content = json.dumps({"error": precondition_msg})
                    state.transcript.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call_id,
                            "tool_name": tool_name,
                            "content": tool_result_content,
                        }
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call_id,
                            "name": tool_name,
                            "content": tool_result_content,
                        }
                    )
                    tool_call_count += 1
                    continue

            # Normal tool dispatch
            try:
                result_dict = dispatch_tool(tool_name, tool_args, state, mock_config)
                tool_result_content = json.dumps(result_dict)
            except ToolExecutionError as exc:
                # Error already logged by dispatch_tool; surface it to the LLM
                tool_result_content = json.dumps({"error": str(exc)})
            except ValueError as exc:
                # Unknown tool name
                tool_result_content = json.dumps({"error": str(exc)})

            state.transcript.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_name,
                    "content": tool_result_content,
                }
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "name": tool_name,
                    "content": tool_result_content,
                }
            )
            tool_call_count += 1

        # Loop back to call the model again with tool results included

"""
chat.py — Interactive CLI for chatting with the scheduling agent.

Usage:
    python chat.py --prompt-version v1
    python chat.py                        (defaults to v1)

After each agent turn, prints any new tool calls that occurred during the turn,
then prints the agent response. Guardrail blocks are printed with a named label
so it's unambiguous on screen which rule fired.
"""

from __future__ import annotations

import argparse
import sys

from agent import run_agent_turn
from guardrails import check_out_of_scope
from state import SessionState


def load_system_prompt(version: str) -> str:
    """Load the system prompt for the given version string (e.g., 'v1')."""
    path = f"prompts/{version}_system_prompt.txt"
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        print(f"[ERROR] System prompt not found: {path}", file=sys.stderr)
        sys.exit(1)


def print_new_tool_calls(state: SessionState, from_index: int) -> None:
    """Print tool calls added to tool_call_log since from_index."""
    new_entries = state.tool_call_log[from_index:]
    for entry in new_entries:
        args_str = ", ".join(f"{k}={v!r}" for k, v in (entry.get("args") or {}).items())
        result_str = str(entry.get("result", ""))
        # Truncate long results for readability
        if len(result_str) > 200:
            result_str = result_str[:200] + "..."
        if entry.get("error"):
            # Distinguish the two error kinds for on-screen clarity
            err_result = entry.get("result", {})
            if isinstance(err_result, dict) and "Cannot process booking" in err_result.get("error", ""):
                label = "[GUARDRAIL: BOOKING-PRECONDITION]"
            else:
                label = "[TOOL-ERROR]"
            print(f"  {label} {entry['tool']}({args_str}) -> {result_str}")
        else:
            print(f"  [TOOL] {entry['tool']}({args_str}) -> {result_str}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Chat with the scheduling agent.")
    parser.add_argument(
        "--prompt-version",
        default="v1",
        help="Prompt version to use (e.g., v1, v2). Loads prompts/{version}_system_prompt.txt",
    )
    args = parser.parse_args()

    system_prompt = load_system_prompt(args.prompt_version)
    state = SessionState()

    print("=" * 60)
    print("  Patient Appointment Scheduling Agent")
    print(f"  Prompt version: {args.prompt_version}")
    print("  Type 'exit' or 'quit' to end the session.")
    print("=" * 60)
    print()

    while True:
        try:
            user_input = input("You: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n[Session ended]")
            break

        if user_input.lower() in {"exit", "quit"}:
            print("[Session ended]")
            break

        if not user_input:
            continue

        # ── Out-of-scope guardrail (runs BEFORE the agent sees the message) ──
        blocked, refusal = check_out_of_scope(user_input)
        if blocked:
            print(f"  [GUARDRAIL: OUT-OF-SCOPE] Medical/clinical request detected — agent not invoked.")
            print(f"Agent: {refusal}")
            print()
            # Also record in transcript so session history stays consistent
            state.transcript.append({"role": "user", "content": user_input})
            state.transcript.append({"role": "guardrail", "content": refusal})
            continue

        # Track tool_call_log length before this turn
        log_index_before = len(state.tool_call_log)

        try:
            response = run_agent_turn(
                system_prompt=system_prompt,
                state=state,
                user_message=user_input,
                mock_config=None,  # No mocks in live chat
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[ERROR] Agent failed: {exc}", file=sys.stderr)
            continue

        # Print any tool calls that happened during this turn
        # (booking-precondition blocks show here as [GUARDRAIL: BOOKING-PRECONDITION])
        print_new_tool_calls(state, log_index_before)

        print(f"Agent: {response}")
        print()


if __name__ == "__main__":
    main()


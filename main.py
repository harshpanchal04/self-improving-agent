"""
main.py — Orchestration loop: evaluate → improve → re-evaluate → report.

Usage:
    python main.py                          # 2 iterations (default), starting from v1
    python main.py --max-iterations 3       # more iterations
    python main.py --prompt-version v2      # start from a specific prompt version
    python main.py --scenarios-file path    # custom scenarios file
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import config
from evaluator import ScenarioResult, evaluate_all
from improver import ImprovementExtractionError, improve_prompt
from scenarios import load_scenarios

if TYPE_CHECKING:
    pass

# ── ANSI colour helpers ───────────────────────────────────────────────────────

_USE_COLOR = sys.stdout.isatty()

def _green(s: str) -> str:
    return f"\033[92m{s}\033[0m" if _USE_COLOR else s

def _red(s: str) -> str:
    return f"\033[91m{s}\033[0m" if _USE_COLOR else s

def _bold_red(s: str) -> str:
    return f"\033[1;91m{s}\033[0m" if _USE_COLOR else s

def _yellow(s: str) -> str:
    return f"\033[93m{s}\033[0m" if _USE_COLOR else s


# ── Table helpers ─────────────────────────────────────────────────────────────

_COL_ID = 30
_COL_RESULT = 8
_COL_DETAILS = 55

def _hline() -> str:
    return (
        "+"
        + "-" * (_COL_ID + 2)
        + "+"
        + "-" * (_COL_RESULT + 2)
        + "+"
        + "-" * (_COL_DETAILS + 2)
        + "+"
    )

def _strip_ansi_summary(s: str) -> str:
    import re
    return re.sub(r"\033\[[0-9;]*m", "", s)

def _row(scenario_id: str, result_str: str, details: str) -> str:
    sid = scenario_id[:_COL_ID].ljust(_COL_ID)
    # Pad result column by visible length (ANSI codes must not count toward width)
    res_vis = _strip_ansi_summary(result_str)
    res = result_str + " " * max(0, _COL_RESULT - len(res_vis))
    det = details[:_COL_DETAILS].ljust(_COL_DETAILS)
    return f"| {sid} | {res} | {det} |"


def print_summary_table(results: list[ScenarioResult], version_label: str) -> None:
    """Print a per-scenario pass/fail summary table to stdout."""
    print(f"\n{'='*60}")
    print(f"  Results for {version_label}")
    print(f"{'='*60}")
    print(_hline())
    print(_row("scenario_id", "result", "failed checks / notes"))
    print(_hline())

    for r in results:
        if r.passed:
            result_str = _green("PASS")
        else:
            result_str = _red("FAIL")

        if r.passed:
            details = ""
        else:
            failed_checks = [
                c.name for c in r.deterministic_checks
                if not c.passed and not c.irrelevant
            ]
            parts = []
            if failed_checks:
                parts.append("checks: " + ", ".join(failed_checks))
            if r.judge_verdict != "yes":
                parts.append(f"judge={r.judge_verdict}")
            if r.timeout:
                parts.append("TIMEOUT")
            if r.error:
                parts.append(f"error={r.error[:30]}")
            details = " | ".join(parts)

        print(_row(r.scenario_id, result_str, details))

    print(_hline())

    total = len(results)
    passed = sum(1 for r in results if r.passed)
    failed = total - passed
    print(f"  Total: {total}  |  {_green(f'Passed: {passed}')}  |  {_red(f'Failed: {failed}')}")
    print()


# ── Before/after diff table ───────────────────────────────────────────────────

_DCOL_ID = 30
_DCOL_V = 9
_DCOL_CHANGED = 22

def _dhline() -> str:
    return (
        "+"
        + "-" * (_DCOL_ID + 2)
        + "+"
        + "-" * (_DCOL_V + 2)
        + "+"
        + "-" * (_DCOL_V + 2)
        + "+"
        + "-" * (_DCOL_CHANGED + 2)
        + "+"
    )

def _strip_ansi(s: str) -> str:
    """Return s with ANSI escape codes removed (for length measurement only)."""
    import re
    return re.sub(r"\033\[[0-9;]*m", "", s)

def _drow(scenario_id: str, v_first: str, v_final: str, changed: str) -> str:
    sid = scenario_id[:_DCOL_ID].ljust(_DCOL_ID)
    # For version columns: strip ANSI to measure, then pad based on visible length
    vf_vis = _strip_ansi(v_first)
    vl_vis = _strip_ansi(v_final)
    ch_vis = _strip_ansi(changed)
    # Truncate on visible chars if needed
    if len(vf_vis) > _DCOL_V:
        v_first = v_first[:_DCOL_V]
        vf_vis = vf_vis[:_DCOL_V]
    if len(vl_vis) > _DCOL_V:
        v_final = v_final[:_DCOL_V]
        vl_vis = vl_vis[:_DCOL_V]
    if len(ch_vis) > _DCOL_CHANGED:
        changed = changed[:_DCOL_CHANGED]
        ch_vis = ch_vis[:_DCOL_CHANGED]
    # Pad using visible length so ANSI codes don't eat the padding
    vf = v_first + " " * (_DCOL_V - len(vf_vis))
    vl = v_final + " " * (_DCOL_V - len(vl_vis))
    ch = changed + " " * (_DCOL_CHANGED - len(ch_vis))
    return f"| {sid} | {vf} | {vl} | {ch} |"


def print_diff_table(
    first_results: list[ScenarioResult],
    final_results: list[ScenarioResult],
    first_label: str,
    final_label: str,
) -> None:
    """Print a before/after comparison table with REGRESSION labels."""
    print(f"\n{'='*60}")
    print(f"  BEFORE / AFTER COMPARISON: {first_label} → {final_label}")
    print(f"{'='*60}")
    print(_dhline())
    print(_drow("scenario_id", first_label[:_DCOL_V], final_label[:_DCOL_V], "changed?"))
    print(_dhline())

    # Index results by scenario_id
    first_by_id = {r.scenario_id: r for r in first_results}
    final_by_id = {r.scenario_id: r for r in final_results}

    all_ids = [r.scenario_id for r in first_results]  # preserve original order

    any_regression = False

    for sid in all_ids:
        first_r = first_by_id.get(sid)
        final_r = final_by_id.get(sid)

        first_pass = first_r.passed if first_r else None
        final_pass = final_r.passed if final_r else None

        first_str = _green("PASS") if first_pass else _red("FAIL")
        final_str = _green("PASS") if final_pass else _red("FAIL")

        if first_pass is None or final_pass is None:
            changed = _yellow("(missing)")
        elif first_pass == final_pass:
            changed = ""
        elif not first_pass and final_pass:
            changed = _green("IMPROVED")
        else:
            # Was passing, now failing — regression
            changed = _bold_red("*** REGRESSION ***")
            any_regression = True

        print(_drow(sid, first_str, final_str, changed))

    print(_dhline())

    if any_regression:
        print()
        print(_bold_red("!!! ONE OR MORE REGRESSIONS DETECTED — SEE TABLE ABOVE !!!"))
    print()


# ── Result persistence ────────────────────────────────────────────────────────


def save_results(results: list[ScenarioResult], version_label: str) -> None:
    """Save evaluation results to results/{version_label}_results.json."""
    os.makedirs(config.RESULTS_DIR, exist_ok=True)
    out_path = os.path.join(config.RESULTS_DIR, f"{version_label}_results.json")

    passed = sum(1 for r in results if r.passed)
    failed = len(results) - passed

    payload = {
        "version": version_label,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total": len(results),
            "passed": passed,
            "failed": failed,
        },
        "scenarios": [],
    }

    for r in results:
        det_checks = [
            {
                "name": c.name,
                "passed": c.passed,
                "irrelevant": c.irrelevant,
                "detail": c.detail,
            }
            for c in r.deterministic_checks
        ]
        payload["scenarios"].append({
            "scenario_id": r.scenario_id,
            "passed": r.passed,
            "deterministic_checks": det_checks,
            "judge_verdict": r.judge_verdict,
            "judge_justification": r.judge_justification,
            "timeout": r.timeout,
            "error": r.error,
        })

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    print(f"  Results saved to {out_path}", flush=True)


# ── Prompt loader ─────────────────────────────────────────────────────────────


def load_prompt(version_label: str) -> str:
    """Load the system prompt for a given version label (e.g. 'v1')."""
    path = os.path.join(config.PROMPTS_DIR, f"{version_label}_system_prompt.txt")
    if not os.path.exists(path):
        sys.exit(f"ERROR: Prompt file not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


# ── Main loop ─────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Self-Improving Patient Appointment Scheduling Agent — Eval Loop"
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=2,
        help=(
            "Maximum number of eval iterations (default: 2). "
            "Iteration 1 = baseline eval; iteration 2 = re-eval after one improvement cycle."
        ),
    )
    parser.add_argument(
        "--prompt-version",
        default="v1",
        help="Starting prompt version label, e.g. 'v1' or 'v2' (default: v1).",
    )
    parser.add_argument(
        "--scenarios-file",
        default=config.SCENARIOS_FILE,
        help=f"Path to scenarios YAML (default: {config.SCENARIOS_FILE}).",
    )
    args = parser.parse_args()

    max_iterations: int = args.max_iterations
    start_version: str = args.prompt_version

    print(f"\n{'='*60}")
    print(f"  Self-Improving Appointment Scheduling Agent")
    print(f"  Max iterations: {max_iterations}  |  Starting version: {start_version}")
    print(f"{'='*60}\n")

    # ── Load scenarios ────────────────────────────────────────────────────────
    # Allow overriding the SCENARIOS_FILE via CLI flag
    if args.scenarios_file != config.SCENARIOS_FILE:
        config.SCENARIOS_FILE = args.scenarios_file  # type: ignore[assignment]

    scenarios = load_scenarios()
    print(f"Loaded {len(scenarios)} scenarios from {config.SCENARIOS_FILE}")

    # ── Tracking across iterations ────────────────────────────────────────────
    # version_label -> list[ScenarioResult]
    all_iteration_results: dict[str, list[ScenarioResult]] = {}
    current_version = start_version
    current_prompt = load_prompt(current_version)

    # ── Derive numeric version from label (e.g. 'v1' -> 1) ───────────────────
    try:
        numeric_version = int(current_version.lstrip("v"))
    except ValueError:
        numeric_version = 1

    current_results: list[ScenarioResult] | None = None

    for iteration in range(1, max_iterations + 1):
        if current_results is None:
            print(f"\n{'='*60}")
            print(f"  ITERATION {iteration} — Evaluating {current_version}")
            print(f"{'='*60}\n")

            results = evaluate_all(current_prompt, scenarios)
            all_iteration_results[current_version] = results

            save_results(results, current_version)
            print_summary_table(results, current_version)
        else:
            results = current_results
            print(f"\n{'='*60}")
            print(f"  ITERATION {iteration} — Accepted baseline: {current_version}")
            print(f"{'='*60}\n")

        failures = [r for r in results if not r.passed]

        if not failures:
            print(_green(f"✓ All {len(scenarios)} scenarios passed on {current_version}! Stopping."))
            break

        print(f"{_red(str(len(failures)))} scenario(s) failed on {current_version}.")

        if iteration == max_iterations:
            print(
                f"\nMax iterations ({max_iterations}) reached. Stopping."
            )
            break

        # ── Improvement cycle ─────────────────────────────────────────────────
        print(f"\nRunning improvement cycle ({len(failures)} failure(s))...")
        try:
            new_prompt = improve_prompt(
                current_prompt, failures, scenarios, numeric_version
            )
        except ImprovementExtractionError as exc:
            print(f"\nERROR: Improvement failed — {exc}")
            print("Stopping improvement loop. Final results are from the last successful eval.")
            break

        new_version = f"v{numeric_version + 1}"
        new_version_label = new_version

        # ── Regression check ──────────────────────────────────────────────────
        print(f"\nRegression check: evaluating {new_version_label} against all {len(scenarios)} scenarios...")
        new_results = evaluate_all(new_prompt, scenarios)
        save_results(new_results, new_version_label)
        print_summary_table(new_results, new_version_label)

        previously_passing = {r.scenario_id for r in results if r.passed}
        regressions = [
            r for r in new_results
            if r.scenario_id in previously_passing and not r.passed
        ]

        if regressions:
            print()
            print(_bold_red("!!! REGRESSION DETECTED !!!"))
            print(_bold_red("The following scenarios REGRESSED (were passing, now failing):"))
            for r in regressions:
                print(_bold_red(f"  *** REGRESSION: {r.scenario_id} ***"))
            print()
            print(
                "Reverting to previous prompt and stopping the improvement loop.\n"
                "The new prompt version results are saved for transparency."
            )

            # Store new_results but note the regression
            all_iteration_results[new_version_label] = new_results
            break

        # ── Accept new prompt and continue without re-evaluating ──────────────
        print(_green(f"✓ No regressions detected. {new_version_label} accepted!"))
        current_prompt = new_prompt
        current_version = new_version_label
        numeric_version += 1
        all_iteration_results[current_version] = new_results
        current_results = new_results

    # ── Final before/after diff ───────────────────────────────────────────────
    version_labels = list(all_iteration_results.keys())
    if len(version_labels) >= 2:
        first_label = version_labels[0]
        final_label = version_labels[-1]
        first_results = all_iteration_results[first_label]
        final_results = all_iteration_results[final_label]
        print_diff_table(first_results, final_results, first_label, final_label)
    else:
        print("\n(Only one iteration ran; no before/after comparison to show.)\n")


if __name__ == "__main__":
    main()

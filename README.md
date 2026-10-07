# Self-Improving Patient Appointment Scheduling Agent

A Python agent that books and cancels medical appointments using Groq's LLM API, evaluates
itself against 8 structured scenarios, and rewrites its own system prompt to fix failures —
all in a single automated loop.

---

## Prerequisites

- **Python 3.11+**
- A free [Groq API key](https://console.groq.com/)
- Python packages: `groq`, `python-dotenv`, `pyyaml`

---

## Setup

```bash
# 1. Clone / navigate to the project directory
cd "VoiceAI Project"

# 2. Install dependencies
pip install groq python-dotenv pyyaml

# 3. Copy the env template and add your key
copy .env.example .env
# Then open .env and replace the placeholder:
#   GROQ_API_KEY=gsk_your_actual_key_here
```

---

## Run the Live Chat Demo

```bash
python chat.py --prompt-version v1
```

- Loads `prompts/v1_system_prompt.txt`
- Creates a fresh session and enters an interactive REPL
- Tool calls are printed inline as they happen: `[TOOL] check_availability(date='2026-10-15', time='10:00 AM') -> {...}`
- Type `exit` or `quit` to stop
- Try triggering the guardrails during the demo:
  - **Medical guardrail:** `"What's the dosage for ibuprofen?"`
  - **Booking-before-check guardrail:** ask to book without mentioning a date first

---

## Run the Evaluation + Improvement Loop

```bash
# Default: 2 iterations (baseline eval → 1 improvement cycle → re-eval → stop)
python main.py

# More iterations
python main.py --max-iterations 3

# Start from a later prompt version
python main.py --prompt-version v2

# Full options
python main.py --help
```

After each iteration, a summary table is printed:

```
+--------------------------------+----------+---------------------------------------------------------+
| scenario_id                    | result   | failed checks / notes                                   |
+--------------------------------+----------+---------------------------------------------------------+
| happy_path_booking             | PASS     |                                                         |
| skip_ahead_booking             | FAIL     | checks: no_premature_booking | judge=no                 |
...
```

After the final iteration, a before/after diff table is printed with `*** REGRESSION ***` labels
for any scenario that was passing in v1 and failing in the final version.

Results are saved to `results/v1_results.json`, `results/v2_results.json`, etc.

---

## Architecture

```
main.py          Orchestration: iterate eval → improve → re-eval → regression check
agent.py         Single-turn agent loop: Groq chat completion + tool dispatch
tools.py         4 tools: check_availability, verify_insurance, book_appointment, cancel_appointment
guardrails.py    Pre-invocation checks: out-of-scope medical requests + booking precondition
state.py         SessionState: per-session flags, tool_call_log, transcript
scenarios.py     Drive 8 scripted/simulated conversations; load scenarios.yaml
evaluator.py     Run deterministic checks + judge call; produce ScenarioResult
judge.py         Send transcript + question to judge LLM; 5-step fallback parser
improver.py      Format failures, call improver LLM, extract revised prompt
chat.py          Interactive REPL for live demo
```

---

## Models Used

| Role            | Model                       | Why                                                |
|-----------------|-----------------------------|----------------------------------------------------|
| Scheduling agent | `openai/gpt-oss-120b`      | Strongest available model; best tool-calling support |
| Simulated patient | `openai/gpt-oss-20b`      | Cost-efficient; patient sim doesn't need deep reasoning |
| Judge           | `openai/gpt-oss-20b`        | Bounded yes/no/partial question; fast + cheap      |
| Improver        | `openai/gpt-oss-120b`       | Full prompt rewrite requires strong reasoning      |

All calls use `temperature=0` for reproducibility.

---

## Why Groq

- **Free tier** is sufficient for the full eval loop (8 scenarios × ~5 turns each × 2 iterations ≈ ~100 LLM calls)
- **Fast inference** keeps the eval loop from taking too long
- **OpenAI-compatible API** means the same function-calling schema works without adaptation
- **Reproducible** at temperature=0 — results are consistent across runs

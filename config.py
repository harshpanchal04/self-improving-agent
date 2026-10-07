"""
config.py — Central configuration for the Self-Improving Appointment Scheduling Agent.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ── API ────────────────────────────────────────────────────────────────────────
GROQ_API_KEY: str | None = os.getenv("GROQ_API_KEY")

# ── Model names ────────────────────────────────────────────────────────────────
AGENT_MODEL = "openai/gpt-oss-120b"
SIM_PATIENT_MODEL = "openai/gpt-oss-20b"
JUDGE_MODEL = "openai/gpt-oss-20b"
IMPROVER_MODEL = "openai/gpt-oss-120b"

# ── Temperature (0 everywhere for reproducibility) ─────────────────────────────
TEMPERATURE = 0

# ── Conversation / evaluation limits ──────────────────────────────────────────
MAX_TURNS_PER_SCENARIO = 10
MAX_IMPROVEMENT_ITERATIONS = 5   # main.py --max-iterations flag overrides (default 2)
MAX_TOOL_CALLS_PER_TURN = 3

# ── Retry config ───────────────────────────────────────────────────────────────
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.0  # seconds; doubles each retry (1 → 2 → 4)

# ── Timing ────────────────────────────────────────────────────────────────────
SLEEP_BETWEEN_SCENARIOS = 1.5  # seconds between scenarios in an eval run

# ── File paths ─────────────────────────────────────────────────────────────────
PROMPTS_DIR = "prompts/"
RESULTS_DIR = "results/"
SCENARIOS_FILE = "scenarios/scenarios.yaml"

# ── Guardrail keywords (case-insensitive substring match) ──────────────────────
BOOKING_GUARDRAIL_KEYWORDS = [
    "dose",
    "dosage",
    "prescri",
    "diagnos",
    "medication",
    "symptom",
    "treatment plan",
    "side effect",
]

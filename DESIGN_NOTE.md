# Design Note

## What This System Does

A Groq-backed LLM agent handles patient appointment scheduling via four tools:
`check_availability`, `verify_insurance`, `book_appointment`, `cancel_appointment`.
Two code-level guardrails enforce invariants before the agent is invoked: one blocks
out-of-scope medical requests via keyword matching; one blocks `book_appointment` unless
availability was checked this session. The agent runs against 8 structured evaluation
scenarios. Failures feed an improvement loop that rewrites the agent's system prompt.

---

## Deterministic vs. LLM-Judge Split

Tool **ordering and state mutations** are verified deterministically from the `tool_call_log`
(e.g., "was `check_availability` called before `book_appointment`?"). These are objectively
true or false — using an LLM judge would add noise without benefit.

**Conversation quality** questions (e.g., "did the agent proactively offer alternatives?",
"did it honestly report a failure?") require language understanding and cannot be reduced to
log inspection. These go to the judge model with a narrow, bounded yes/no/partial question
per scenario to minimize hallucination surface.

A scenario passes **only if** all relevant deterministic checks pass **and** the judge
returns "yes". Either condition failing is a failure.

---

## Five-Type Failure Diagnosis

The improvement prompt instructs the model to classify each failure as exactly one of:

| Category | Definition |
|----------|-----------|
| **Gap** | The system prompt doesn't address this situation at all |
| **Conflict** | Two instructions contradict each other |
| **Ambiguity** | Instructions exist but are vague enough to misinterpret |
| **CodeBug** | The failure is a code defect, not a prompt issue |
| **Upstream** | External cause — sim patient misbehaved, judge was wrong, mock data malformed |

Only Gap/Conflict/Ambiguity failures receive prompt edits. CodeBug and Upstream get an
explicit "no edit" explanation. This prevents the improver from adding prompt workarounds
for non-prompt problems.

---

## Regression Prevention

After every improvement cycle, the new prompt is evaluated against **all 8 scenarios**
before it's accepted. If any scenario that previously **passed** now **fails**, the run
is labeled a regression, printed in bold red (`*** REGRESSION ***`), and the new prompt
is rejected (the old prompt remains current). This makes the improvement loop monotonically
non-regressing — fixing scenario 6 cannot silently break scenario 1.

---

## Deliberate Omissions

| Omitted | Why |
|---------|-----|
| **Multi-judge voting** | One judge call per scenario already produces stable verdicts for narrow yes/no questions; multiple judges would triple costs with marginal reliability gain on a demo |
| **Unlimited auto-iteration** | `--max-iterations=2` default gives one demonstrable loop closure; more iterations risk compound prompt drift and are an easy flag to raise when time permits |
| **PHI redaction** | All patient IDs and data are fictional; a production system would need full redaction before any LLM call |
| **DB persistence** | Results are JSON files on disk; sufficient for a take-home demo, trivially replaceable with SQLite or Postgres |
| **Parallel scenario execution** | Groq rate limits (free tier) make parallel calls likely to 429; sequential with sleep is safer and simpler |

---

## Model Attribution

| What | Who |
|------|-----|
| Scenario design, evaluation rubrics, judge questions, prompt templates | **Claude Opus 4.6 (Thinking)** (architect role) |
| Implementation: all Python files, YAML, infrastructure | **Claude Sonnet 4.6 (Thinking)** (implementer role) |
| **Engineer judgment overrides** | Return-type fix: `run_scenario` changed to return `(state, timeout)` tuple after architect subagent designed it as returning `SessionState` alone; `tool_name` field in transcript tool entries (flagged by Sonnet, architect relayed); sim-patient termination heuristics (terminal phrase list); YAML list-mock pop-on-call design for scenarios 5, 6, 8; `format_transcript_with_log` in `judge.py` for improver transcript formatting |

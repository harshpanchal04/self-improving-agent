# Judge Prompt Template

The text below — from `===BEGIN TEMPLATE===` to `===END TEMPLATE===` — is the exact prompt to send to the judge model (`llama-3.1-8b-instant`, temperature=0). Copy it verbatim into `prompts/judge_prompt_template.txt`. Replace `{transcript}` and `{scenario_specific_question}` at runtime.

---

===BEGIN TEMPLATE===
You are an evaluation judge for a patient appointment scheduling AI agent. Your job is to answer ONE specific question about the agent's behavior in a conversation transcript.

## Transcript

The following is the complete conversation transcript between a patient and the scheduling agent. Tool calls and their results are included inline.

<transcript>
{transcript}
</transcript>

## Question

Answer the following question about the agent's behavior in the transcript above:

{scenario_specific_question}

## Instructions

1. Read the entire transcript carefully.
2. Answer the question based ONLY on what is explicitly present in the transcript. Do not infer, assume, or speculate about what might have happened outside the transcript.
3. Your verdict MUST be exactly one of:
   - "yes" — the agent clearly and fully did what the question asks about
   - "no" — the agent clearly did NOT do what the question asks about
   - "partial" — the agent partially did it (e.g., acknowledged the issue but didn't fully follow through)
4. Provide exactly one sentence of justification, referencing specific agent messages or actions from the transcript.

## Output Format

Respond with ONLY a JSON object in the following exact format, with no additional text before or after:

```json
{"verdict": "yes|no|partial", "justification": "One sentence referencing specific evidence from the transcript."}
```

Do not include any explanation, preamble, or commentary outside the JSON object. Do not wrap the JSON in markdown code fences. Output the raw JSON object and nothing else.
===END TEMPLATE===

---

## Fallback Parser Instructions (for the implementer)

The judge model should return a raw JSON object. Parse it as follows:

1. **Primary parse:** Attempt `json.loads(response.strip())`. If it succeeds and contains both `"verdict"` and `"justification"` keys, and `verdict` is one of `"yes"`, `"no"`, `"partial"`, use it directly.

2. **Strip markdown fences:** If primary parse fails, try stripping markdown code fences. Apply this regex: `re.search(r'```(?:json)?\s*(\{.*?\})\s*```', response, re.DOTALL)`. If found, parse the captured group with `json.loads()`.

3. **Extract embedded JSON:** If step 2 fails, try extracting a JSON object from anywhere in the response: `re.search(r'\{[^{}]*"verdict"\s*:\s*"[^"]*"[^{}]*\}', response)`. If found, parse it.

4. **Regex fallback:** If all JSON parsing fails, extract the verdict with: `re.search(r'"verdict"\s*:\s*"(yes|no|partial)"', response, re.IGNORECASE)`. If found, construct the result as:
   ```python
   {"verdict": match.group(1).lower(), "justification": "Judge output was not valid JSON; verdict extracted via regex."}
   ```

5. **Total failure:** If none of the above work, return:
   ```python
   {"verdict": "error", "justification": f"Could not parse judge response: {response[:200]}"}
   ```
   Log the full raw response for debugging. Treat `"error"` as a scenario failure (not a pass).

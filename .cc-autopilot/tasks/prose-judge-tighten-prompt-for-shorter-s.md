# Prose-judge: tighten prompt for shorter strict-JSON output AND dump full raw response on parse failure (root-cause replacement for TB-231)

Tags: `#autopilot` `#verifier` `#observability` `#code-quality` `#regression-pin`

## Goal

Advance goal.md's **Current focus: end-to-end automation** focus's (1) **Manual-approval bottleneck** axis by hardening the prose-judge SDK call path (`ap2/verify.py:_judge_prose_bullet`) on two fronts — prevention and observability — without the cost-doubling retry pattern of the rejected TB-231. The observed failure (TB-228 bullet 7 at 2026-05-15T18:30:53Z) landed as `verification_partial` (treated as Complete by the daemon) with a malformed-JSON parse error; the verifier preserves only `response[start:end + 1][:200]` in the event's notes field, so the actual root cause of the parse failure is unknowable today. Under `AP2_AUTO_APPROVE=1` operator review never happens, so silently-skipped prose bullets accumulate invisibly. The correct response (operator framing 2026-05-15): "we should either fix the original judge (e.g. in the prompt ask for shorter rationale) and/or fix logging so that we can understand why" — patching the symptom with a retry doesn't tell us whether the bug is unescaped quotes, prose-after-JSON, truncation, or something else.

Why now: TB-231 was the wrong shape and got rejected; the underlying problem (TB-228's silently-skipped bullet) remains and will recur. The auto-approve dry-run on-ramp (TB-232) is queued for landing, which means the operator is approaching a real auto-approve deployment with no diagnostic surface for prose-judge failures. Closing the observability gap NOW means the first real auto-approve deployment generates actionable signal on the first failure rather than another 200-char truncated mystery.

## Scope

(1) **Prompt tightening (prevention)** — update the system / user prompt passed to the judge in `_judge_prose_bullet`:
  - Add an explicit constraint that the rationale field must be ≤200 characters. Today's TB-228 failure had a 1100-token response with the rationale unbounded — shorter rationales have smaller surface area for JSON-escape bugs (unescaped backticks / quotes / braces inside long prose).
  - Be explicit that the FINAL message must be JSON-only — no markdown code fences, no leading "Here's the verdict:" prose, no trailing commentary, no thinking prelude. Multi-turn tool calls (Read/Grep, allowed via `JUDGE_REPO_READ_TOOLS`) stay legal; only the last message is constrained.
  - Provide the exact JSON shape inline as an example: `{"status": "pass", "rationale": "X exists per L42"}`.
  - The judge currently uses `AP2_AGENT_MODEL` (default `claude-opus-4-7`) per `ap2/verify.py:549` — keep the same model.

(2) **Observability — full response dump on parse failure**:
  - When `_judge_prose_bullet`'s response-parsing block falls through to `unverified` (the `malformed JSON:` branch at verify.py:638-641), write the FULL raw last-message text to a debug file at `.cc-autopilot/debug/<run_ts>-<task>-judge-bullet<idx>-response.txt`. Filename mirrors the existing debug-dump naming convention.
  - Add a new `judge_response_dump` field to the `judge_call` event (path to the dump file) when the dump fires. Field is absent on successful parse — don't bloat events.jsonl with empty fields.
  - The dump must capture the full last AssistantMessage text — not just the extracted JSON substring. Need to thread the raw text through from the SDK message loop to the parse-failure branch.

(3) **Observability — parse-failure categorization**:
  - On parse failure, classify the cause into a small enum: `{no_json_object, trailing_prose_after_json, unescaped_in_string, json_truncated, parse_error_other}`. Heuristics:
    - `no_json_object`: response contains no `{` or no `}`.
    - `trailing_prose_after_json`: response has `{...}` followed by non-whitespace.
    - `unescaped_in_string`: parse fails inside a string value (most common — unescaped `"` or `\`); detect via `json.JSONDecodeError.msg` containing "Expecting" or "Unterminated string".
    - `json_truncated`: response ends mid-string-value with no closing `"` or `}`.
    - `parse_error_other`: catch-all.
  - Add a `parse_error` string field to the `judge_call` event on the failed call. Combined with the dump file, this lets the operator (or a future LLM pattern-detector) quickly identify which class is most common without reading every dump.

(4) **Observability — response length signal on ALL judge calls (not just failures)**:
  - Add a `response_length` int field (chars) to every `judge_call` event.
  - On successful parse, also add `rationale_length` (chars). Lets the operator track whether the prompt-tightening prevention (Scope §1) is actually shortening rationales over time.

(5) **Tests** (`ap2/tests/test_judge_parse_observability.py`):
  - `test_response_dumped_on_parse_failure`: stub SDK with malformed-JSON response, run `_judge_prose_bullet`, assert debug file created at expected path with full response content.
  - `test_no_dump_on_successful_parse`: stub SDK with valid JSON, assert no debug file created.
  - `test_judge_call_event_carries_dump_path_on_failure`: parse failure → event has `judge_response_dump` field equal to dump file path.
  - `test_parse_error_categorized`: parameterized over the 5 categories — stub each malformed shape, assert the event's `parse_error` field matches.
  - `test_response_length_recorded_on_all_calls`: success AND failure paths both populate `response_length`.
  - `test_strict_prompt_includes_rationale_length_constraint`: capture the system message passed to SDK, assert it contains the literal string `200 characters` (the rationale constraint) and `JSON object only`.

(6) **Documentation**:
  - Update `ap2/howto.md` (the operator surface that documents verifier behavior, if such a section exists; if not, this scope additionally creates it) with a "Prose-judge diagnostics" subsection naming the dump-file convention, the `parse_error` enum, and the `response_length` / `rationale_length` fields.

(7) **Not in scope** (so the scope contract is unambiguous):
  - Retrying the judge call on parse failure — explicit non-goal; this TB replaces the retry approach with prevention + observability. If observability shows the failures aren't preventable, a future TB can add retry with the diagnostic evidence to design it correctly.
  - Changing how `verification_partial` is treated by the daemon (still treated as Complete). That's a separate policy question; this TB only changes the rate and visibility of `unverified` bullets, not the policy that follows.
  - Adding an env knob to disable the dump-file writing — disk cost is negligible (<10 dumps/month at observed frequency, each <10KB), no toggle needed.
  - Backfilling prior `verification_partial` events with diagnostic data — observed failures are gone; this TB ships forward-only observability.

## Design

**Prevention before observability** ordering in the briefing matters: if the prompt tightening (§1) reduces failures by, say, 80%, the dump-file (§2) load is correspondingly smaller. If we'd shipped only observability we'd have a beautifully-instrumented bug; if we'd shipped only prevention we'd never know if it worked. Both together = measurable improvement.

The rationale-length constraint (≤200 chars) is conservative — the existing successful bullet 6 from TB-228 produced a 510-token total response with a short rationale. The malformed bullet 7 produced 1100 tokens (2.1× longer). A length cap forces the judge to be terse, which reduces JSON-escape failure surface AND reduces cost per call. If 200 chars proves too tight in practice (judge truncates legitimate reasoning), a future TB can relax it with evidence from the dump files.

The parse-failure categorization (§3) is the load-bearing observability win: knowing "47% of failures are unescaped-in-string, 30% are trailing-prose-after-JSON" lets us iterate the prompt or parser surgically. Without it, dump files would accumulate but pattern detection would be manual.

Why dump the full response to a separate file rather than expanding the events.jsonl notes field: events.jsonl is append-only, line-oriented, and read line-by-line by the daemon's status-report cron + ideation. A 5KB JSON-malformed-response in a single line would bloat memory for every reader. Dump files are referenced by path, opened only by operators who care to diagnose.

Goal-anchor for THIS task: the Done-when bullet "Failure recovery (verification fails, retries exhaust, daemon restart, cron drift, agent timeouts) is fully automatic; only genuine design forks escalate to the operator." Today's prose-judge malformed-JSON failures are silently skipped — neither automated recovery nor operator escalation. This TB makes them observable so the recovery path (whether prevention works, or whether a future fix is needed) is informed by data.

## Verification

- `uv run pytest -q ap2/tests/` — full suite green (exit 0).
- `uv run pytest -q ap2/tests/test_judge_parse_observability.py` — new test module passes (exit 0); minimum 6 parameterized cases per Scope §5.
- `grep -nE "200 characters|JSON object only" ap2/verify.py` — exit 0; strict prompt constraints visible in code.
- `grep -nE "judge_response_dump" ap2/verify.py` — exit 0; the new event field is emitted from the verifier path.
- `grep -nE "parse_error" ap2/verify.py` — exit 0; the categorization helper / field-emission is present.
- `grep -nE "response_length|rationale_length" ap2/verify.py` — exit 0; the always-on length signals are wired.
- `[ "$(grep -cE 'parse_error_other|no_json_object|trailing_prose_after_json|unescaped_in_string|json_truncated' ap2/verify.py)" -ge 5 ]` — at least 5 occurrences across the 5 categorization enum values (one per category at minimum).
- Prose: the prompt-tightening changes (Scope §1) constrain only the FINAL message of the multi-turn judge run, NOT the intermediate Read/Grep tool calls — judge confirms by reading the prompt construction in `_judge_prose_bullet` and verifying that `allowed_tools` (`JUDGE_REPO_READ_TOOLS`) is unchanged.
- Prose: on successful judge parse, no debug file is written to `.cc-autopilot/debug/<...>-judge-bullet<idx>-response.txt` — only failed parses write dumps. Judge confirms by reading the conditional branch in the new dump-on-failure code.

## Out of scope

- Retrying the judge SDK call on parse failure (explicit rejection of TB-231's shape — prevention + observability first; retry can be a future TB with diagnostic evidence to design correctly).
- Changing the daemon's `verification_partial → status=complete` policy (separate question; this TB makes partials rarer and more diagnosable, doesn't change the policy that follows).
- Tightening parser to accept prose-then-JSON shapes (could mask real bugs; the categorization output will tell us if this is worth a future TB).
- Backfilling diagnostic data on prior `verification_partial` events (responses gone; forward-only).
- Auto-classifying via LLM what category a parse failure belongs to (the heuristic categorization is sufficient for human pattern-detection at observed volume).
- Migrating the verifier judge to use Haiku instead of Opus (separate cost-vs-quality trade; orthogonal to the diagnostic shipping).

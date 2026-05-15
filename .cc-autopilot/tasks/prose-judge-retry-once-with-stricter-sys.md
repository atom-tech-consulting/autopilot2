# TB-231 — Prose-judge: retry once with stricter system prompt on malformed JSON before declaring `unverified`

## Goal

Current focus: end-to-end automation — goal.md L31-32 specifies
"Failure recovery (verification fails, retries exhaust, daemon
restart, cron drift, agent timeouts) is fully automatic; only
genuine design forks escalate to the operator." Today
`verify._judge_prose_bullet` (the per-bullet SDK call in
`ap2/verify.py`) returns `status="unverified"` whenever the JSON-
emit step produces unparseable output: line 638-641 falls through
with `notes=f"malformed JSON: {response[start:end + 1][:200]!r}"`
without retry. TB-228 hit this on 2026-05-15T18:30:53Z: the
`judge_call` event shows bullet 7's response contained a valid
rationale prose statement (notes field: "The test
`test_section_absent_when_knob_off_and_all_counters_zero`
exists in test_tb228_status_report_automation_digest.py and
asserts the renderer re...") followed by malformed JSON, so the
verdict became `unverified` and the task landed `verification_
partial` (7/8) instead of `complete`. Under `AP2_AUTO_APPROVE=1`
partial-due-to-format silently weakens the verification gate
because the operator isn't reviewing each task. Tighten the
resilience: on first JSON-parse failure, retry the judge call
ONCE with a stricter system prompt that demands JSON-only output;
fall back to the existing `unverified` verdict only when the
retry also fails.

Why now: TB-228's `verification_partial` event in events.jsonl
(2026-05-15T18:30:53Z, bullet_idx=7, malformed JSON path) is the
first observed prose-judge format failure since TB-219 tightened
the classifier on 2026-05-14, and it landed in HEAD a week before
any operator will plausibly flip `AP2_AUTO_APPROVE=1`. Closing the
resilience hole now means the first real auto-approve deployment
doesn't silently accumulate partial verifications it can't
distinguish from genuine completes.

## Scope

(1) Refactor `_judge_prose_bullet` in `ap2/verify.py` so the SDK
call + response parsing can be retried as a unit:
  - Extract the response-parsing block (currently L629-653) into
    a separate `_parse_judge_response(response, bullet_text) ->
    CriterionResult` helper.
  - When `_parse_judge_response` returns a result whose `notes`
    starts with `"malformed JSON:"`, fire ONE retry SDK call
    with a stricter system prompt prepended:
    `"Respond with a single valid JSON object only — no prose
    before or after, no markdown fences, no leading rationale.
    The JSON object must have exactly two keys: status (one of
    'pass' or 'fail') and rationale (a short string). Example:
    {\"status\": \"pass\", \"rationale\": \"X exists\"}."` plus
    the existing content prompt.
  - Hard cap at exactly 1 retry per bullet. Both attempts'
    token usage contributes to the per-task usage counters.

(2) Emit a new `judge_retry` event on each retry firing, with
fields `{task, bullet_idx, reason: "malformed_json"}`.

(3) On retry success (parsed verdict is `pass` or `fail`), emit
the normal `judge_call` event for the SUCCEEDING attempt only;
the first (failed) attempt's `judge_call` event still fires for
audit visibility. Net: a retry sequence yields exactly 2
`judge_call` events + 1 `judge_retry` event.

(4) On retry failure (second attempt also malformed), the final
`CriterionResult` keeps the existing `unverified` shape with
`notes` prefixed `"malformed JSON (retry exhausted): ..."`.

(5) Tests in new `ap2/tests/test_tb231_prose_judge_retry.py`:
  - `test_judge_prose_bullet_retries_on_malformed_json`: stubs
    first SDK response with valid prose + malformed JSON, second
    with valid JSON pass verdict; asserts (a) two `judge_call`
    events fire, (b) one `judge_retry` event fires between them,
    (c) final `CriterionResult.status == "pass"`.
  - `test_judge_prose_bullet_strict_prompt_used_on_retry`:
    captures the second SDK call's system message; asserts the
    string `"single valid JSON object only"` appears in it.
  - `test_judge_prose_bullet_retry_failure_falls_back`: stubs
    both SDK calls with malformed JSON; asserts final
    `CriterionResult.status == "unverified"` with `notes`
    containing `"retry exhausted"`.

## Design

Single-retry shape was chosen over multi-retry to bound cost:
two prose bullets × auto-approve dispatch volume could
double-multiply judge-call cost. One retry covers the observed
failure mode (TB-228 showed valid content + format bug, not
reasoning failure) without unbounded escalation.

The stricter system prompt is purely additive (prepended to the
existing prompt); the content prompt stays unchanged so the
judge's reasoning surface doesn't shift across attempts.

No new env knob: operators who want to disable retries can
already set `AP2_VERIFY_JUDGE_MAX_TURNS=1` to short-circuit
multi-turn judge runs entirely.

## Verification

- `uv run pytest -q ap2/tests/test_tb231_prose_judge_retry.py` — new test module exists and all three retry-path cases pass.
- `uv run pytest -q ap2/tests/test_verify_retry_diff.py` — existing verify-path tests stay green (no regressions).
- `uv run pytest -q ap2/tests/` — full suite green vs current baseline.
- `test -f ap2/tests/test_tb231_prose_judge_retry.py` — test module present on disk.
- `grep -nE "judge_retry" ap2/verify.py` — retry event emission wired into verify.py.
- `grep -nE "single valid JSON object only" ap2/verify.py` — strict-retry system prompt added.
- `grep -nE "retry exhausted" ap2/verify.py` — retry-failure notes path added.
- `grep -nE "_parse_judge_response" ap2/verify.py` — parser extracted into a separate helper.
- Prose: `ap2/verify.py` Prose: the retry fires exactly once per bullet on the malformed-JSON branch and not on the `no JSON object in response` branch (the latter is a more severe failure mode that retry won't fix); judge confirms by reading the retry-decision branch in `_judge_prose_bullet`.

## Out of scope

- Multi-tier retry (e.g. retry with claude-opus when first
  attempt was haiku) — single retry with stricter prompt is the
  proven cheap fix; tier-escalation is a separate cost trade.
- Retroactive re-judging of historical `verification_partial`
  events — one-shot resilience improvement going forward, not a
  backfill task.
- Tightening the response-parser (`_parse_judge_response`) to
  accept prose-then-JSON shapes directly — orthogonal to the
  retry path; parser fixes risk masking real reasoning failures.
- Restructuring the judge's `allowed_tools` set — TB-136 already
  covers Read/Glob/Grep allowance.

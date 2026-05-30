# Harden real-SDK smokes to skip on transient SDK errors instead of false-failing

Tags: #autopilot #smoke #real-sdk #test-reliability #monitoring

## Goal

The real-SDK smokes in `ap2/tests/smoke/` currently hard-fail when the
underlying live Claude call errors at the transport/service level rather
than returning a wrong answer. Observed 2026-05-30:

    test_prose_judge_passes_obvious_pass_case
    got status='unverified' notes='judge error: Exception: Claude Code
    returned an error result: success'

That is NOT a smoke failure — the wiring works; the live service
transiently errored. But the smoke asserts `result.status == "pass"` and
fails hard, which (before the per-task-gate descope) bounced unrelated
tasks through retry, and (after it) will fire a false Mattermost alarm
from the new 6h smoke cron job. The smokes test that the SDK *wiring*
round-trips (MCP tools fire, the judge is reachable and returns a
structured verdict) — they do NOT test Anthropic's uptime. A transport
error means the wiring couldn't be exercised this run: that is an
inconclusive SKIP, not a FAIL.

Make every real-SDK smoke distinguish a transient SDK transport/service
error (→ skip, optionally after one bounded retry) from a genuine wrong
verdict (→ still fail). After this lands, a service blip during the 6h
cron run produces a skipped smoke (exit 0, no alarm) instead of a false
`smoke_check_failed`.

Why now: the operator just moved the smokes to a 6h cron alert; without
this, transient Claude blips — which we've hit repeatedly this week —
will page the operator with false alarms, training them to ignore the
channel (the exact failure mode that makes a canary worthless). The
smokes only earn their place as an alerting signal if a failure reliably
means "our SDK wiring regressed," not "the API had a 5xx." Operator-
directed 2026-05-30; meta-infra test-reliability with no active focus,
so `--skip-goal-alignment`.

## Scope

- **Shared transient-error classifier.** Add a small pure function
  (e.g. `ap2/tests/smoke/_transient.py::is_transient_sdk_error(result)`
  or in the smoke package `__init__`) that returns True for the
  transport/service-error signatures — `status == "unverified"` with a
  notes/message substring matching any of: `judge error`, `returned an
  error result`, `temporarily unavailable`, `overloaded`, `rate limit` /
  `429`, `5xx` / `internal server error`, `timed out` / `timeout`,
  `connection` — and False for a clean `pass`/`fail` verdict (even a
  wrong one). Keep the signature list in one place so all smokes agree.
- **Apply in each smoke** (`test_prose_judge_real_sdk.py`,
  `test_validator_judge_real_sdk.py`, `test_cron_propose_real_sdk.py`,
  `test_pipeline_task_start_real_sdk.py`, `test_report_result_real_sdk.py`):
  before asserting the expected verdict, if the result is a transient
  error, optionally retry the call ONCE, and if it's still transient,
  `pytest.skip(reason=...)` naming the transient signature. A clean
  verdict (right or wrong) flows to the existing assert unchanged — a
  genuinely-wrong verdict still fails the smoke.
- **Bounded retry.** At most one retry per smoke on transient error
  (keeps worst-case cost ~2x, recovers most one-off blips). Do not loop.
- **Non-live unit test** (must live where the normal gate runs it, i.e.
  NOT gated behind `AP2_REAL_SDK`): feed `is_transient_sdk_error`
  synthetic result objects and assert the transient-vs-real mapping —
  the observed `judge error: ... returned an error result` note maps to
  transient; a clean `status='fail'` (wrong-but-confident) maps to real
  (still asserted/failed by the smoke). This pins the classifier without
  spending a live call.

## Design

- **Skip, not xfail.** A transient error is inconclusive, not an
  expected failure; `pytest.skip` reports it cleanly (exit 0, visible
  reason) without xpass churn when the service recovers.
- **Wrong-verdict still fails.** The classifier override is narrow: it
  only catches transport/service errors (which surface as `unverified` +
  an error note). A confident-but-incorrect `pass`/`fail` is a real
  regression and must still fail — this task must not weaken that.
- **One source of truth for signatures.** Centralizing the substring
  list prevents the five smokes from drifting in what they treat as
  transient, and lets the unit test pin it.
- **Composes with the cron job (TB-350).** That task's routine reports
  pass/fail from the suite's exit code; with transient errors skipping,
  a blip yields all-pass-with-skips → exit 0 → no `smoke_check_failed`
  → no alarm. Independent code (smoke tests vs `smoke_runner.py`), so no
  ordering dependency — but this is what makes the cron alert trustworthy.

## Verification

- `uv run --extra dev pytest -q ap2/tests/ --ignore=ap2/tests/smoke` — full suite (the descoped gate) passes, including the new non-live classifier unit test.
- `grep -rnE "pytest\.skip|is_transient_sdk_error" ap2/tests/smoke/` — the smokes skip via the shared transient-error classifier rather than asserting through a transport error.
- `grep -rnE "returned an error result|temporarily unavailable|judge error" ap2/tests/smoke/` — the observed transient signatures are recognized by the classifier.
- `ap2/tests/smoke/` Prose: each of the five real-SDK smokes, on a transient SDK transport/service error (e.g. `unverified` + `returned an error result`), retries at most once and then `pytest.skip`s with a reason instead of failing; a clean-but-wrong verdict still fails. A non-live unit test (runs in the normal gate, not gated on `AP2_REAL_SDK`) pins `is_transient_sdk_error`'s transient-vs-real mapping. Judge confirms via Read.

## Out of scope

- The 6h smoke cron job + its routine/dispatch/alerting (separate task).
- Adding retries/backoff to the daemon's actual SDK call path
  (task/control/verifier agents) — this task touches only the smoke
  tests' interpretation of an already-returned error result.
- Changing what the smokes assert on a clean verdict.

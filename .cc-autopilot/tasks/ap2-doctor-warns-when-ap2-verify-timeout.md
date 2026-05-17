# TB-252 — `ap2 doctor` warns when `AP2_VERIFY_TIMEOUT_S` is below the observed-typical successful full-suite `verify_run` duration (TB-234/TB-239-shape preventive surface for axis-2 failure-recovery)

## Goal

Anchored to goal.md's **Current focus: end-to-end automation**, axis
2 (failure-recovery operator dependency, L88-100): close a
fresh-evidence axis-2 gap by adding a preventive doctor surface that
detects when the project-wide verifier timeout (`AP2_VERIFY_TIMEOUT_S`,
default 600s — see `ap2/config.py:39`, `DEFAULT_VERIFY_TIMEOUT_S`) is
configured below the project's observed-typical full-suite runtime.
Goal.md axis-2 names the failure mode as "failure recovery
(verification fails, retries exhaust, daemon restart, cron drift,
agent timeouts) is fully automatic; only genuine design forks
escalate." This task generalizes that promise from briefing-shape
regressions (TB-225 BriefingFix path) onto a SECOND recurring class —
env-knob-vs-current-workload drift — that the existing axis-2
surfaces (TB-225 BriefingFix auto-apply, TB-233 dry-run, TB-239
misconfiguration WARN) do not catch. Exact pattern mirror of TB-234
(`f350824` — doctor WARN when `AP2_AUTO_APPROVE=1` is set without
token caps) and TB-239: pre-flight diagnostic surface, fail-loud-not-
fail-fast, no auto-mutation, single new audit function in
`ap2/doctor.py` + tests + howto.md cross-reference.

Why now: On 2026-05-17 between 11:30Z and 14:22Z, FIVE consecutive
tasks (TB-245, TB-246, TB-247, TB-249, TB-250) hit
`retry_exhausted last_status=verification_failed` with the IDENTICAL
fingerprint `command=uv run pytest -q ap2/tests/ exit_code=None
duration_s=600.01` — the verifier killed the project-wide regression-
pin bullet at exactly the 600s `AP2_VERIFY_TIMEOUT_S` default while
the agent's own re-run measures the suite at 1320-1349s (TB-245 +
TB-250 summaries name the exact figure: "1734 passed in 1320s" /
"1734 passed in 1349s"). All 5 task implementations are in HEAD; only
the verifier's budget-vs-runtime gap blocks completion. This is
exactly the "failure-recovery operator dependency" cascade goal.md
axis 2 exists to prevent — and the existing axis-2 surfaces missed
it because none of them measure env-knob fitness for current
workload. The current focus on end-to-end automation directly fails
its own delete-test (goal.md L86-88) when this regression goes
silent: the walk-away promise breaks the moment a single task hits
the timeout. Had this doctor WARN existed, the operator would have
seen "AP2_VERIFY_TIMEOUT_S (600s) is below observed-typical successful
verify duration (1349s); recommend bumping to >=1800s" before the
cascade.

## Scope

(1) New audit function `verify_timeout_audit(state_dir: Path, cfg:
    Config) -> list[Issue]` in `ap2/doctor.py` (sibling to
    `auto_approve_audit` at line ~130 and `auto_unfreeze_audit` from
    TB-239 at line ~288). Reads `.cc-autopilot/events.jsonl` tail
    looking at recent successful project-wide verifier completion
    events. Window: last 7 days OR last 20 successful samples,
    whichever covers more.

(2) The audit's verdict logic:
    - If fewer than 3 successful-verify samples in window: emit INFO
      "insufficient data to assess `AP2_VERIFY_TIMEOUT_S` headroom"
      (no WARN — avoid false-positives on fresh installs).
    - Compute `typical_duration_s` = max of the recent successful
      sample set (NOT mean — the worst-case is what blows up).
    - If `cfg.verify_timeout_s` < `typical_duration_s` * 1.0: emit
      WARN with one-line fix: "AP2_VERIFY_TIMEOUT_S=<X>s is below
      observed-typical successful verify duration (<Y>s, n=<N>
      samples over <D> days); recommend `export
      AP2_VERIFY_TIMEOUT_S=<ceil(Y*1.5)>` and `ap2 unfreeze TB-N`
      for any 600s-timeout-shape Frozen tasks."
    - If `cfg.verify_timeout_s` < `typical_duration_s` * 1.5 (the
      "tight" band): emit INFO "AP2_VERIFY_TIMEOUT_S=<X>s has <Z>%
      headroom over recent verifies — consider bumping for safety
      margin."
    - Else: emit INFO "AP2_VERIFY_TIMEOUT_S=<X>s has comfortable
      headroom over observed-typical <Y>s."

(3) Wire `verify_timeout_audit` into the existing doctor entry point
    in `ap2/doctor.py` (call site lives alongside the existing audit
    calls — `auto_approve_audit`, `auto_unfreeze_audit`, etc.).

(4) Tests in `ap2/tests/test_doctor_verify_timeout.py`:
    - `test_verify_timeout_audit_warns_when_timeout_below_typical`:
      synthesize 5 successful-verify events with duration=900s, set
      cfg.verify_timeout_s=600, assert WARN issue is emitted with
      "recommend `export AP2_VERIFY_TIMEOUT_S=" in message.
    - `test_verify_timeout_audit_info_when_insufficient_samples`:
      synthesize 2 events, assert INFO (no WARN).
    - `test_verify_timeout_audit_info_when_comfortable_headroom`:
      synthesize 5 events with duration=200s, cfg.verify_timeout_s=
      600, assert INFO ("comfortable headroom"), no WARN.
    - `test_verify_timeout_audit_handles_missing_events_file`:
      no events.jsonl, assert INFO ("insufficient data"), no
      crash.

(5) Documentation: add a cross-reference line under the existing
    `AP2_VERIFY_TIMEOUT_S` row in `ap2/howto.md` (currently L854):
    "`ap2 doctor` warns when set below observed-typical successful
    verify duration." No new env knob introduced.

## Design

Read `.cc-autopilot/events.jsonl` line-by-line, filter by event-type
matching the verifier's per-run completion event for SUCCESSFUL runs
(confirm the exact event-type name via `grep -n "verify_run\|verification_passed"
ap2/verify.py ap2/daemon.py` before implementing; the right event is
the one whose payload carries `duration_s` for successful runs).
Window by either `ts` (last 7 days) or take last 20 successful
samples — pick whichever yields the larger sample set.

Use `max(durations)` not `mean(durations)` — the worst-case
successful run is the realistic ceiling for sizing the timeout. A
1349s P100 matters more than an 850s mean when the timeout is 600s.

No new env knobs. Internal constants for the 7-day / 20-sample
window and the 1.0× / 1.5× WARN/INFO bands live as module-level
constants in `ap2/doctor.py` next to the audit function.

Issue severity uses the existing doctor `Issue(level=...)` enum —
WARN matches TB-234 / TB-239 precedent, INFO for the no-issue and
insufficient-data branches.

## Verification

- `uv run pytest -q ap2/tests/test_doctor_verify_timeout.py` — new test
  module passes (4 tests).
- `grep -n "verify_timeout_audit" ap2/doctor.py` — new audit function
  defined and called from the doctor entry point (post-change grep
  returns ≥2 lines: the def and the call site).
- `grep -n "AP2_VERIFY_TIMEOUT_S" ap2/howto.md` — howto.md mentions
  the doctor cross-reference (post-edit grep returns the new line in
  addition to the existing L854 row).
- `test -f ap2/tests/test_doctor_verify_timeout.py` — new test file
  exists at the named path.
- Prose: the new audit function in `ap2/doctor.py` uses `max()` over
  recent successful verify durations (not `mean()` or any other
  central-tendency statistic) — judge confirms via Read on
  `ap2/doctor.py` after the change lands.
- Prose: `ap2/doctor.py` Prose: the new `verify_timeout_audit`
  function reads `.cc-autopilot/events.jsonl` and filters for the
  successful-verify event type that carries `duration_s` — judge
  confirms via Read on the new function body.

## Out of scope

- Auto-mutating `AP2_VERIFY_TIMEOUT_S` (operator-only, per goal.md
  Constraints L210-220).
- Auto-unfreezing the 5 timeout-shape Frozen tasks (TB-225 doesn't
  cover env-knob-mismatch fix-shapes; operator owns this unfreeze
  path).
- Raising `DEFAULT_VERIFY_TIMEOUT_S` from 600 to a larger value in
  `ap2/config.py` (goal-direction call; operator decides).
- Per-task verifier timeout (separate axis — would require a new
  briefing-frontmatter field; not in this task's scope).
- Reactive "frozen-task count exceeded threshold" surface (separate
  task; this one is preventive on a per-knob basis).

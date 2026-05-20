## Goal

Close an axis-1 gap inside the **Current focus: end-to-end
automation** roadmap: the TB-235 dep-coherence validator-judge —
the load-bearing upstream gate that goal.md L82-85 commits as the
"upstream gates already make this safe in practice" floor — has
been silently fail-open on essentially every operator queue-append
for the last 7+ days. TB-257's investigation artifact
(`.cc-autopilot/insights/validator-judge-timeout-2026-05-18.md`)
measured the real `_judge_dep_coherence_default` call at 17.6-46.8s
wall-clock and explicitly named `timeout-too-tight` as the dominant
factor: the 15s `AP2_VALIDATOR_JUDGE_TIMEOUT_S` default + 5s
outer-thread grace (`worker.join(timeout=timeout_s + 5)` in
`ap2/validator_judge.py:439`) totals a 20s ceiling that sits BELOW
the median completion of even the smallest measured briefing
(4621 B → ~22s avg). This TB ships the calibration follow-up the
artifact explicitly called out as deferred, paired with a TB-252-
shape preventive doctor audit so the same calibration-drift class
can't silently re-degrade after a future workload shift.

Why now: 15 `validator_judge_timeout` events in the last 500 events
(8 in the trailing 24h alone per `ap2 status`); TB-243 fail-open
hides the cost from the user-facing path but the gate contributes
zero gating verdicts on the queue-append set. Without this
calibration, the load-bearing "upstream gates already make this
safe in practice" claim on goal.md L82-85 is documentation fiction
— the Current focus: end-to-end automation roadmap cannot honestly
flip auto-approve on while the safety floor is empty.

## Scope

1. Bump `_VALIDATOR_JUDGE_TIMEOUT_S_DEFAULT` in
   `ap2/validator_judge.py:44` from `15.0` to `60.0`. Rationale per
   TB-257 artifact: ceiling = 1.5× the artifact's measured worst
   case of ~47s (rounded up), matching TB-252's
   `_VERIFY_TIMEOUT_AUDIT_FIX_MULT` recommendation pattern.
   Operators can still tighten via env knob; default sits above the
   real-world ceiling instead of below the median.

2. Emit a new `validator_judge_passed` event on every successful
   `_judge_dep_coherence_default` call, with payload fields
   `{type, ts, duration_s, briefing_bytes, max_turns, timeout_s}`
   — mirrors TB-252's `verify_passed` shape verbatim. Emission
   site: just after the worker thread returns successfully in
   `ap2/validator_judge.py` (before the JSON parse). Document the
   new event type in `ap2/events.py` (alongside the existing
   `verify_passed` (TB-252) entry).

3. Add `validator_judge_timeout_audit(state_dir, cfg) -> AuditResult`
   to `ap2/doctor.py`, mirroring `verify_timeout_audit` (`ap2/
   doctor.py:437`) verbatim with `verify_passed` → `validator_
   judge_passed` and `AP2_VERIFY_TIMEOUT_S` → `AP2_VALIDATOR_
   JUDGE_TIMEOUT_S`. Reuse the same `_iter_*_durations` shape (lift
   the body into a module-private `_iter_passed_durations` helper
   taking the event-type string as a parameter, then have both
   `_iter_verify_passed_durations` and the new
   `_iter_validator_judge_passed_durations` delegate — keep the
   public function names so any existing tests pinning them stay
   green). Wire the new audit into `diagnose()` directly after the
   existing `verify_timeout_audit` call.

4. Update the TB-257 insight artifact `.cc-autopilot/insights/
   validator-judge-timeout-2026-05-18.md`: add a `## Calibration
   applied (TB-269)` section at the bottom (do NOT mutate the
   existing measurement sections) noting the new default value and
   the follow-up event emission. Update the YAML front matter's
   `updated` to the TB-269 completion date and `updated_by` to
   `TB-269`.

5. Update `ap2/howto.md`: the existing `AP2_VALIDATOR_JUDGE_
   TIMEOUT_S` reference (search `AP2_VALIDATOR_JUDGE_TIMEOUT_S`) to
   mention the new 60s default + cross-reference the doctor audit +
   cite TB-257 + TB-269.

6. Regression-pin module `ap2/tests/test_tb269_validator_judge_
   timeout_calibration.py` covering: (a) `_VALIDATOR_JUDGE_TIMEOUT_
   S_DEFAULT == 60.0` constant pin; (b) `validator_judge_passed`
   event emission shape (mock the SDK call returning a fast
   response, assert event lands with `duration_s` + `briefing_
   bytes` + `timeout_s` keys); (c) `validator_judge_timeout_audit`
   verdict bands (insufficient samples → INFO; timeout below
   typical → WARN; comfortable → INFO) using synthesized
   `validator_judge_passed` rows in a temp events.jsonl.

## Design

The TB-252 pattern is intentionally template-shaped — its docstring
calls itself an "axis-2 mirror of TB-234/239 misconfiguration-floor
audit." TB-269 is the axis-1 mirror onto the validator-judge
surface. Keeping the implementation strictly parallel (lift the
iterator helper, keep the audit-function shape, mirror the verdict
bands) makes the operator's mental model uniform across `ap2
doctor` output and makes a future "fold both into one parametric
audit" refactor obvious.

The 60s bump is conservative but principled: TB-257's measurement
covered 3 SDK calls against the smallest briefing in the
distribution; real briefings averaged ~6KB and the artifact's
"prompt-too-heavy" secondary factor means real-world calls likely
sit closer to 40-50s than 22s. 60s is the smallest round number
above 1.5× the 47s worst case the artifact measured.

`validator_judge_passed` is named for symmetry with `verify_passed`
(TB-252) and the existing `validator_judge_fail` /
`validator_judge_timeout` events (TB-243/247) — the operator now
sees the full happy-path/fail-open/timeout triangle on a single
namespace.

## Verification

- `uv run pytest -q ap2/tests/test_tb269_validator_judge_timeout_calibration.py` — new regression-pin module passes (all 3 scope bullets covered).
- `uv run pytest -q ap2/tests/` — full suite passes (no regressions in existing doctor / validator-judge / events tests).
- `grep -nE "_VALIDATOR_JUDGE_TIMEOUT_S_DEFAULT[[:space:]]*=[[:space:]]*60\.0" ap2/validator_judge.py` — exits 0 (default bumped to 60.0).
- `grep -nE "validator_judge_passed" ap2/events.py ap2/validator_judge.py ap2/doctor.py` — exits 0 in all three files (event documented + emitted + consumed by audit).
- `grep -nE "def validator_judge_timeout_audit" ap2/doctor.py` — exits 0 (new audit function present).
- `grep -cE "validator_judge_timeout_audit" ap2/doctor.py` — prints a number ≥ 2 (definition + diagnose wire-up).
- `grep -nE "TB-269|AP2_VALIDATOR_JUDGE_TIMEOUT_S" ap2/howto.md` — exits 0 with ≥1 match (howto cross-reference present).
- `grep -nE "Calibration applied \(TB-269\)" .cc-autopilot/insights/validator-judge-timeout-2026-05-18.md` — exits 0 (artifact append-only update present).
- Prose: the new `validator_judge_timeout_audit` in `ap2/doctor.py` mirrors `verify_timeout_audit`'s verdict-band structure (insufficient/WARN-below-typical/INFO-tight/INFO-comfortable) — judge confirms by Read of the new function's body and visual comparison against `verify_timeout_audit` at `ap2/doctor.py:437-540`.

## Out of scope

- Adaptive timeout (auto-tune from observed P95 distribution) —
  premature without baseline calibration first; defer to a follow-
  up TB after TB-269 + TB-270 land and re-measurement happens.
- Per-task validator-judge linkage on `ap2 status` (which queue-
  append got fail-open'd?) — TB-243 count surface is already
  actionable; complexity not justified without operator demand.
- Refactoring `_iter_verify_passed_durations` into a fully generic
  utility used by other audits — the lift to `_iter_passed_
  durations` taking the event-type string is the minimum-viable
  shared helper; broader generalization waits for a third caller.

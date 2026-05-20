## Goal

Close an axis-1+3 cross-cut safety-floor gap inside the **Current
focus: end-to-end automation** roadmap: the TB-235 dep-coherence
validator-judge — the load-bearing upstream gate that goal.md
L82-85 commits as the "upstream gates already make this safe in
practice" floor — can silently fail-open at high rate while
`AP2_AUTO_APPROVE=1` continues stripping `@blocked:review` and
dispatching ideation proposals. Today
`automation_status.validator_judge_noisy_threshold` (default 5 per
TB-243) drives ONLY a cosmetic `[noisy]` suffix on `ap2 status`
text and a warn-tint on the web automation card — there is NO
`validator_judge_noisy` entry in the `_pause_reason` discriminator
vocabulary (verified: `grep -n validator_judge ap2/auto_approve.py`
returns zero matches) and the auto-approve dispatch gate runs
unaffected. This TB ships the symmetric pause discriminator
mirroring `consecutive_freezes`, gates the auto-approve write step
on it, and surfaces the new pause-reason token through the
existing renderers (`ap2 status` text/JSON, web home automation
card, status-report cron digest) — zero new operator-facing
surfaces; the existing `ap2 ack auto_approve_unfreeze` resume verb
clears the pause exactly like `consecutive_freezes` clears today.

Why now: current `ap2 status` line prints "auto-approve: disabled
(validator-judge 24h: 0 fail, 11 timeout [noisy])" — the noisy
threshold IS already crossed in the trailing 24h on production
data, but flipping `AP2_AUTO_APPROVE=1` today would give 11/11
fail-open against the load-bearing axis-1 dep-coherence gate
goal.md L82-85 commits as the safety floor. TB-269 / TB-270 attack
the timeout root cause and ship the median-case fix; this TB ships
the belt-and-suspenders safety floor that holds even if
calibration regresses or a future SDK update reopens the timeout
window. Without the pause, the operator can never honestly flip
auto-approve on while keeping goal.md's walk-away promise — the
gate is documentation fiction whenever the noisy threshold is
crossed.

## Scope

1. Extend the `pause_reason` discriminator vocabulary in
   `ap2/automation_status.py`:
   - Add `"validator_judge_noisy"` as a new return value from
     `_pause_reason` (currently at L325). When `(validator_judge_
     fail_count_24h + validator_judge_timeout_count_24h) >=
     validator_judge_noisy_threshold()`, return
     `"validator_judge_noisy"` as the highest-priority reason
     (overrides cost/freeze pauses when both fire — the operator's
     signal-clarity choice; safety-floor failure is the strictest).
   - Add `"validator_judge_noisy"` to `_PAUSE_REASON_ACK_VERB`
     (L68) mapping to `"auto_approve_unfreeze"` — same resume verb
     the operator already uses for `consecutive_freezes`. No new
     CLI verbs, no new ack tokens.
   - Update `collect_auto_approve_state` (L357) docstring's
     enumeration of `pause_reason` values to include the new
     token.

2. Gate the auto-approve write step on the new pause-reason in
   `ap2/auto_approve.py`:
   - In the dispatch path that today checks `pause_reason` for
     `consecutive_freezes` / cost halts (locate by grepping
     `pause_reason` in `auto_approve.py`), extend the skip branch
     to also fire on `validator_judge_noisy`. When the skip
     branch fires for this reason, emit an `auto_approve_skipped`
     event with payload `{type, ts, task, reason:
     "validator_judge_noisy", fail_count_24h, timeout_count_24h,
     threshold}` — same shape `auto_approve_paused` /
     `auto_approve_halted` use for their structured payloads.
   - Document the new event payload variant in `ap2/events.py`
     alongside the existing `auto_approve_paused` /
     `auto_approve_halted` entries.

3. Add an opt-out knob `AP2_AUTO_APPROVE_NOISY_PAUSE_DISABLED`
   (truthy-set parse identical to `_is_truthy` at
   `automation_status.py:76`). Default unset → False (pause
   active). When set, the noisy-state check returns None from
   `_pause_reason` so the gate behaves exactly as it does today
   (cosmetic-only `[noisy]` surface). Add the knob to
   `HOT_RELOADABLE_KNOBS` in `ap2/env_reload.py:74` so operators
   can flip it without daemon restart (TB-271 path).

4. Reuse the existing renderers (no new render code):
   - `ap2/cli_daemon.py:cmd_status` text path already renders
     `pause_reason` through `_pause_reason_display_name` —
     register the new token's display string ("validator-judge
     noisy") in the same display-name mapping.
   - `ap2/web_home.py:_render_automation_card` already renders
     `pause_reason` via the same token-string mapping; same
     register.
   - `ap2/status_report.py:render_automation_loop_activity_section`
     already pulls `pause_reason` from the collector and surfaces
     "PAUSED reason=<token>" in the cron digest; the new token
     surfaces automatically once the collector emits it.

5. Update `ap2/howto.md`: in the env-knobs reference section,
   add `AP2_AUTO_APPROVE_NOISY_PAUSE_DISABLED` with the default
   (unset → pause active), cross-reference TB-243's
   `AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD`, and cite TB-272.

6. New regression-pin module `ap2/tests/test_tb272_validator_
   judge_noisy_pause.py`:
   - `test_pause_reason_returns_validator_judge_noisy_above_
     threshold` — seed N=5 `validator_judge_timeout` events in
     trailing 24h via temp events.jsonl; assert
     `collect_auto_approve_state(cfg)["pause_reason"] ==
     "validator_judge_noisy"`.
   - `test_pause_reason_returns_validator_judge_noisy_with_mixed_
     fail_and_timeout` — seed N=3 fail + N=3 timeout; combined
     count crosses the default-5 threshold; assert same.
   - `test_pause_reason_priority_over_consecutive_freezes` — seed
     noisy + 3 consecutive freezes; assert
     `"validator_judge_noisy"` returned (safety-floor priority).
   - `test_noisy_pause_disabled_knob_opts_out` — set
     `AP2_AUTO_APPROVE_NOISY_PAUSE_DISABLED=1` via monkeypatch;
     seed noisy state; assert `pause_reason` is None or one of
     the cost/freeze tokens but NOT `validator_judge_noisy`.
   - `test_auto_approve_skipped_when_validator_judge_noisy` —
     drive the `auto_approve.py` dispatch path against a seeded
     noisy state; assert the `@blocked:review` codespan stays
     intact on the task and an `auto_approve_skipped` event with
     `reason="validator_judge_noisy"` lands in events.jsonl.
   - `test_ack_verb_mapping_includes_validator_judge_noisy` —
     pin `_PAUSE_REASON_ACK_VERB["validator_judge_noisy"] ==
     "auto_approve_unfreeze"`.
   - `test_hot_reloadable_knob_set_includes_noisy_pause_
     disabled` — pin `"AP2_AUTO_APPROVE_NOISY_PAUSE_DISABLED" in
     env_reload.HOT_RELOADABLE_KNOBS`.

## Design

The `pause_reason` discriminator pattern (introduced in TB-223 for
`consecutive_freezes` and extended in TB-224 for cost halts) is
intentionally a string-token enumeration consumed identically
across CLI/web/cron-digest renderers. Adding a new token requires
ONE change at the collector + ONE change at the dispatch gate;
every downstream surface picks up the new state mechanically via
the existing `pause_reason`-string path. This is exactly the shape
goal.md L102-113 names as the safety floor: a gate that closes a
specific failure mode at the chokepoint, reuses operator-trained
ack vocabulary, and degrades safely (the opt-out knob exists for
operators who want the status-quo cosmetic-only behavior).

Priority ordering (validator-judge noisy > consecutive freezes >
cost halts) reflects the operator's failure-mode hierarchy: a
silently-disabled upstream gate is a stricter failure than a
streak of bad task verdicts (which the consecutive-freezes pause
already catches) or a cost overrun (which the per-task / window
caps already catch). Surfacing the most severe reason gives the
operator the clearest single-line diagnosis on `ap2 status`.

The new event payload shape mirrors `auto_approve_paused` /
`auto_approve_halted` exactly so a future "consolidate all halt
events into one parametric type" refactor is obvious.

## Verification

- `uv run pytest -q ap2/tests/test_tb272_validator_judge_noisy_pause.py` — new regression-pin module passes (all 6 scope bullets covered).
- `uv run pytest -q ap2/tests/` — full suite passes (no regressions in TB-227 automation status, TB-223/224/228 auto-approve pause, TB-243 noisy threshold tests).
- `grep -nE "validator_judge_noisy" ap2/automation_status.py` — exits 0 with ≥2 matches (discriminator return + ack-verb mapping).
- `grep -nE "validator_judge_noisy" ap2/auto_approve.py` — exits 0 with ≥1 match (dispatch gate branch).
- `grep -nE "AP2_AUTO_APPROVE_NOISY_PAUSE_DISABLED" ap2/env_reload.py ap2/howto.md` — exits 0 with ≥2 matches (env-reload allowlist + howto reference).
- `grep -nE "auto_approve_skipped" ap2/events.py ap2/auto_approve.py` — exits 0 (event documented + emitted).
- `grep -nE "TB-272" ap2/howto.md` — exits 0 (howto cross-reference present).
- Prose: the new `validator_judge_noisy` branch in `automation_status._pause_reason` reads from the same 24h-window event tail that TB-243's `validator_judge_fail_count_24h` / `validator_judge_timeout_count_24h` already use — judge confirms by Read of `_pause_reason` and visual comparison against `collect_auto_approve_state`'s existing count-derivation code at `automation_status.py:357-535`.

## Out of scope

- `ap2 doctor` pre-flight warn when `AP2_AUTO_APPROVE=1` AND
  validator-judge noisy — sibling-shape mirror of TB-234/TB-239
  doctor warns. Worthwhile but secondary to the runtime pause;
  defer to a follow-up TB if the runtime pause leaves a
  pre-flight surface gap that operators want closed.
- Per-task validator-judge linkage on `ap2 status` (which queue-
  append got fail-open'd?) — deferred per prior ideation cycle.
- Adaptive validator-judge timeout (auto-tune from observed P95)
  — TB-269's lane; premature without baseline calibration first.
- Changing the default `AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD` (5)
  — TB-243's calibration choice; this TB inherits the threshold
  verbatim. Re-calibration of the threshold itself is a separate
  TB if real-world data after TB-269 lands shows the default is
  off-target.

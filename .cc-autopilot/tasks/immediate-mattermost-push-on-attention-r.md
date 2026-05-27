## Goal

Add an opt-in immediate Mattermost push so a detector fire (e.g.
`auto_approve_paused`, `task_stuck`, `cost_cap_approach`) reaches
the walk-away operator inside one tick instead of waiting up to
2h for the next status-report cron. The push is OFF by default
(`AP2_ATTENTION_IMMEDIATE_PUSH=0`); operators opt in once they've
verified their detector cadence is low enough not to noise the
channel. Closes Current focus: operator-legible reporting and
monitoring's third Progress signal — "Attention-needing conditions
(stuck / failed / frozen tasks, decisions-needed, cost or
validator-judge anomalies) are surfaced proactively in
operator-legible terms, distinct from routine progress updates" —
on the push-cadence side; the per-2h status-report cron remains
the routine push surface.

Why now: the upstream attention vocabulary fully landed (5
detectors shipped: task_stuck, task_frozen, validator_judge_noisy,
auto_approve_paused, cost_cap_approach). Several of those
conditions are time-sensitive — `auto_approve_paused` fires only
after dispatch has already halted, and `cost_cap_approach` is
explicitly a pre-trip nudge that loses utility if delayed by 2h.
The operator named "immediate-MM push" verbatim as a remaining
axis in the 2026-05-27T06:33:52Z rewind reason on focus-2.
Without this, the time-from-condition to operator-glance is
bounded below by the status-report cron interval — defeating the
"proactively surfaced" claim of Progress signal #3 for the
post-trip / time-sensitive conditions.

## Scope

(1) New env knob `AP2_ATTENTION_IMMEDIATE_PUSH` wired through
`ap2/config.py` (`DEFAULT_ATTENTION_IMMEDIATE_PUSH = False`) and
`ap2/env_reload.py` `HOT_RELOADABLE_KNOBS` (operator can toggle
without daemon restart). Bool parse same as the sibling
`AP2_FOCUS_AUTO_ADVANCE_DISABLED` style.

(2) Extend `_maybe_emit_attention_events` in `ap2/daemon.py`.
Inside the per-candidate loop, AFTER the existing
`should_suppress` debounce check and AFTER the `attention_raised`
event append succeeds, call a new `_maybe_push_attention(cfg,
cond)` helper. The helper:
  - returns early when `AP2_ATTENTION_IMMEDIATE_PUSH` is false;
  - returns early when `_first_mm_channel()` returns "" (parity
    with the watchdog's missing-destination short-circuit;
    emits one sticky `attention_push_no_destination` audit event
    then suppresses further such audits via a state-file flag,
    mirroring the watchdog's `warned_no_destination` pattern);
  - composes a one-line message: project-name prefix (sourced
    from the existing project-name helper the status-report
    cron uses, looked up at call-time so this task doesn't
    duplicate that helper) + `⚠` + the detector's `summary`
    field;
  - calls `tools._mm_post(channel, text)`; on success emits
    `attention_pushed type=<...> key=<...> channel=<...>
    post_id=<...> summary=<...>`; on failure emits
    `attention_push_error channel=<...> error=<...>` and
    continues (a push hiccup must not abort the tick).

(3) Per-(attention_type, key) push debounce piggybacks on the
existing `attention_raised` debounce: because the push runs only
after a fresh `attention_raised` is appended, the same
`AP2_ATTENTION_DEBOUNCE_S` window (default 21600s / 6h)
suppresses repeat pushes structurally — no new state file
needed. A condition that fires once at minute 5 of an hour and
remains active is pushed once; the next push waits the full
debounce window.

(4) Register `attention_pushed`, `attention_push_error`, and
`attention_push_no_destination` event types in `ap2/events.py`
(docstring entries paralleling the existing
`attention_raised` registration); add `attention_pushed` to
`_STATUS_REPORT_AUTOMATION_INTERESTING_TYPES` so a fresh push
un-skips the dedup/idle gate (lets the next status-report
acknowledge the push happened).

(5) Regression-pin module
`ap2/tests/test_tb297_attention_immediate_push.py` covers:
push opt-out by default (knob off → no post, no event); knob on
+ destination set → one `_mm_post` call per fresh condition with
the documented one-line shape (project name + glyph + summary);
knob on + `AP2_MM_CHANNELS` unset → one
`attention_push_no_destination` event then no further such
events on subsequent fires (sticky flag); knob on + `_mm_post`
raises → `attention_push_error` event emitted; debounce reuse —
a condition that just fired produces an `attention_raised` but
NO second push within `AP2_ATTENTION_DEBOUNCE_S`; project-name
prefix present in the post text (verifiable by monkeypatching
the project-name helper and asserting the post text changes).

(6) Documentation: add `AP2_ATTENTION_IMMEDIATE_PUSH` to
`ap2/howto.md`'s knob-reference table (alongside the existing
attention knobs); note the push surface in `ap2/architecture.md`
alongside the existing attention-detector inventory.

## Design

Inline in `_maybe_emit_attention_events` keeps the push co-located
with the event emission it mirrors — same per-(type, key)
debounce, same "best-effort, can't abort the tick" failure mode.
Conservative-default opt-in via env knob honors goal.md Non-goals
L253-256 ("Unconditional automation: auto-approve, auto-unfreeze,
and any other operator-in-the-loop relaxation are OPT-IN env
knobs with conservative defaults"); operators enable per project
once they've sampled their own detector cadence. Re-uses the
watchdog's `_mm_post` + `_first_mm_channel` chain to avoid a
parallel Mattermost-routing path (one bug-surface, one channel
discovery).

## Verification

- `grep -Eq "AP2_ATTENTION_IMMEDIATE_PUSH" ap2/config.py` — env knob default declared.
- `grep -q "AP2_ATTENTION_IMMEDIATE_PUSH" ap2/env_reload.py` — knob listed in `HOT_RELOADABLE_KNOBS`.
- `grep -Eq "_maybe_push_attention|attention_pushed" ap2/daemon.py` — push helper + event call-site wired.
- `grep -q "attention_pushed" ap2/events.py` — event type registered.
- `grep -q "attention_pushed" ap2/status_report.py` — event in `_STATUS_REPORT_AUTOMATION_INTERESTING_TYPES`.
- `test -f ap2/tests/test_tb297_attention_immediate_push.py` — regression-pin module exists.
- `uv run pytest -q ap2/tests/test_tb297_attention_immediate_push.py` — module passes.
- `uv run pytest -q ap2/tests/` — full suite passes.

## Out of scope

- Cross-project channel routing (per-project legibility scope
  guard at goal.md focus-2 L227-228 explicitly excludes
  cross-project aggregation; this push posts only to
  `AP2_MM_CHANNELS[0]`, the single per-project channel the
  watchdog / status-report cron already use).
- Threading / replying-in-thread to a prior post (each push is
  a top-level message; thread management is a richer-surface
  follow-up).
- Per-detector-type opt-in granularity (the knob is a single
  on/off for the whole attention surface; if cadence turns out
  noisy in production, a future task can split per-type).
- An `attention_resolved` push when conditions clear (would need
  the deferred `attention_cleared` event class; out-of-band of
  this task).
- Modifying the detector module itself or the
  `attention_raised` debounce contract — this task is purely an
  additive consumer of the existing `attention_raised` stream.

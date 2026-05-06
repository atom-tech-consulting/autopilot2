# Fix `ideation_skipped_no_slots` event spam — slot check fires before cooldown gate (TB-183 regression)

## Goal

This task is anchored in goal.md's `## Done when` criterion: "An operator can point ap2 at a fresh project, paste a goal.md (with Mission + `## Done when`), and walk away for a week without intervention." Runaway event-log spam directly breaks that walk-away promise — the operator returning after a week to a `events.jsonl` that's been emitting the same `ideation_skipped_no_slots` event every 30s for hours/days has lost their primary forensic surface (events.jsonl is the canonical aggregation surface for every operator-facing audit) to noise.

TB-183 introduced an early-skip path in `_maybe_ideate` for the case where the queue is at-or-above the operator's `AP2_IDEATION_TRIGGER_TASK_COUNT` threshold (slots=0). The commit message explicitly stated: "advance the cooldown via `mark_run` (so a wedged-at-threshold board can't hammer the gate every tick)." That intent didn't reach the code — the slot check was placed BEFORE the cooldown check in the gate-ordering, so the early-skip branch returns before the cooldown check ever runs. `mark_run` updates `cron_state.json[ideation].last_run`, but the slot-skip branch doesn't read that timestamp before emitting next time.

Empirical confirmation: post-train's events.jsonl currently has **17 `ideation_skipped_no_slots` events**, with 16 of them in a 9.5-minute window — exactly one per ~30s tick interval. autopilot2 (lower spam rate due to less time at-or-above threshold) has 4. Both projects are affected; both will continue accumulating until the fix lands.

The fix is a gate-ordering swap: move the cooldown check above the slot check. After the swap, when slots=0 the path emits the event + calls `mark_run` (writing `last_run = now`), and on the next tick the cooldown check (now positioned ABOVE the slot check) returns silent until the cooldown elapses. Result: at most one event per cooldown window (default 2h), as TB-183's commit message originally claimed.

Why now: the spam is actively biting in production right now (~120 events/hour in post-train at current cadence). Each tick that doesn't fix it adds two more events to events.jsonl across the two projects. Filing this fix promptly prevents the events.jsonl tail from being permanently dominated by skip-spam over the operator's walk-away window — this is exactly the "runaway noise" failure mode that breaks the walk-away promise the goal.md anchors on.

## Scope

- `ap2/ideation.py::_maybe_ideate` (lines 599-650, post-TB-183) — swap the slot-check block (currently lines 608-622) with the cooldown-check block (currently lines 623-628). Final gate ordering should be: AP2_IDEATION_DISABLED → Active hard gate → cooldown → slot check (emit + mark_run + return) → focus-exhausted gate → `_run_ideation`.
- `ap2/ideation.py::_maybe_ideate` (docstring at lines 559-598) — update the gates-in-order list to reflect the corrected ordering. The current docstring lists slot check at #3 and cooldown at #4; after the fix they swap to #3 (cooldown) and #4 (slot check).
- `ap2/tests/test_ideation*.py` — extend with a regression test that pins the cooldown-suppression behavior. The TB-183 tests verify single-call behavior; the bug is in repeat-call behavior. Concretely: synthesize slots=0 + cooldown elapsed, call `_maybe_ideate` twice back-to-back, assert exactly ONE `ideation_skipped_no_slots` event. Advance a mocked clock past the cooldown, call a third time, assert a second event lands.

## Design

### Gate ordering rationale

The cooldown check is the appropriate "rate-limit" gate for any cron-driven ideation behavior. It MUST run before any branch that emits + advances the cooldown clock — otherwise the clock advancement in those branches is decorative (the next tick's branch doesn't read the clock; it short-circuits before the cooldown check is reached).

TB-174's focus-exhausted gate (lines 636-649) was correctly positioned AFTER the cooldown check — its emit + mark_run pattern works as intended. TB-183's slot-check was incorrectly positioned BEFORE the cooldown check, breaking the same pattern.

After the fix, the gates have a consistent invariant: **any gate that emits an event + calls `mark_run` MUST be positioned after the cooldown check**, so the cooldown clock effectively gates the emit rate.

### Why not gate the slot-skip emission on cooldown elapsed inside the slot-check branch

Alternative considered: keep current ordering, but add an `if now - last < cooldown: return` guard at the top of the slot-check block. Rejected because:

- Duplicates the cooldown logic in two places (once at the top-level cooldown gate, once inside the slot-check branch)
- Same logic shape would have to be added to the focus-exhausted gate too if it ever moved before cooldown — fragile
- Re-ordering the gates is the structural fix; per-branch guards are a patch

### Backwards compatibility

- `mark_run` write semantics unchanged — still writes `last_run = now` to cron_state.json
- `ideation_skipped_no_slots` event payload unchanged — same `queued`, `threshold` fields
- Slot-count derivation (`_compute_slots`) unchanged — same `max(0, threshold - workable)` math
- Cooldown semantics unchanged — same `AP2_IDEATION_COOLDOWN_S` env knob, same default 7200s
- The TB-183 commit message's stated intent ("at most one event per cooldown window") was correct; only the implementation gate-ordering was wrong. The fix matches the original intent.

### Existing TB-183 tests should still pass

The TB-183 test suite (per its commit message) pins:
- Slot math (workable=3, threshold=5 → slots=2)
- The `max(0, ...)` clamp (workable=6, threshold=5 → slots=0)
- The early-skip path (slots=0 emits + mark_run + SDK never invoked)
- Backwards compat (default threshold=3 with empty board → slots=3)

All four pin BEHAVIOR for a single call, not call-rate. They continue to pass after the gate-ordering swap. The new regression test added by THIS task fills the call-rate gap.

### Empirical validation post-fix

After the fix lands, observe the post-train and autopilot2 events.jsonl: `ideation_skipped_no_slots` should appear at most once per 2h cooldown window per project, not once per 30s tick. Confirm via:

```
grep "ideation_skipped_no_slots" .cc-autopilot/events.jsonl | tail -5
```

…and verify the timestamps are spaced ~7200s apart, not ~30s apart.

## Verification

- `uv run pytest -q ap2/tests/` — full regression gate passes (gating). All four pre-existing TB-183 single-call behavior tests should still pass.
- `python3 -c "import inspect; from ap2 import ideation; src = inspect.getsource(ideation._maybe_ideate); cd_idx = src.find('cooldown'); slot_idx = src.find('slots'); assert cd_idx < slot_idx, f'cooldown check must precede slot check (cooldown at {cd_idx}, slots at {slot_idx})'"` — source-level assert that the cooldown-check text appears before the slot-check text in `_maybe_ideate`'s body.
- prose: a regression test in `test_ideation*.py` named `test_maybe_ideate_slot_skip_respects_cooldown` synthesizes a fixture with slots=0 (e.g. workable=5, threshold=5) AND cooldown elapsed (advance the mock clock past last_run + cooldown_s). Calls `_maybe_ideate` twice back-to-back without advancing the clock between calls. Asserts:
  - The first call emits exactly ONE `ideation_skipped_no_slots` event AND calls `mark_run`
  - The second call emits ZERO new events (cooldown gate suppresses the second skip-emission)
- prose: a follow-on regression test named `test_maybe_ideate_slot_skip_re_emits_after_cooldown` advances the mocked clock past the cooldown after the first emission, calls `_maybe_ideate` a third time, asserts a second `ideation_skipped_no_slots` event lands.
- prose: a test pins the docstring update — `_maybe_ideate.__doc__` enumerates the gates in their post-fix order (cooldown listed at gate #3, slot check at gate #4). Greppable phrasing pin: "Cooldown" appears before "proposal-slot budget" in the docstring's gate list.
- prose: a manual-verification note in the briefing's commit message includes the empirical-validation step (`grep "ideation_skipped_no_slots" .cc-autopilot/events.jsonl | tail -5` showing ~7200s gaps post-fix). Operator-checklist style; not a unit test, but worth calling out so the implementer notices the production validation.

## Out of scope

- Refactoring `_maybe_ideate` further — extracting helpers, deduplicating the skip-and-mark-run pattern across branches, etc. Pure gate-ordering swap; structural cleanup is a separate concern.
- Changing the TB-183 slot-check semantic itself (the `max(0, threshold - workable)` math, the `slots <= 0` skip condition, the event payload shape). All of that stays exactly as TB-183 designed.
- Backfilling / cleaning up the existing spam events in events.jsonl. They stay as historical record; the fix prevents future accumulation.
- Adding a generic test framework that asserts "any gate emitting + calling mark_run is positioned after the cooldown check" structurally. Out of scope; today's manual gate-by-gate review is sufficient given the small number of gates.
- Auditing other modules (status_report, cron-job dispatch) for similar gate-ordering bugs. Same code pattern (emit + mark_run early-return) doesn't appear in those modules; verified via a quick grep when filing this task. If a similar bug surfaces in another module, that's a separate fix-TB.
- Adding an env knob to disable the slot-skip emission entirely. The fix restores the original "once per cooldown window" rate; if operators want to silence further they can raise `AP2_IDEATION_COOLDOWN_S`.
- Renaming `ideation_skipped_no_slots` to anything else.

# TB-246: Add `roadmap_complete` skip gate to `_maybe_ideate` (TB-174 sibling for axis-4 walk-away halt)

## Goal

Current focus: end-to-end automation. TB-226 wired the axis-4
dispatch + auto-approve gates to honor `goal.roadmap_exhausted(cfg)`:
when the pointer advances past the last focus and the operator
hasn't acked the `roadmap_complete` halt, the dispatch path blocks
Backlog promote (`daemon.py:3946`) and the auto-approve gate refuses
(`tools.py:1970`). TB-242 + TB-244 added pull + push surface
coverage so the halt is visible to the operator on return. But
`_maybe_ideate` (`ap2/ideation.py:786-846`) does NOT honor
`roadmap_exhausted` — the TB-174 focus-exhausted gate (L825-845)
covers only the ideator's `ideation_state.md` self-reported
`exhausted-needs-operator` signal, not the daemon-side axis-4 halt.
`grep "roadmap_exhausted\|roadmap_complete" ap2/ideation.py` returns
zero matches.

Result: when the roadmap exhausts during a walk-away weekend (axis 4
fires `roadmap_complete`, dispatch + auto-approve halt as designed),
ideation keeps firing every cooldown window. Each firing burns one
SDK call generating proposals that pile up as `@blocked:review` in
Backlog because the dispatch and auto-approve gates both honor
`roadmap_exhausted`. The operator returns to a pile of speculative
proposals against an already-exhausted roadmap — exactly the
walk-away clean-up burden goal.md's done-when bullet 3 ("stops
proposing when the target project's `## Done when` criteria are all
met") was meant to eliminate.

Why now: TB-244 (`aa971f8`, 2026-05-17T00:09Z) just closed the
push-surface half of axis-4 visibility and TB-245 closes the
validator-judge push surface; axis-4 surface parity across pull +
push is now complete. The next walk-away gap on axis 4 is no longer
observability — it's ideation cost discipline during the halt
itself. Concrete cost: 60-min cooldown × 48h weekend = up to 48
wasted ideation SDK calls plus an unbounded backlog clutter the
operator must triage on return. Exact TB-174-shape transplant: same
skip-with-event-emit + `mark_run` pattern, different gate (daemon-
side `goal.roadmap_exhausted` vs. ideator self-report).

## Scope

(1) Add a `roadmap_exhausted` gate to `_maybe_ideate` in
    `ap2/ideation.py`, placed AFTER the slots check (TB-183/TB-186
    at L802-824) and BEFORE the TB-174 focus-exhausted gate
    (L825-845). When `goal.roadmap_exhausted(cfg)` returns True,
    emit `events.append(cfg.events_file, "ideation_skipped",
    reason="roadmap_complete")`, call `mark_run(cfg.cron_state_file,
    IDEATION_NAME)`, then return.

(2) Update `force_ideate` (`ap2/ideation.py:849-...`) docstring to
    note that it bypasses this new gate alongside the TB-174 gate
    (the operator may force ideation after extending goal.md but
    before the next tick has updated the pointer; force must still
    work).

(3) Add a focused test module
    `ap2/tests/test_tb246_ideation_roadmap_complete_gate.py`
    pinning the new gate: assert ideation skips with
    `reason=roadmap_complete` when `goal.roadmap_exhausted(cfg)`
    returns True; assert `mark_run` advances the cooldown so the
    next 30s tick doesn't re-evaluate; assert `force_ideate`
    bypasses the gate even when `roadmap_exhausted` is True; assert
    the new gate does NOT fire when `roadmap_exhausted` is False
    (regression pin against over-application). Mirror TB-174's
    `test_maybe_ideate_skips_when_all_focus_exhausted` shape at
    `ap2/tests/test_ideation_trigger.py:733`.

(4) Cross-reference the new gate in `ap2/howto.md` ideation
    skip-gate enumeration (the existing TB-174 reference is the
    natural insertion point — add a parallel paragraph naming the
    canonical predicate and the bypass behavior in `force_ideate`).

## Design

Mirror TB-174's gate shape exactly — that's the proven worked
example of an ideation skip-gate in this codebase, was
operator-approved, ships cleanly, and the two gates differ only in
their trigger condition:

- **Gate trigger** (`ideation.py`): `goal.roadmap_exhausted(cfg)`
  is the canonical predicate (already used by the dispatch path at
  `daemon.py:3946` and the auto-approve gate at `tools.py:1970`).
  No new state file, no new parsing logic — pure reuse via local
  import (mirror the import pattern at `daemon.py:3946`).

- **Event emission**: `events.append(cfg.events_file,
  "ideation_skipped", reason="roadmap_complete")`. Reuses the
  existing `ideation_skipped` event type that TB-174 already
  emits with `reason=focus_exhausted` — downstream consumers
  (ideation prompt's events block via TB-169's
  `IDEATION_RELEVANT_EVENT_TYPES` allowlist) see a uniform
  `ideation_skipped` shape with a structured `reason` field.

- **Cooldown advancement**: `mark_run(cfg.cron_state_file,
  IDEATION_NAME)` after the skip event, matching TB-174's
  pattern. Without `mark_run`, the daemon's 30s tick would
  re-evaluate the gate every loop until the operator acks the
  halt — bursty events.jsonl noise for no signal gain.

- **Placement**: AFTER the slots check, BEFORE the focus-exhausted
  gate. Two reasons: (a) `goal.roadmap_exhausted` is a cheap dict
  load + bounded event-tail scan; the focus-exhausted gate parses
  `ideation_state.md` (file I/O + regex). Cheaper gate first.
  (b) the daemon-side roadmap halt is the formal axis-4 state and
  supersedes the ideator's self-report — when the daemon has
  formally declared the roadmap exhausted, the ideator's prior
  self-report is moot.

- **`force_ideate` bypass**: mirror TB-174's bypass pattern
  (`force_ideate` already documents bypassing every other gate
  including TB-174's). The operator's recovery path after
  extending goal.md is `ap2 ack roadmap_complete && ap2 update-goal
  && ap2 ideate --force`. The force path must work even if the ack
  hasn't propagated to the pointer state yet.

- **Tests**
  (`ap2/tests/test_tb246_ideation_roadmap_complete_gate.py`):
  modelled byte-for-byte on
  `test_maybe_ideate_skips_when_all_focus_exhausted`
  (`test_ideation_trigger.py:733`). Covers: gate fires + emits
  `ideation_skipped reason=roadmap_complete` when
  `goal.roadmap_exhausted` returns True; `mark_run` advances the
  cron state file; `force_ideate` bypasses the gate even when
  `roadmap_exhausted` is True; the new gate does NOT fire when
  `roadmap_exhausted` is False (regression pin); the gate's event
  emission precedes the `mark_run` call (ordering pin so a
  reader inspecting `events.jsonl` always sees the skip event
  with a fresh cron-state timestamp).

- **Howto cross-reference**: extend the existing TB-174 paragraph
  in `ap2/howto.md` with one sentence pointing at the new sibling
  gate. No separate top-level section — the two are adjacent in
  the skip-gate enumeration.

Anti-shape avoided: do NOT collapse the focus-exhausted and
roadmap-complete gates into a single check. They're semantically
distinct (ideator self-report vs. daemon-side halt), trigger on
different state files, and the ideator-self-report path needs to
keep working in single-focus projects with no pointer state.

Anti-shape avoided: do NOT add a new event type
(`ideation_skipped_roadmap_complete`). Reusing `ideation_skipped`
with a `reason` field is what TB-174 established and what the
existing TB-169 allowlist already pipes into the ideation prompt
events block.

## Verification

- `uv run pytest -q ap2/tests/` — full suite green vs current baseline.
- `uv run pytest -q ap2/tests/test_tb246_ideation_roadmap_complete_gate.py` — new test module exists and all behavioral cases pass.
- `test -f ap2/tests/test_tb246_ideation_roadmap_complete_gate.py` — new test module present on disk.
- `grep -nE "roadmap_exhausted" ap2/ideation.py` — new gate references the canonical predicate (at minimum one match).
- `grep -nE "roadmap_complete" ap2/ideation.py` — new event reason string emitted (at minimum one match).
- `grep -nE "roadmap_complete|roadmap_exhausted" ap2/howto.md` — howto.md ideation skip-gate enumeration extended to name the new gate.
- `uv run pytest -q ap2/tests/test_ideation_trigger.py` — TB-174 focus-exhausted gate regression tests still pass (proves the new gate doesn't break the older one).
- Prose: `ap2/ideation.py` Prose: `_maybe_ideate` calls `goal.roadmap_exhausted(cfg)` before the SDK invocation; when True, emits `events.append(cfg.events_file, "ideation_skipped", reason="roadmap_complete")` and calls `mark_run(cfg.cron_state_file, IDEATION_NAME)` then returns. The gate sits AFTER the slots check (TB-183/TB-186) and BEFORE the TB-174 focus-exhausted gate.
- Prose: `ap2/ideation.py` Prose: `force_ideate` docstring updated to enumerate the new `roadmap_complete` gate alongside the existing TB-174 focus-exhausted gate in the list of bypassed checks; the function body still calls `_run_ideation` unconditionally without consulting `goal.roadmap_exhausted`.

## Out of scope

- Doctor-side warning when `roadmap_exhausted` returns True and `AP2_AUTO_APPROVE=1` — `ap2 status` already surfaces the halt state on the pull surface (TB-242) and the cron digest carries `roadmap_complete` on the push surface (TB-244); doctor would re-render existing signals. Revisit if walk-away monitoring evidence shows a pre-flight warning need.
- Auto-acknowledgement of the `roadmap_complete` halt — operator-only by goal.md non-goal "Goal.md auto-rotation" (L186-191): operator owns the focus list extension.
- Combining the focus-exhausted and roadmap-complete signals into a single ideation gate — different semantic levels (ideator self-report vs. daemon-side halt); collapsing them loses the regression pin on the older TB-174 path and conflates two distinct project states (single-focus exhausted vs. multi-focus roadmap end).
- Changes to `_status_report_should_skip` or `_STATUS_REPORT_AUTOMATION_INTERESTING_TYPES` — `ideation_skipped` is already excluded from the report's interesting types (only halt + completion events drive the digest); no new wiring needed there.
- Symmetric ideation gate for `auto_approve_paused` (axis-3 consecutive-freezes halt) — different cost profile (minutes-recoverable vs. days-to-weeks); deferred pending evidence the halt durations justify gating ideation cost.
## Attempts

### 2026-05-17 — verification_failed
(no summary)
- **kind:** project_wide
- **verify_command:** uv run pytest -q ap2/tests/
- **exit_code:** None
- **stderr_tail:** 
- **Debug dumps:** `prompt: .cc-autopilot/debug/20260517T072635Z-TB-246.prompt.md`, `stream: .cc-autopilot/debug/20260517T072635Z-TB-246.stream.jsonl`, `messages: .cc-autopilot/debug/20260517T072635Z-TB-246.messages.jsonl`
### 2026-05-17 — verification_failed
(no summary)
- **kind:** project_wide
- **verify_command:** uv run pytest -q ap2/tests/
- **exit_code:** None
- **stderr_tail:** 
- **Debug dumps:** `prompt: .cc-autopilot/debug/20260517T103131Z-TB-246.prompt.md`, `stream: .cc-autopilot/debug/20260517T103131Z-TB-246.stream.jsonl`, `messages: .cc-autopilot/debug/20260517T103131Z-TB-246.messages.jsonl`

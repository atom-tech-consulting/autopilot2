# Axis 4 foundation — goal.md focus-list parser, pointer state, advance heuristic, `roadmap_complete` halt

## Goal

Deliver axis 4 of `Current focus: end-to-end automation` (goal.md
L115-138). Today goal.md carries a single `## Current focus:` heading;
the daemon parses it as a free-text block and never rotates. Per the
goal-stated design, the operator should be able to list multiple
`## Current focus:` headings in priority order (top = active), and the
daemon should advance its in-memory pointer to the next focus when the
topmost is exhausted — either by an explicit `Done when:` sub-block's
criteria being substantively met or, when no `Done when:` is present,
by an `AP2_FOCUS_ADVANCE_EMPTY_CYCLES` (default 3) heuristic. When all
foci exhaust, emit a `roadmap_complete` decisions-needed entry and
halt auto-promotion until the operator extends the roadmap and acks
via `ap2 ack roadmap_complete`. The daemon NEVER mutates goal.md
itself (goal.md L187-191 "Goal.md auto-rotation" Non-goal).

Why now: without focus rotation, walk-away time is bounded by the
topmost focus's natural exhaustion point — when one focus's gaps are
addressed, ideation has nothing valuable to propose until the operator
manually rotates `goal.md`, forcing intervention at exactly the moment
the loop should be most productive. Axes 1-3 just shipped (TB-223 /
TB-224 / TB-225); the focus arc is currently sized for hours of
walk-away, not weeks. Axis 4 is the gating delivery on Done-when-1's
"walk away for a week without intervention" promise scaling from
days-to-weeks to weeks-to-months.

## Scope

(1) Parser in `ap2/goal.py` (new module if it doesn't exist;
otherwise add to existing): `parse_focus_list(goal_md_text: str) ->
list[FocusItem]` where `FocusItem` is a dataclass with `title`,
`body`, `done_when_bullets: list[str] | None`, `line_range:
tuple[int,int]`. Iterates all `## Current focus:` headings in order;
captures each focus's body block; for each, optionally captures a
nested `### Done when` or inline `Done when:` sub-block whose bullets
are explicit completion criteria. Empty list when no focus headings
exist (today's pre-pivot test fixtures).

(2) Pointer state file `.cc-autopilot/focus_pointer.json` —
gitignored, fenced from task agents (add to
`TASK_AGENT_FENCED_PATHS`). Schema: `{"active_index": int,
"active_title": str, "empty_cycles": int, "exhausted_titles":
[str], "roadmap_complete_ack_idx": int | null}`. Loaded under
`locked_inplace`. Default-emit when missing: index 0.

(3) Three env knobs (all `ap2/_shared.py`-style parse + default +
clamp):
  - `AP2_FOCUS_ADVANCE_EMPTY_CYCLES` (default `3`, min `1`, max
    `20`) — controls the heuristic-fallback advance threshold for
    foci without an explicit `Done when:` sub-block.
  - `AP2_FOCUS_AUTO_ADVANCE_DISABLED` (default unset) — kill-switch:
    when `1`, the daemon never auto-advances even if criteria are
    met; emits a decisions-needed entry instead.
  - `AP2_FOCUS_DONE_WHEN_JUDGE_EFFORT` (default `medium`) — the
    LLM-judge effort knob when ideation evaluates whether a focus's
    `Done when:` bullets are substantively met (mirrors
    `AP2_JANITOR_JUDGE_EFFORT` pattern).

(4) Daemon-side advance pass in `daemon._tick` step 0.6 (after
TB-225's step 0.5 auto-unfreeze, before step 1 board-promote):
`_maybe_advance_focus(cfg)`. Reads goal.md focus list + current
pointer. If active focus has `done_when_bullets`, invokes a
short ideation-judge SDK call (similar shape to `verify._judge_prose_
bullet` but goal-aligned: "Given recent completes + ideation_state,
are the Done-when bullets substantively met?") to decide; otherwise
falls back to empty-cycles heuristic (increment `empty_cycles` on
each `ideation_skipped_no_slots` / 0-proposal ideation_complete event
against the active focus; advance when ≥ threshold). On advance,
emit `focus_advanced from=<old_title> to=<new_title>
trigger={done_when_judge|empty_cycles_heuristic}` event and update
pointer. When all foci are exhausted, emit `roadmap_complete` event
+ decisions-needed entry + halt auto-promotion (block dispatch path,
mirroring `_auto_approve_paused` shape).

(5) `ap2 ack roadmap_complete` operator verb (queue-routed via
`do_operator_queue_append`, mirrors TB-224's
`auto_approve_window_resume` ack shape). Strips the halt; daemon
resumes on next tick. Pointer state records the ack timestamp.

(6) Tests in new `ap2/tests/test_tb226_focus_rotation.py`:
parser happy + error paths (zero / one / three focus headings;
malformed `Done when:` sub-block; embedded code fences); env-knob
parse defaults + override + invalid-value fallback for all three
knobs; pointer state load/save round-trip; advance via empty-cycles
heuristic; advance via Done-when judge (stub the SDK call); halt
on all-exhausted with `roadmap_complete` event + decisions-needed
entry; ack-resume restores dispatch path; `AP2_FOCUS_AUTO_ADVANCE_
DISABLED=1` short-circuits even when criteria are met.

(7) Howto + architecture docs: new section in `ap2/howto.md` —
`### Focus rotation (axis 4)` — names the three env knobs, the
pointer file path, the `roadmap_complete` halt contract, the ack
verb. Update `ap2/architecture.md` event-types reference with the
two new event types (`focus_advanced`, `roadmap_complete`).

(8) Coverage drift: register `focus_advanced` + `roadmap_complete`
in `events.py`'s event-type registry so `test_coverage_drift.py`
sees them. Register the three env knobs in the env-knob registry
(per TB-208 + TB-210 patterns). The TB-226 test file covers all
five via direct refs.

## Design

- `goal.py` (new): parser module so `daemon.py` doesn't grow more
  goal.md-walking responsibility. Pure-function `parse_focus_list`
  + `parse_done_when` (line-based scan, not full Markdown AST — the
  schema is shallow enough that regex-on-heading-lines is adequate
  and avoids pulling mistune into the daemon path). Test fixtures
  live in `ap2/tests/_goal_fixtures.py`.
- Pointer state file pattern follows TB-201's
  `operator_log_state.json` shape: tiny JSON, locked via
  `locked_inplace`, schema-versioned via `schema: 1` field.
- `_maybe_advance_focus` is gated on `daemon._tick`'s normal
  housekeeping cadence — no separate scheduler. Runs once per tick;
  cheap (one file read + at most one SDK call when Done-when judging).
- `roadmap_complete` halt re-uses TB-223's `_auto_approve_paused`-style
  flag check: dispatch path consults `focus_pointer.json`'s
  `roadmap_complete_ack_idx` against the current `len(focus_list)`;
  when index < total count, dispatch proceeds; when equal AND no
  ack recorded, halt with operator-decision surfacing.
- The Done-when judge call is structured: pass the focus's
  `done_when_bullets` + the last ~10 completes' titles + summaries
  + recent ideation_state Mission-alignment summary, ask "Are these
  bullets substantively met?" with a yes/no/insufficient-evidence
  output and a one-sentence rationale. Effort knob bounds cost.

## Verification

- `uv run pytest -q ap2/tests/test_tb226_focus_rotation.py` — new
  test module exists and all behavioral cases pass (parser, knobs,
  pointer round-trip, advance paths, halt + ack, kill-switch).
- `uv run pytest -q ap2/tests/` — full suite green (no regression
  vs current 1421 baseline).
- `test -f ap2/goal.py` — new parser module landed at the named
  path.
- `test -f ap2/tests/test_tb226_focus_rotation.py` — test module
  present.
- `grep -nE "^def parse_focus_list" ap2/goal.py` — public parser
  symbol exposed.
- `grep -nE "AP2_FOCUS_ADVANCE_EMPTY_CYCLES|AP2_FOCUS_AUTO_ADVANCE_DISABLED|AP2_FOCUS_DONE_WHEN_JUDGE_EFFORT" ap2/daemon.py ap2/goal.py` — at least one match per knob across daemon/goal modules.
- `grep -nE "focus_advanced|roadmap_complete" ap2/events.py` — both
  new event types registered in the event-type registry.
- `grep -nE "^### Focus rotation" ap2/howto.md` — howto section
  landed.
- Prose: `daemon._tick` calls `_maybe_advance_focus(cfg)` in step
  0.6 (after step 0.5 `_maybe_auto_unfreeze`, before step 1 board
  promote); judge confirms via Read of `ap2/daemon.py`.
- Prose: `ap2 ack roadmap_complete` is a routed operator-queue verb
  whose drain handler updates `focus_pointer.json` and clears the
  halt; judge confirms via Read of `ap2/tools.py` and
  `ap2/cli.py`.

## Out of scope

- Goal.md auto-mutation (goal.md L187-191 Non-goal) — pointer is
  in-memory state only; adding / reordering / retiring foci stays
  operator-CLI-only via `ap2 update-goal`.
- A web-UI visualization of focus list + pointer position. This
  task's surface is daemon + CLI + howto. Web rendering can ride
  TB-227's status-visibility work or land later.
- Backfilling existing focus headings retroactively. Today's
  goal.md has one focus heading — that's the post-pivot active
  one; pointer starts at index 0 against the live file.

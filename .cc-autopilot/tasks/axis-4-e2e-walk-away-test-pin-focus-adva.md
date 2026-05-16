# TB-237 — Axis-4 e2e walk-away test: pin `focus_advanced` + `roadmap_complete` event chain in concert across daemon `_tick` cycles

## Goal

Current focus: end-to-end automation — TB-230 (`ad1ae3e`) shipped
the axes 1+2 in-concert e2e test (`ap2/tests/e2e/test_walk_away_
loop.py`) and explicitly deferred axis-4 to a sibling task (its
`## Out of scope` line 107-110: "Axis-4 focus-advance e2e
(`focus_advanced` + `roadmap_complete` event chain) — multi-cycle
ideation accumulator state pushes the test wall-clock beyond this
task's scope; defer to a sibling task"). Axis-4 today has only
unit-level coverage in `ap2/tests/test_tb226_focus_rotation.py`
(`test_roadmap_complete_event_on_exhaustion` at line 586 +
`test_ack_clears_roadmap_complete_halt` at line 623); no test
drives daemon `_tick` cycles through a two-focus `goal.md` to
exercise the full advance-pointer-then-halt sequence in concert
with the rest of the loop.

This task adds the missing axis-4 e2e: a two-focus `goal.md`
fixture, FakeSDK ideation that returns 0 proposals for each focus
in sequence, and `AP2_FOCUS_ADVANCE_EMPTY_CYCLES` set low enough
to bound the test wall-clock. Asserts the `focus_advanced
from=<a> to=<b>` event lands after N empty cycles on focus A,
then `roadmap_complete` lands after N empty cycles on focus B,
and the daemon halts auto-promote per goal.md L130
("emits a `roadmap_complete` decisions-needed entry and halts
auto-approval until the operator extends the roadmap").

Why now: TB-230 landed at `ad1ae3e` on 2026-05-16T01:13:25Z with
the axis-4 deferral explicit; axes 1+2 now have e2e coverage but
axis 4 does not, leaving the walk-away promise (goal.md L131-138:
"walk-away time scales with the operator-declared roadmap length
weeks-to-months") unverified in concert. Without this test, an
operator who configures a multi-focus `goal.md` cannot trust that
exhaustion-rotation-halt actually fires end-to-end — every
existing axis-4 test exercises ONE helper at a time, not the
sequence under real `_tick` dispatch.

## Scope

(1) Add `test_focus_advance_and_roadmap_complete_across_ticks`
to `ap2/tests/e2e/test_walk_away_loop.py` (extend the existing
module rather than a parallel file; matches the TB-230 location).

(2) Test setup:
  - `tmp_path`-rooted `project_root` with a `goal.md` carrying
    two `## Current focus:` headings ("focus-a" then "focus-b"),
    each with an explicit `Done when:` sub-block listing one
    trivially-met criterion so the heuristic empty-cycles path
    is what drives advancement (not Done-when satisfaction).
  - `monkeypatch.setenv("AP2_FOCUS_ADVANCE_EMPTY_CYCLES", "2")`
    to bound the test to 4 ideation invocations total
    (2 empty on focus-a → advance → 2 empty on focus-b → halt).
  - FakeSDK ideation returns a `mcp__autopilot__ideation_state_
    write` payload + zero `add_backlog` board_edit calls,
    simulating "ideation can't find proposals" on each
    invocation; reuse the FakeSDK pattern from TB-230's
    `test_auto_approve_dispatches_ideation_proposal_without_
    operator`.

(3) Test assertions:
  - After 2 ideation runs with 0 proposals on focus-a, a
    `focus_advanced` event fires with `from=focus-a to=focus-b`
    payload.
  - After 2 more ideation runs with 0 proposals on focus-b, a
    `roadmap_complete` event fires.
  - The `roadmap_complete` halt blocks auto-promote: any
    subsequent `_tick` that would have dispatched an auto-
    approved task does not (assert no `task_start` events
    appear after `roadmap_complete` until an
    `auto_approve_window_resume` ack-equivalent fires, mirroring
    `test_ack_clears_roadmap_complete_halt`'s expectation).
  - Event ordering: sort events.jsonl by index, verify
    `focus_advanced` strictly precedes `roadmap_complete`,
    and `roadmap_complete` strictly precedes any halt-clear
    event.

(4) Helper reuse: if a fixture (multi-focus `goal.md` builder,
empty-cycle ideation FakeSDK) is needed in more than one test,
factor it into `ap2/tests/e2e/conftest.py` rather than
duplicating. Match TB-230's convention.

## Design

Test isolation: each test gets a clean `events.jsonl` +
`tmp_path` project root. Env-knob set/unset via
`monkeypatch.setenv` / `monkeypatch.delenv` for automatic
cleanup. FakeSDK reuses the existing per-axis pattern; no new
SDK shape required.

No production-code changes — this task is test-only. If a
focus-rotation bug surfaces during implementation (e.g., the
empty-cycles counter doesn't reset on advance, the
`roadmap_complete` halt doesn't actually block auto-promote
across ticks), file a follow-up rather than fixing in this
task. The point is to PIN the contract; bugs surfaced by the
pin are separate work.

Wall-clock bound: with `AP2_FOCUS_ADVANCE_EMPTY_CYCLES=2`, the
test drives 4 ideation invocations + a few interleaved ticks.
FakeSDK keeps each ideation invocation sub-second; total test
runtime target <10s to match TB-230's e2e budget.

## Verification

- `uv run pytest -q ap2/tests/e2e/test_walk_away_loop.py::test_focus_advance_and_roadmap_complete_across_ticks` — new test passes.
- `uv run pytest -q ap2/tests/e2e/test_walk_away_loop.py` — full e2e walk-away module green (TB-230 tests still pass alongside the new one).
- `uv run pytest -q ap2/tests/` — full suite green vs current baseline.
- `test -f ap2/tests/e2e/test_walk_away_loop.py` — module present on disk (extended, not replaced).
- `grep -nE "def test_focus_advance_and_roadmap_complete_across_ticks" ap2/tests/e2e/test_walk_away_loop.py` — new test function declared.
- `grep -nE "focus_advanced" ap2/tests/e2e/test_walk_away_loop.py` — event-type assertion on `focus_advanced`.
- `grep -nE "roadmap_complete" ap2/tests/e2e/test_walk_away_loop.py` — event-type assertion on `roadmap_complete`.
- `grep -nE "AP2_FOCUS_ADVANCE_EMPTY_CYCLES" ap2/tests/e2e/test_walk_away_loop.py` — empty-cycles knob exercised.
- `grep -cE "## Current focus:" ap2/tests/e2e/test_walk_away_loop.py` — multi-focus fixture present (two-focus goal.md).
- Prose: `ap2/tests/e2e/test_walk_away_loop.py` Prose: the new test drives `daemon._tick` (not just helper functions in isolation) across enough cycles to exercise focus-a-exhaust → `focus_advanced` → focus-b-exhaust → `roadmap_complete` in sequence; judge confirms by reading the new test body and checking the `_tick` invocation count is ≥4.

## Out of scope

- Live-SDK smoke (replaces FakeSDK with real `sdk.query`) —
  cost-prohibitive and orthogonal to in-concert wiring
  validation.
- Production-code changes to the focus-rotation / advance /
  halt paths — test-only. Any bug surfaced is a follow-up.
- Web-UI surface for `focus_advanced` / `roadmap_complete`
  events — defer to a separate cycle; the operator's primary
  return channel (status-report cron, ap2 status CLI) already
  carries the events via `decisions_needed` paths.
- Axis-3 e2e (`task_error` halt across ticks) — has unit
  coverage via `test_tb224_token_caps.py::test_task_error_
  single_event_halts_auto_promote`; not a TB-230 deferral.

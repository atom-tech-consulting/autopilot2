# TB-230 — End-to-end walk-away integration test pinning auto-approve dispatch + auto-unfreeze BriefingFix in concert (axes 1+2)

## Goal

Current focus: end-to-end automation — the four axes (TB-223/224
auto-approve + token caps, TB-225/229 auto-unfreeze BriefingFix,
TB-226 focus-rotation, TB-227/228 surfaces) shipped foundations
on 2026-05-14/15 but have only been exercised in isolation. Every
existing test (`test_tb223_auto_approve.py`, `test_tb225_auto_
unfreeze.py`, `test_tb226_focus_rotation.py`, `test_tb228_status_
report_automation_digest.py`) pins ONE axis at a time. The
dispatch path `ideation → auto-approve → task-run → verify →
complete` with `AP2_AUTO_APPROVE=1` has zero end-to-end coverage,
and the auto-unfreeze sweep's full `BriefingFix:` parse → patch
apply → re-dispatch path has zero end-to-end coverage. This task
adds two focused integration tests that put the daemon's `_tick`
through one full walk-away cycle each: (1) auto-approve dispatches
an ideation-queued proposal without operator action; (2) auto-
unfreeze repairs a Frozen task whose blocked summary names a
trusted fix-shape.

Why now: TB-223/224/225/226/229 closed the per-axis foundations
and the burst of 4 task completes at 2026-05-15T17:30-18:45Z just
drained the board. Without in-concert e2e coverage, the operator
can't risk flipping `AP2_AUTO_APPROVE=1` (events.jsonl shows zero
`auto_approved` events in the recent tail, confirming the feature
is in HEAD but never deployed). The walk-away promise stays
aspirational until the loop is pinned working in concert.

## Scope

(1) New `ap2/tests/e2e/test_walk_away_loop.py` with two tests:

  - `test_auto_approve_dispatches_ideation_proposal_without_operator`:
    Starts from an empty board with `AP2_AUTO_APPROVE=1` set;
    FakeSDK stubs ideation to queue one canonical-valid briefing
    via `do_board_edit(action="add_backlog", blocked_on="review")`;
    runs `daemon._tick` enough cycles to let the auto-approve
    branch fire; asserts (a) the proposal lands with the
    `@blocked:review` codespan on first tick, (b) the next tick
    auto-strips the review token (matching `do_board_edit`'s
    `_approve_review_token` helper), (c) `auto_approved` event
    fires with `task=TB-N` and a `knob` field, (d) the task
    auto-promotes to Active without any `operator_queue_append`
    event with `op="approve"` in events.jsonl.

  - `test_auto_unfreeze_briefingfix_repairs_frozen_task`:
    Seeds a Frozen TB-N whose latest `task_complete` event has
    `status="blocked"` and a summary containing
    `BriefingFix: \`bare-line\` => \`fixed-line\`` that matches
    a default entry in `AP2_AUTO_UNFREEZE_FIX_SHAPES`; the
    briefing file on disk contains the matching `bare-line`;
    runs `_maybe_auto_unfreeze` sweep; asserts (a) the briefing
    file now contains `fixed-line` and not `bare-line`,
    (b) `auto_unfreeze_applied` event lands with `task` and a
    `fix_shape` field, (c) the task moves out of Frozen.

(2) Both tests use the existing FakeSDK + env-knob monkeypatch
pattern from `ap2/tests/test_tb223_auto_approve.py` and
`ap2/tests/test_tb225_auto_unfreeze.py`; if a new helper is
required, factor it into `ap2/tests/e2e/conftest.py` rather than
adding a parallel module.

(3) Verify causal ordering on the first test: the events
`ideation_complete` → `auto_approved` → `task_start` →
`task_complete` for the same `task=TB-N` MUST appear in
increasing-index order in events.jsonl (sort-by-event-index
assertion).

## Design

Test isolation: each test gets a `tmp_path`-rooted `project_root`
+ clean `events.jsonl`. Reuse the e2e fixtures from
`ap2/tests/e2e/conftest.py`. Env-knob set/unset via
`monkeypatch.setenv` / `monkeypatch.delenv` so cleanup is
automatic across the test session.

FakeSDK shape: extend (don't replace) the existing FakeSDK in
the per-axis tests. For the ideation step, the FakeSDK call
returns one `mcp__autopilot__board_edit` tool-call payload that
queues a canonical-valid briefing (use the
`canonical_valid_briefing()` helper from
`ap2/tests/_briefing_fixture.py` that TB-204 extracted). For
the task-dispatch step, the FakeSDK returns a no-op assistant
message + a `task_complete` MCP call so the verifier path runs
against an empty implementation diff (gate the test on event-
ordering, not on real diff content).

No production-code changes required — this task is test-only.
If a verifier-path bug surfaces during implementation, file a
follow-up rather than fixing in this task.

## Verification

- `uv run pytest -q ap2/tests/e2e/test_walk_away_loop.py` — new test module exists and both behavioral cases pass.
- `uv run pytest -q ap2/tests/` — full suite green vs current baseline.
- `test -f ap2/tests/e2e/test_walk_away_loop.py` — test module present on disk.
- `grep -nE "def test_auto_approve_dispatches_ideation_proposal_without_operator" ap2/tests/e2e/test_walk_away_loop.py` — first test function declared.
- `grep -nE "def test_auto_unfreeze_briefingfix_repairs_frozen_task" ap2/tests/e2e/test_walk_away_loop.py` — second test function declared.
- `grep -nE "AP2_AUTO_APPROVE" ap2/tests/e2e/test_walk_away_loop.py` — auto-approve knob exercised in the tests.
- `grep -nE "BriefingFix:" ap2/tests/e2e/test_walk_away_loop.py` — briefing-fix path exercised in the tests.
- `grep -nE "auto_approved|auto_unfreeze_applied" ap2/tests/e2e/test_walk_away_loop.py` — both event types asserted on.
- Prose: `ap2/tests/e2e/test_walk_away_loop.py` Prose: the tests drive `daemon._tick` with `AP2_AUTO_APPROVE=1` set (not just the helper functions in isolation) so the auto-approve event-ordering assertion reflects the real dispatch path; judge confirms by reading the test bodies in the new file.

## Out of scope

- Axis-4 focus-advance e2e (`focus_advanced` + `roadmap_complete`
  event chain) — multi-cycle ideation accumulator state pushes
  the test wall-clock beyond this task's scope; defer to a
  sibling task.
- Real-SDK smoke (replaces FakeSDK with a live `sdk.query` call)
  — cost-prohibitive and orthogonal to the in-concert wiring
  validation this task is for.
- Production-code changes to the auto-approve / auto-unfreeze
  paths — this is test-only. Any bug surfaced during
  implementation is a follow-up task.

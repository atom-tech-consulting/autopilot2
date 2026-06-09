"""TB-230: end-to-end walk-away integration tests for axes 1+2 in concert.

Background — the four "end-to-end automation" axes (TB-223/224 auto-approve
+ token caps, TB-225/229 auto-unfreeze BriefingFix, TB-226 focus-rotation,
TB-227/228 surfaces) shipped per-axis foundations on 2026-05-14/15 but only
have isolated test coverage today. Each existing test
(`test_tb223_auto_approve.py`, `test_tb225_auto_unfreeze.py`,
`test_tb226_focus_rotation.py`, `test_tb228_status_report_automation_digest.py`)
pins ONE axis at a time. This module pins the loop end-to-end through
`daemon._tick` so the operator can trust the walk-away promise without
reading every per-axis test to confirm the wiring lines up.

Two tests, one per axis dispatch path:

  - `test_auto_approve_dispatches_ideation_proposal_without_operator`:
    Empty board, `AP2_AUTO_APPROVE=1`, FakeSDK ideation responder queues
    one canonical-valid briefing via `do_board_edit(action="add_backlog",
    blocked_on="review", ...)`. Drives two `_tick` cycles and asserts the
    full `ideation → auto-approve → task-run → verify → complete` chain
    landed without any operator-queue `op="approve"` event ever firing.

  - `test_auto_unfreeze_briefingfix_repairs_frozen_task`:
    Frozen task + `task_complete status=blocked` carrying a structured
    `BriefingFix:` summary that matches an `AP2_AUTO_UNFREEZE_FIX_SHAPES`
    allowlist entry. Drives `_tick` twice (tick 1 sweep queues update +
    unfreeze; tick 2 drain applies them) and asserts the briefing file
    now contains the fixed line and the task moved off Frozen.

The TB-225 e2e test already walks the two-tick sweep+drain shape for the
auto-unfreeze path; this module adds the missing in-concert pin where
`AP2_AUTO_APPROVE=1` is ALSO set so a future refactor that breaks one
axis's behavior under the other axis's env-knobs trips here.

Per the briefing: production-code changes are out of scope. If a wiring
bug surfaces during implementation, file a follow-up rather than fixing
in this task.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from ap2 import daemon, events, goal, tools
from ap2.board import Board
from ap2.config import Config
from ap2.cron import save_state
from ap2.daemon import _tick
from ap2.init import init_project
from ap2.tests._briefing_fixtures import canonical_briefing
from ap2.tests.e2e._fakes import (
    FakeSDK,
    _FakeMixedMsg,
    _FakeToolUseBlock,
    tool_call_respond,
)


# ---------------------------------------------------------------------------
# Shared goal.md anchoring both tests to the project's `## Current focus`
# heading. Ideation- and operator-queue-authored briefings cite this anchor
# in their `## Goal` body via `canonical_briefing(..., goal_anchor=...)` so
# `_validate_briefing_structure`'s TB-161 goal-anchor check passes cleanly.
# Matches the shape `test_tb223_auto_approve.py::_GOAL_MD` uses.
# ---------------------------------------------------------------------------

_GOAL_MD = (
    "# Project Goals\n\n"
    "## Mission\n\n"
    "Drive the project toward end-to-end automation.\n\n"
    "## Done when\n\n"
    "- An operator can point ap2 at a fresh project and walk away "
    "without intervention.\n\n"
    "## Current focus: end-to-end automation\n\n"
    "Close the manual-approval bottleneck plus failure-recovery gaps.\n\n"
    "## Non-goals\n\n"
    "- something out of scope.\n"
)

# `_goal_md_anchors` derives anchors as the FULL normalized `## Current
# focus:` heading title — `_normalize_anchor("Current focus: end-to-end
# automation")` = `"current focus end to end automation"`. The briefing's
# `## Goal` body must contain this anchor as a substring after the same
# normalization, so we cite the full heading verbatim (not just the
# `end-to-end automation` tail) when constructing briefings.
_GOAL_ANCHOR = "current focus: end-to-end automation"


# ---------------------------------------------------------------------------
# Briefing for the auto-unfreeze test. The `## Verification` section carries
# a patchable `grep -lE` bullet that the `grep_missing_r_on_dir` fix-shape
# rewrites to `grep -rlE`. We DON'T reuse `canonical_briefing()` for this
# case because we need the patched line at a deterministic location in the
# briefing file (the `BriefingFix:` payload names `<path>:<line>`).
# ---------------------------------------------------------------------------

_UNFREEZE_BRIEFING = (
    "# TB-230 fixture briefing\n\n"
    "## Goal\n\n"
    f"Closes the failure mode the briefing scope names; advances goal.md's "
    f"{_GOAL_ANCHOR}.\n\n"
    "Why now: closes the failure-recovery dependency — without this, every "
    "briefing-shape regression cascades into operator-manual unfreeze and "
    "the walk-away envelope contracts.\n\n"
    "## Scope\n\n"
    "- ap2/daemon.py\n\n"
    "## Design\n\n"
    "Direct edit.\n\n"
    "## Verification\n"
    "- `grep -lE 'pattern' ap2/tests/` — matches at least one file.\n\n"
    "## Out of scope\n\n"
    "- nothing\n"
)


# ---------------------------------------------------------------------------
# Local project fixture. The shared `e2e_project` factory in
# `ap2/tests/e2e/conftest.py` doesn't write a goal.md (the simple cron /
# task tests don't need one) — we layer that on here so the briefing-
# structure validator's TB-161 goal-anchor check has anchors to match
# against. `init_project` seeds the autopilot dir + a placeholder
# goal.md; we overwrite the placeholder with `_GOAL_MD`.
# ---------------------------------------------------------------------------

@pytest.fixture
def walk_away_cfg(tmp_path: Path, monkeypatch) -> Config:
    """Project root with the standard ap2 init layout + a real goal.md +
    env scrubbed of the cron-tick noise the per-axis tests rely on."""
    # Same env scrub as e2e_project — mattermost, verify, watchdog off.
    for k in (
        "AP2_MM_CHANNELS",
        "MATTERMOST_URL",
        "MATTERMOST_TOKEN",
        "AP2_MM_BOT_USER_ID",
        "AP2_MM_MENTION",
        "AP2_VERIFY_CMD",
        "AP2_VERIFY_TIMEOUT_S",
        "AP2_AUTO_DIAGNOSE_IDLE_THRESHOLD_S",
        "AP2_AUTO_DIAGNOSE_COOLDOWN_S",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AP2_TASK_TIMEOUT_S", "30")
    monkeypatch.setenv("AP2_CONTROL_TIMEOUT_S", "30")
    monkeypatch.setenv("AP2_MAX_RETRIES", "3")

    init_project(tmp_path)
    (tmp_path / "goal.md").write_text(_GOAL_MD)
    # Pre-set the next task ID so the test can predict the TB-N the
    # ideation/operator-queue path will allocate.
    (tmp_path / "CLAUDE.md").write_text(
        "## Autopilot\n\n"
        "- Task list: `TASKS.md`\n"
        "- Next task ID: TB-10\n"
    )
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()
    return cfg


def _unwrap(res: dict) -> dict:
    assert not res.get("isError"), res
    return json.loads(res["content"][0]["text"])


# ===========================================================================
# Test 1 — auto-approve dispatches an ideation-queued proposal without any
# operator action. Walks the full chain through TWO `_tick` cycles:
#   - tick 1: empty board → step-4 ideation fires → FakeSDK ideation
#     responder calls `do_board_edit(add_backlog, blocked_on="review", ...)`
#     → because `AP2_AUTO_APPROVE=1`, the `review` token is stripped
#     synchronously inside `do_board_edit` AND an `auto_approved` event
#     fires (TB-223 behavior). Task lands in Backlog dispatchable.
#   - tick 2: step-3 backlog auto-promote moves the task to Ready and
#     dispatches it → FakeSDK task responder calls
#     `report_result(status="complete", ...)` → daemon routes the task
#     to Complete and emits `task_complete`.
#
# Asserts the dispatch path landed without any `operator_queue_append`
# event with `op="approve"` (no operator action) and that the canonical
# event sequence appears in events.jsonl in increasing index order:
# `ideation_empty_board` → `auto_approved` → `task_start` →
# `task_complete`.
# ===========================================================================


def test_auto_approve_dispatches_ideation_proposal_without_operator(
    walk_away_cfg: Config, monkeypatch,
):
    cfg = walk_away_cfg

    # Knob set + freeze threshold zeroed out so the circuit-breaker
    # doesn't accidentally fire on this clean run (no prior failure
    # window exists, but explicit is safer than implicit).
    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")
    monkeypatch.setenv("AP2_AUTO_APPROVE_FREEZE_THRESHOLD", "0")
    # Enable ideation + short cooldown so step-4 fires on tick 1.
    monkeypatch.delenv("AP2_IDEATION_DISABLED", raising=False)
    monkeypatch.setenv("AP2_IDEATION_COOLDOWN_S", "3600")
    # Project-override ideation prompt — gives the FakeSDK a stable
    # substring to match on rather than depending on `ideation.default.md`.
    override = cfg.project_root / ".cc-autopilot" / "ideation_prompt.md"
    override.write_text("TB-230 walk-away ideation prompt — propose a task.\n")
    # Bookkeeping: stale the ideation cooldown clock so `_maybe_ideate`
    # actually fires on tick 1.
    save_state(cfg.cron_state_file, {"ideation": time.time() - 7200})

    # FakeSDK with two responders: (1) ideation step, (2) task dispatch.
    # The expected TB-N is TB-10 (CLAUDE.md preallocates to that mark in
    # the walk_away_cfg fixture).
    expected_tb = "TB-10"
    # Verification bullet uses `true` (an unconditional-success shell
    # builtin) so the per-task verifier passes against an empty diff —
    # the dispatched FakeSDK task responder commits nothing on disk, so
    # the default `uv run pytest -q` canonical bullet would exit 5 (no
    # tests collected in tmp_path) and trip `verification_failed`. The
    # walk-away pin is about the dispatch wiring, not the per-task
    # verifier (TB-122 / TB-132 already pin that surface separately).
    briefing_text = canonical_briefing(
        expected_tb,
        title="auto-approved walk-away task",
        goal_anchor=_GOAL_ANCHOR,
        verification="- `true` — sanity check that always passes\n",
    )

    def ideation_factory(prompt, options):  # noqa: ARG001
        """Drive the synchronous `do_board_edit` path the real ideation
        agent would have taken via its MCP tool call. FakeSDK doesn't
        route MCP calls, so the responder calls `tools.do_board_edit`
        directly under the same Config the daemon passed to ideation.
        Also emits an `ideation_complete` event the way the real ideation
        agent does (`log_event` MCP call) so the causal-ordering pin has
        a deterministic anchor."""

        async def _gen():
            tools.do_board_edit(
                cfg,
                {
                    "action": "add_backlog",
                    "title": "auto-approved walk-away task",
                    "blocked_on": "review",
                    "briefing": briefing_text,
                    "tags": ["#autopilot"],
                },
            )
            tools.do_log_event(
                cfg,
                {
                    "type": "ideation_complete",
                    "summary": (
                        f"queued one proposal: {expected_tb} "
                        f"(walk-away test fixture)"
                    ),
                },
            )
            # Yield one assistant-shape text message so the SDK consumer
            # observes a non-empty stream (matches the real ideation
            # agent's prompt-acknowledgment text).
            yield _FakeMixedMsg([
                _FakeToolUseBlock(
                    name="log_event",
                    input={"type": "ideation_complete", "summary": "done"},
                ),
            ])

        return _gen()

    sdk = FakeSDK()
    sdk.on("TB-230 walk-away ideation prompt", ideation_factory)
    sdk.on(
        f"## Task\n{expected_tb}",
        tool_call_respond(
            "report_result",
            {
                "status": "complete",
                "commit": "abc12345",
                "summary": "did the walk-away work",
                "files_changed": "foo.py",
                "tests_passed": "true",
            },
        ),
    )

    # Tick 1: ideation runs, queues the auto-approved task into Backlog.
    asyncio.run(_tick(cfg, sdk, mcp_server=None))

    board = Board.load(cfg.tasks_file)
    loc = board.find(expected_tb)
    assert loc is not None, (
        f"tick 1: expected {expected_tb} on the board after ideation; "
        f"sections={list(board.sections.keys())}"
    )
    section, idx = loc
    assert section == "Backlog", (
        f"tick 1: expected {expected_tb} in Backlog (auto-approved row "
        f"awaits next-tick auto-promote); got section={section}"
    )
    row = board.sections[section][idx]
    # TB-383: `board_edit` is policy-free, so ideation's proposal is born
    # `@blocked:review` on tick 1 (ideation runs at the END of the tick,
    # AFTER the PRE_DISPATCH loop pass). No `auto_approved` event has fired
    # yet — the loop pass strips the token on tick 2, before that tick's
    # dispatch stage. This preserves the pre-TB-383 DISPATCH timing (the
    # proposal still dispatches on tick 2) while moving the strip out of
    # the mid-agent-run mutation.
    assert "@blocked:review" in row, (
        f"tick 1: policy-free `board_edit` must leave `@blocked:review` on "
        f"the ideation-queued row (the loop pass strips it next tick); "
        f"got: {row!r}"
    )

    evts_tick1 = events.tail(cfg.events_file, 200)
    assert [e for e in evts_tick1 if e.get("type") == "auto_approved"] == [], (
        "tick 1: no `auto_approved` event fires at proposal time "
        "(policy-free `board_edit`); the loop pass emits it on tick 2"
    )

    # Tick 2: the PRE_DISPATCH loop pass strips `@blocked:review` + emits
    # `auto_approved`; the auto-promote step then dispatches the task; the
    # FakeSDK task responder calls report_result(status=complete) → Complete.
    asyncio.run(_tick(cfg, sdk, mcp_server=None))

    auto_evts = [
        e for e in events.tail(cfg.events_file, 300)
        if e.get("type") == "auto_approved" and e.get("task") == expected_tb
    ]
    assert len(auto_evts) == 1, (
        f"tick 2: exactly one `auto_approved` event must fire for "
        f"{expected_tb} (from the loop pass); got: {auto_evts}"
    )
    # The knob field captures the env value at strip time — pins the
    # TB-223 payload contract (unchanged across the TB-383 relocation).
    assert auto_evts[0]["knob"] == "1"

    board = Board.load(cfg.tasks_file)
    loc = board.find(expected_tb)
    assert loc is not None and loc[0] == "Complete", (
        f"tick 2: auto-approved task must land in Complete after dispatch; "
        f"got section={loc[0] if loc else 'missing'}"
    )

    # Negative pin: NO operator-driven approve op fired. The whole point
    # of auto-approve is that the operator never had to ack anything; if
    # a regression sneaks an `operator_queue_append op=approve` into the
    # auto path, this assertion catches it.
    evts = events.tail(cfg.events_file, 500)
    approve_ops = [
        e for e in evts
        if e.get("type") == "operator_queue_append"
        and e.get("op") == "approve"
    ]
    assert approve_ops == [], (
        "no `operator_queue_append op=approve` events should fire in the "
        f"auto-approve path; got: {approve_ops}"
    )

    # Causal ordering pin: `ideation_empty_board` (daemon-emitted entry
    # marker) → `auto_approved` → `task_start` → `task_complete` must
    # appear in increasing-index order in events.jsonl for the same
    # TB-N. Uses event-index (oldest-first order) to be robust against
    # timestamp resolution / clock skew.
    expected_chain = (
        "ideation_empty_board",
        "auto_approved",
        "task_solve",
        "task_complete",
    )
    indices: dict[str, int] = {}
    for i, e in enumerate(evts):
        typ = e.get("type")
        if typ not in expected_chain:
            continue
        # For task-scoped events, only count the ones matching our TB-N
        # (the ideation_empty_board entry marker has no `task` field, so
        # it's matched globally — fine: there's only one ideation cycle
        # in this test).
        task_field = e.get("task")
        if task_field and task_field != expected_tb:
            continue
        # First occurrence wins (matches the "increasing index order"
        # rule: we want the FIRST appearance of each event).
        indices.setdefault(typ, i)
    missing = [t for t in expected_chain if t not in indices]
    assert not missing, (
        f"causal chain missing event types {missing}; saw indices={indices}"
    )
    ordered = [indices[t] for t in expected_chain]
    assert ordered == sorted(ordered), (
        f"events out of causal order: {list(zip(expected_chain, ordered))}"
    )

    # Final pin: the task_complete event carries the responder's payload
    # verbatim (commit, status) — confirms the dispatch round-tripped
    # through `report_result`, not a HEAD-recovery fallback.
    end = next(e for e in reversed(evts) if e.get("type") == "task_complete")
    assert end["task"] == expected_tb
    assert end["status"] == "complete"
    assert end["commit"] == "abc12345"


# ===========================================================================
# Test 2 — auto-unfreeze BriefingFix repairs a Frozen task and re-dispatches
# it. In concert with `AP2_AUTO_APPROVE=1` so a future refactor that breaks
# the auto-unfreeze path under the auto-approve env-knobs trips here (the
# per-axis TB-225 test doesn't set `AP2_AUTO_APPROVE`).
#
# Two `_tick` cycles:
#   - tick 1: step-0 drain (no-op) → step-0.5 auto-unfreeze sweep parses
#     the `BriefingFix:` summary, applies the patch in-memory, queues
#     `update` + `unfreeze` ops on the operator queue. Briefing file is
#     NOT yet patched on disk; task is still Frozen.
#   - tick 2: step-0 drain applies the queued ops → briefing file is
#     patched on disk, task moves Frozen → Backlog.
#
# Asserts:
#   - briefing file now contains `grep -rlE` and no longer contains
#     `grep -lE` on the patched line.
#   - `auto_unfreeze_applied` event fired with `task=<TB-N>` and the
#     fix-shape recorded.
#   - task moved out of Frozen (to Backlog).
# ===========================================================================


def test_auto_unfreeze_briefingfix_repairs_frozen_task(
    walk_away_cfg: Config, monkeypatch,
):
    cfg = walk_away_cfg

    # Allowlist: only the grep_missing_r_on_dir shape (matches the
    # _UNFREEZE_BRIEFING content). Auto-approve is also ON to pin the
    # in-concert behavior — TB-225 doesn't exercise this combination.
    monkeypatch.setenv(
        "AP2_AUTO_UNFREEZE_FIX_SHAPES", "grep_missing_r_on_dir",
    )
    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")
    monkeypatch.setenv("AP2_AUTO_APPROVE_FREEZE_THRESHOLD", "0")
    # Suppress ideation — this test is focused on the auto-unfreeze path.
    monkeypatch.setenv("AP2_IDEATION_DISABLED", "1")

    # Add a Backlog task via the operator queue (the same path the
    # operator's `ap2 add` takes) so the briefing materializes on disk
    # at the canonical location the daemon's auto-unfreeze sweep will
    # later look it up at.
    res = tools.do_operator_queue_append(
        cfg,
        {
            "op": "add_backlog",
            "title": "tb-230 auto-unfreeze fixture",
            "briefing": _UNFREEZE_BRIEFING,
        },
    )
    info = _unwrap(res)
    task_id = info["task_id"]

    # Drain the queued add so the row + briefing land on disk.
    tools.drain_operator_queue(cfg)
    board = Board.load(cfg.tasks_file)
    task = board.get(task_id)
    assert task is not None and task.briefing, (
        f"fixture: {task_id} has no briefing path after add_backlog drain"
    )
    briefing_path = cfg.project_root / task.briefing
    assert briefing_path.exists(), f"briefing not on disk: {briefing_path}"

    # Move directly to Frozen via the synchronous board-edit seam (the
    # operator queue doesn't expose Backlog→Frozen; only `retry_exhausted`
    # does, which we simulate here without a real failed task agent run).
    tools.do_board_edit(
        cfg, {"action": "move_to_frozen", "task_id": task_id},
    )

    # Emit a `task_complete status=blocked` event whose summary carries
    # the structured `BriefingFix:` prefix the parser consumes. Find the
    # 1-indexed line number of the patchable bullet so the patch payload
    # passes the briefing-line-literal-match guard.
    rel = str(briefing_path.relative_to(cfg.project_root))
    lines = briefing_path.read_text().splitlines()
    grep_line_idx = next(
        i for i, line in enumerate(lines) if "grep -lE" in line
    )
    summary = (
        "Agent self-diagnosis: the Verification grep bullet returns nothing "
        "without `-r` on a directory target.\n"
        f"BriefingFix: grep_missing_r_on_dir at {rel}:{grep_line_idx + 1}: "
        f"grep -lE -> grep -rlE\n"
        "Recommend re-dispatch after the patch lands."
    )
    events.append(
        cfg.events_file,
        "task_complete",
        task=task_id,
        status="blocked",
        commit="",
        summary=summary,
    )

    # FakeSDK: no scripts registered. If `_tick` ever tries to dispatch
    # a task (which it shouldn't — the only board task is Frozen) the
    # SDK falls through to the default empty stream and the task path
    # would HEAD-recover; the assertions below would then fail loudly,
    # which is the right behavior.
    sdk = FakeSDK()

    # Tick 1: drain (no pending ops); auto-unfreeze sweep parses the
    # BriefingFix and queues update + unfreeze. Briefing file not yet
    # patched; task still Frozen until tick 2 drain.
    asyncio.run(_tick(cfg, sdk, mcp_server=None))
    board = Board.load(cfg.tasks_file)
    loc = board.find(task_id)
    assert loc is not None and loc[0] == "Frozen", (
        f"tick 1: sweep queues ops; task is still Frozen until drain on "
        f"tick 2; got section={loc[0] if loc else 'missing'}"
    )
    evts = events.tail(cfg.events_file, 400)
    applied = [
        e for e in evts
        if e.get("type") == "auto_unfreeze_applied"
        and e.get("task") == task_id
    ]
    assert len(applied) == 1, (
        f"tick 1: exactly one `auto_unfreeze_applied` event must fire for "
        f"{task_id}; got: {applied}"
    )
    # The applied event records the fix-shape (briefing's "fix_shape"
    # field requirement — the event uses `shape` per TB-225's payload
    # contract; we pin both task + shape to lock the audit-trail shape).
    assert applied[0]["shape"] == "grep_missing_r_on_dir"
    assert applied[0]["from"] == "grep -lE"
    assert applied[0]["to"] == "grep -rlE"

    # Tick 2: drain applies the queued update + unfreeze.
    asyncio.run(_tick(cfg, sdk, mcp_server=None))
    board = Board.load(cfg.tasks_file)
    loc = board.find(task_id)
    assert loc is not None, f"{task_id} disappeared after tick 2"
    assert loc[0] != "Frozen", (
        f"tick 2: drain must have moved {task_id} off Frozen; got "
        f"section={loc[0]}"
    )

    # Briefing file: the patched bullet now uses `grep -rlE`, and the
    # original `grep -lE` is gone (replacement is scoped to the named
    # line, but the whole briefing only had one occurrence).
    patched_text = briefing_path.read_text()
    assert "grep -rlE" in patched_text, (
        f"tick 2 drain must have patched the briefing to `grep -rlE`; "
        f"got:\n{patched_text}"
    )
    assert "grep -lE 'pattern'" not in patched_text, (
        f"the original `grep -lE 'pattern'` form must be replaced on the "
        f"patched line; got:\n{patched_text}"
    )

    # Drain-side events landed: task_unfrozen + task_updated for the
    # patched briefing.
    drained = events.tail(cfg.events_file, 600)
    assert any(
        e.get("type") == "task_unfrozen" and e.get("task") == task_id
        for e in drained
    ), "drain must emit task_unfrozen"
    assert any(
        e.get("type") == "task_updated" and e.get("task") == task_id
        for e in drained
    ), "drain must emit task_updated for the briefing patch"


# ===========================================================================
# Test 3 (TB-237, collapsed TB-342) — axis-4 ideation-exhaustion halt
# end-to-end across daemon `_tick` cycles.
#
# Sibling to test 1/2 above. TB-230's `## Out of scope` explicitly deferred
# axis-4 e2e (multi-cycle ideation accumulator pushed the wall-clock beyond
# TB-230's scope); this test closes that gap so the walk-away promise
# (goal.md L131-138: "walk-away time scales with the operator-declared
# roadmap length") is verified end-to-end under real `_tick` dispatch.
#
# Setup: two-focus goal.md (focus-a, focus-b) with no `Progress signals:`
# block on either. FakeSDK ideation returns 0 proposals on every
# invocation, simulating "ideation can't find substantive proposals"
# each cycle.
#
# Empty-cycles threshold is set to 2 via `AP2_FOCUS_ADVANCE_EMPTY_CYCLES`.
# Per the TB-292 cycle-grouped heuristic, each ideation invocation that
# exits via `ideation_complete` with NO `ideation_proposal_recorded`
# inside the cycle increments the counter by exactly 1. With threshold=2,
# TWO ideation invocations are enough to trip the halt on the next tick
# (the detector pass runs at step 0.6 of `_tick`, BEFORE step 4's
# `_maybe_ideate`; the events from tick N's ideation are visible to tick
# N+1's detector pass).
#
# Tick-by-tick trace (asserted below; TB-342 collapse — no more
# focus-by-focus pointer walk; the global counter trips the halt
# directly once it crosses the threshold):
#   - Tick 1: detector sees 0 empty cycles → no halt; ideation runs
#     (count=1).
#   - Tick 2: detector sees 1 empty cycle → no halt; ideation runs
#     (count=2).
#   - Tick 3: detector sees 2 empty cycles ≥ threshold → emits
#     `roadmap_complete`; ideation runs but the next tick's gate will
#     park it.
#
# After tick 3: assert `roadmap_complete` landed exactly once,
# the ideation gate is active (`goal.roadmap_exhausted(cfg) is True`),
# no `task_start` events fired (vacuously true here since no Backlog
# task was seeded; TB-275 made dispatch UN-gated by roadmap state, so
# the only thing roadmap_complete now blocks is the ideation trigger),
# and (TB-340) an `operator_ack` with the `roadmap_complete` token
# does NOT clear the gate. Resume is editing goal.md via
# `ap2 update-goal` — simulated here by calling
# `goal.reset_pointer_on_goal_updated` directly. The TB-275
# regression-pin `test_dispatch_promotes_when_roadmap_exhausted` in
# `test_tb226_focus_rotation.py` asserts the inverse — that a
# dispatchable Backlog task DOES auto-promote under roadmap_complete.
# ===========================================================================


# Two-focus goal.md fixture. NEITHER focus carries a `Progress signals:`
# sub-block (rendered name post-TB-285 of the prior `Done when:` block).
# Post-TB-283 the empty-cycles heuristic runs regardless of the
# sub-block's presence — the prior LLM-judge advance path that read
# operator-authored bullets was deleted because it ruled on commit
# diffs rather than substantive progress. Omitting the sub-block here
# is purely a minimal-fixture choice; the advancement behavior is the
# same with or without it.
_MULTI_FOCUS_GOAL_MD = (
    "# Project Goals\n\n"
    "## Mission\n\n"
    "Drive the project toward end-to-end automation.\n\n"
    "## Done when\n\n"
    "- An operator can point ap2 at a fresh project and walk away "
    "without intervention.\n\n"
    "## Current focus: focus-a\n\n"
    "Body for focus-a — first focus in the two-step roadmap.\n\n"
    "## Current focus: focus-b\n\n"
    "Body for focus-b — second (and final) focus in the roadmap.\n\n"
    "## Non-goals\n\n"
    "- something out of scope.\n"
)


def test_focus_advance_and_roadmap_complete_across_ticks(
    walk_away_cfg: Config, monkeypatch,
):
    cfg = walk_away_cfg

    # Override the walk_away_cfg's single-focus goal.md with a two-focus
    # one. The fixture's preallocated `Next task ID: TB-10` is irrelevant
    # here (no tasks are ever queued — FakeSDK ideation returns 0
    # proposals on every invocation).
    (cfg.project_root / "goal.md").write_text(_MULTI_FOCUS_GOAL_MD)

    # Bound empty-cycles to 2 so two ideation invocations are enough to
    # trip the halt on the following tick. Each invocation emits entry
    # (`ideation_empty_board`) + exit (`ideation_complete`) with no
    # proposal inside, forming one full empty cycle (+1 to the counter
    # under TB-292's cycle-grouped semantics).
    monkeypatch.setenv("AP2_FOCUS_ADVANCE_EMPTY_CYCLES", "2")
    # Kill-switch off (default) so the detector pass is allowed to fire.
    monkeypatch.delenv("AP2_FOCUS_AUTO_ADVANCE_DISABLED", raising=False)
    # Enable ideation; cooldown=0 so it fires every tick.
    monkeypatch.delenv("AP2_IDEATION_DISABLED", raising=False)
    monkeypatch.setenv("AP2_IDEATION_COOLDOWN_S", "0")
    # Project-override ideation prompt — stable substring for FakeSDK
    # routing.
    override = cfg.project_root / ".cc-autopilot" / "ideation_prompt.md"
    override.write_text("TB-237 walk-away ideation prompt — propose a task.\n")
    # Stale the ideation cooldown clock so the first tick's gate passes
    # cleanly.
    save_state(cfg.cron_state_file, {"ideation": time.time() - 7200})

    # FakeSDK ideation responder: returns 0 proposals on every call.
    def ideation_factory(prompt, options):  # noqa: ARG001
        async def _gen():
            tools.do_log_event(
                cfg,
                {
                    "type": "ideation_complete",
                    "summary": (
                        "no proposals; ideation reports the goal "
                        "appears exhausted (test fixture)"
                    ),
                },
            )
            yield _FakeMixedMsg([
                _FakeToolUseBlock(
                    name="log_event",
                    input={
                        "type": "ideation_complete",
                        "summary": "no proposals",
                    },
                ),
            ])

        return _gen()

    sdk = FakeSDK()
    sdk.on("TB-237 walk-away ideation prompt", ideation_factory)

    # Drive 3 ticks through `daemon._tick`. TB-342: the collapsed
    # detector emits `roadmap_complete` after `threshold` empty cycles
    # directly, instead of walking the pointer through each focus first.
    for _ in range(3):
        asyncio.run(_tick(cfg, sdk, mcp_server=None))

    evts = events.tail(cfg.events_file, 1000)

    # ----- roadmap_complete fired exactly once -----
    rc = [e for e in evts if e.get("type") == "roadmap_complete"]
    assert len(rc) == 1, (
        f"expected exactly one `roadmap_complete` event; got: {rc}"
    )
    assert rc[0]["exhausted_count"] == 2
    assert rc[0]["trigger"] == "empty_cycles_heuristic"

    # TB-342: no `focus_advanced` events fire — the rotation pointer
    # walk is gone.
    fa = [e for e in evts if e.get("type") == "focus_advanced"]
    assert fa == [], (
        f"TB-342: `focus_advanced` events must NOT fire — the rotation "
        f"pointer walk was collapsed; got: {fa}"
    )

    idx_rc = next(
        i for i, e in enumerate(evts)
        if e.get("type") == "roadmap_complete"
    )

    # ----- Ideation gate active. -----
    assert goal.roadmap_exhausted(cfg) is True, (
        "roadmap_complete event fired but `goal.roadmap_exhausted` returns "
        "False — the gate is detached from the event emit"
    )

    # ----- No `task_start` events appear after `roadmap_complete`. -----
    task_starts_after_halt = [
        e for e in evts[idx_rc + 1:]
        if e.get("type") == "task_solve"
    ]
    assert task_starts_after_halt == [], (
        f"no `task_start` events should fire after `roadmap_complete` "
        f"in THIS fixture (no Backlog tasks seeded); "
        f"got: {task_starts_after_halt}"
    )

    # ----- TB-340: ack does NOT clear the gate. -----
    events.append(
        cfg.events_file,
        "operator_ack",
        note="ack: roadmap_complete — dismissing the notice",
    )
    assert goal.roadmap_exhausted(cfg) is True, (
        "TB-340: the ack must NOT clear the gate — `roadmap_exhausted` "
        "is the `roadmap_complete_emitted` flag and the detector has "
        "set it"
    )

    # ----- Resume is editing goal.md (TB-342 collapsed `rewind-focus`
    # away with the rotation pointer walk): simulate the `update_goal`
    # drain handler's reset call directly. -----
    foci = goal.read_focus_list(cfg)
    resumed = goal.reset_pointer_on_goal_updated(cfg, foci)
    goal.save_pointer(cfg, resumed)
    assert goal.roadmap_exhausted(cfg) is False, (
        "editing goal.md (update_goal → reset_pointer_on_goal_updated) "
        "must clear the gate — `goal.roadmap_exhausted` still returns "
        "True"
    )

    # ----- Final ordering pin: `roadmap_complete` strictly precedes the
    # `operator_ack`. -----
    evts_after_ack = events.tail(cfg.events_file, 1000)
    idx_rc2 = next(
        i for i, e in enumerate(evts_after_ack)
        if e.get("type") == "roadmap_complete"
    )
    idx_ack = next(
        i for i, e in enumerate(evts_after_ack)
        if e.get("type") == "operator_ack"
        and "roadmap_complete" in str(e.get("note") or "")
    )
    assert idx_rc2 < idx_ack, (
        f"roadmap_complete (idx {idx_rc2}) must precede the "
        f"operator_ack (idx {idx_ack})"
    )

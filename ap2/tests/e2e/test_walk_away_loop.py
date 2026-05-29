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
    # AP2_AUTO_APPROVE=1 strips the @blocked:review codespan synchronously
    # in `do_board_edit`. After tick 1 the row is dispatchable.
    assert "@blocked:review" not in row, (
        f"tick 1: AP2_AUTO_APPROVE must strip `@blocked:review` from the "
        f"ideation-queued row; got: {row!r}"
    )

    evts_tick1 = events.tail(cfg.events_file, 200)
    auto_evts = [e for e in evts_tick1 if e.get("type") == "auto_approved"]
    assert len(auto_evts) == 1, (
        f"tick 1: exactly one `auto_approved` event must fire; got: {auto_evts}"
    )
    assert auto_evts[0]["task"] == expected_tb
    # The knob field captures the env value at proposal time — pins the
    # TB-223 payload contract.
    assert auto_evts[0]["knob"] == "1"

    # Tick 2: backlog auto-promote dispatches the task; the FakeSDK task
    # responder calls report_result(status=complete) → Complete.
    asyncio.run(_tick(cfg, sdk, mcp_server=None))

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
        "task_start",
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
# Test 3 (TB-237) — axis-4 focus rotation: focus_advanced + roadmap_complete
# event chain in concert across daemon `_tick` cycles.
#
# Sibling to test 1/2 above. TB-230's `## Out of scope` explicitly deferred
# axis-4 e2e (multi-cycle ideation accumulator pushed the wall-clock beyond
# TB-230's scope); this test closes that gap so the walk-away promise
# (goal.md L131-138: "walk-away time scales with the operator-declared
# roadmap length") is verified end-to-end under real `_tick` dispatch.
#
# Setup: two-focus goal.md (focus-a, focus-b) with no `Progress signals:`
# block on either — though post-TB-283 the empty-cycles heuristic runs
# regardless of sub-block presence (the prior LLM-judge advance path
# was deleted; TB-285 renamed the sub-block to reflect the new advisory
# semantics). FakeSDK ideation returns 0 proposals on every invocation
# (an `ideation_complete` event with no `add_backlog` call), simulating
# "ideation can't find proposals against the active focus" each cycle.
#
# Empty-cycles threshold is set to 2 via `AP2_FOCUS_ADVANCE_EMPTY_CYCLES`.
# Per the TB-292 cycle-grouped heuristic in
# `_ideation_empty_against_focus`, each ideation invocation that exits
# via `ideation_complete` with NO `ideation_proposal_recorded` inside
# the cycle increments the counter by exactly 1 (entry + exit form one
# cycle; pre-TB-292 the counter naively summed both event types and
# bumped by 2 per cycle — the bug this restructure closed). With
# threshold=2, TWO ideation invocations per focus are needed before
# the advance pass trips on the NEXT tick (the advance pass runs at
# step 0.6 of `_tick`, BEFORE step 4's `_maybe_ideate`; the events
# from tick N's ideation are visible to tick N+1's advance pass).
#
# Tick-by-tick trace (asserted below):
#   - Tick 1: advance pass sees 0 empty cycles → no advance; ideation runs
#     against focus-a (1 empty cycle, count=1).
#   - Tick 2: advance pass sees 1 empty cycle → no advance; ideation runs
#     against focus-a again (count=2).
#   - Tick 3: advance pass sees 2 empty cycles against focus-a → ADVANCE
#     focus-a → focus-b; ideation runs against focus-b (1 empty cycle
#     past the cutoff; count for focus-b = 1).
#   - Tick 4: advance pass sees 1 empty cycle for focus-b → no advance;
#     ideation runs (count for focus-b = 2).
#   - Tick 5: advance pass sees 2 empty cycles against focus-b → ADVANCE
#     focus-b → "" (pointer past last); ideation runs (no-op for the
#     pointer since it's already past last).
#   - Tick 6: advance pass sees `active_idx >= len(foci)` → emits
#     `roadmap_complete`; ideation runs.
#
# After tick 4: assert `focus_advanced` precedes `roadmap_complete`,
# the ideation gate is active (`goal.roadmap_exhausted(cfg) is True`),
# no `task_start` events fired (vacuously true here since no Backlog
# task was seeded; TB-275 made dispatch UN-gated by roadmap state, so
# the only thing roadmap_complete now blocks is the ideation trigger),
# and (TB-340) an `operator_ack` with the `roadmap_complete` token
# does NOT clear the gate — the gate is a pure pointer predicate, so
# resuming is a pointer move (here we simulate `rewind-focus` /
# `update-goal` by re-pointing `active_index` back in range). The
# TB-275 regression-pin
# `test_dispatch_promotes_when_roadmap_exhausted` in
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

    # Bound empty-cycles to 2 so two ideation invocations per focus are
    # enough to trip the advance pass on the following tick. Each
    # invocation emits entry (`ideation_empty_board`) + exit
    # (`ideation_complete`) with no proposal inside, forming one full
    # empty cycle (+1 to the counter under TB-292's cycle-grouped
    # semantics; pre-TB-292 the same shape naively bumped by +2).
    monkeypatch.setenv("AP2_FOCUS_ADVANCE_EMPTY_CYCLES", "2")
    # Kill-switch off (default) so the advance pass is allowed to fire.
    monkeypatch.delenv("AP2_FOCUS_AUTO_ADVANCE_DISABLED", raising=False)
    # Enable ideation; cooldown=0 so it fires every tick (default 7200s
    # would suppress ideation on ticks 2-4).
    monkeypatch.delenv("AP2_IDEATION_DISABLED", raising=False)
    monkeypatch.setenv("AP2_IDEATION_COOLDOWN_S", "0")
    # Project-override ideation prompt — stable substring for FakeSDK
    # routing (no need to depend on `ideation.default.md`).
    override = cfg.project_root / ".cc-autopilot" / "ideation_prompt.md"
    override.write_text("TB-237 walk-away ideation prompt — propose a task.\n")
    # Stale the ideation cooldown clock so the first tick's gate passes
    # cleanly (defensive — cooldown=0 already passes, but explicit is
    # safer in case the gate-ordering changes upstream).
    save_state(cfg.cron_state_file, {"ideation": time.time() - 7200})

    # FakeSDK ideation responder: returns 0 proposals on every call. Just
    # emits an `ideation_complete` event via the same `tools.do_log_event`
    # path the real ideation agent's `log_event` MCP tool uses. NO
    # `do_board_edit(add_backlog, ...)` call, so no
    # `ideation_proposal_recorded` event ever fires (which would reset the
    # empty-cycles counter — see `_ideation_empty_against_focus`).
    def ideation_factory(prompt, options):  # noqa: ARG001
        async def _gen():
            tools.do_log_event(
                cfg,
                {
                    "type": "ideation_complete",
                    "summary": (
                        "no proposals; ideation reports the active focus "
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

    # Drive 6 ticks through `daemon._tick`. Each tick runs the focus-
    # advance pass (step 0.6) and then ideation (step 4). The trace
    # above this function explains the expected event sequence.
    # TB-292: bumped from 4 → 6 ticks to match the cycle-grouped
    # counter (two empty cycles per advance instead of the pre-TB-292
    # double-count's one).
    for _ in range(6):
        asyncio.run(_tick(cfg, sdk, mcp_server=None))

    evts = events.tail(cfg.events_file, 1000)

    # ----- focus_advanced focus-a → focus-b -----
    fa_a_to_b = [
        e for e in evts
        if e.get("type") == "focus_advanced"
        and e.get("from") == "focus-a"
        and e.get("to") == "focus-b"
    ]
    assert len(fa_a_to_b) == 1, (
        f"expected exactly one `focus_advanced from=focus-a to=focus-b` "
        f"event; got: {fa_a_to_b}"
    )
    assert fa_a_to_b[0]["trigger"] == "empty_cycles_heuristic", (
        f"focus-a → focus-b advance must fire via the heuristic path; "
        f"got trigger={fa_a_to_b[0].get('trigger')!r}"
    )
    assert fa_a_to_b[0]["new_index"] == 1
    assert fa_a_to_b[0]["total_foci"] == 2

    # ----- focus_advanced focus-b → "" (pointer past last) -----
    # The advance from focus-b pushes the pointer past the last focus
    # but does NOT itself emit `roadmap_complete` — the next tick's
    # advance pass detects `active_idx >= len(foci)` and emits the
    # halt event.
    fa_b_to_end = [
        e for e in evts
        if e.get("type") == "focus_advanced"
        and e.get("from") == "focus-b"
    ]
    assert len(fa_b_to_end) == 1, (
        f"expected exactly one `focus_advanced from=focus-b` event; "
        f"got: {fa_b_to_end}"
    )
    assert fa_b_to_end[0]["to"] == ""
    assert fa_b_to_end[0]["new_index"] == 2

    # ----- roadmap_complete fired exactly once -----
    rc = [e for e in evts if e.get("type") == "roadmap_complete"]
    assert len(rc) == 1, (
        f"expected exactly one `roadmap_complete` event; got: {rc}"
    )
    assert rc[0]["exhausted_count"] == 2
    assert rc[0]["trigger"] == "pointer_past_last"

    # ----- Causal ordering: focus_advanced (focus-a → focus-b) strictly
    # precedes focus_advanced (focus-b → "") strictly precedes
    # roadmap_complete. Walk events.jsonl in index order. -----
    idx_fa_a = next(
        i for i, e in enumerate(evts)
        if e.get("type") == "focus_advanced"
        and e.get("from") == "focus-a"
    )
    idx_fa_b = next(
        i for i, e in enumerate(evts)
        if e.get("type") == "focus_advanced"
        and e.get("from") == "focus-b"
    )
    idx_rc = next(
        i for i, e in enumerate(evts)
        if e.get("type") == "roadmap_complete"
    )
    assert idx_fa_a < idx_fa_b < idx_rc, (
        f"event ordering wrong: focus-a→focus-b at {idx_fa_a}, "
        f"focus-b→\"\" at {idx_fa_b}, roadmap_complete at {idx_rc}"
    )

    # ----- Ideation gate active: pointer past last + no clearing ack yet. -----
    assert goal.roadmap_exhausted(cfg) is True, (
        "roadmap_complete event fired but `goal.roadmap_exhausted` returns "
        "False — the gate scan is detached from the event emit"
    )

    # ----- No `task_start` events appear after `roadmap_complete`.
    # Vacuously true here since no Backlog tasks are seeded (FakeSDK
    # ideation returns 0 proposals). TB-275 NOTE: post-fix this is a
    # weak invariant — dispatch is no longer gated by roadmap state, so
    # under a seeded Backlog row we WOULD expect `task_start` to fire
    # after `roadmap_complete` (that's the entire point of the fix).
    # The TB-275 regression-pin
    # `test_dispatch_promotes_when_roadmap_exhausted` in
    # `test_tb226_focus_rotation.py` exercises the un-gated dispatch
    # against a real dispatchable Backlog row. -----
    task_starts_after_halt = [
        e for e in evts[idx_rc + 1:]
        if e.get("type") == "task_start"
    ]
    assert task_starts_after_halt == [], (
        f"no `task_start` events should fire after `roadmap_complete` "
        f"in THIS fixture (no Backlog tasks seeded); "
        f"got: {task_starts_after_halt}"
    )

    # ----- TB-340: ack does NOT clear the gate. The operator emits
    # `operator_ack` with the `roadmap_complete` token in the note
    # (same shape `test_ack_does_not_clear_roadmap_complete_gate` in
    # `test_tb226_focus_rotation.py` exercises). After the ack,
    # `goal.roadmap_exhausted(cfg)` STAYS True — the gate is a pure
    # pointer predicate; the ack only dismisses the operator nag. -----
    events.append(
        cfg.events_file,
        "operator_ack",
        note="ack: roadmap_complete — dismissing the notice",
    )
    assert goal.roadmap_exhausted(cfg) is True, (
        "TB-340: the ack must NOT clear the gate — `roadmap_exhausted` "
        "is a pure pointer predicate and the pointer is still past the "
        "last focus"
    )

    # ----- Resume IS a pointer move: simulate `ap2 rewind-focus` /
    # `ap2 update-goal` by re-pointing `active_index` back in range.
    # Now (and only now) `goal.roadmap_exhausted(cfg)` flips to
    # False. -----
    resume_pointer = goal.load_pointer(cfg)
    resume_pointer["active_index"] = 0
    resume_pointer["roadmap_complete_emitted"] = False
    goal.save_pointer(cfg, resume_pointer)
    assert goal.roadmap_exhausted(cfg) is False, (
        "a pointer move back in range (rewind-focus / update-goal) must "
        "clear the gate — `goal.roadmap_exhausted` still returns True"
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

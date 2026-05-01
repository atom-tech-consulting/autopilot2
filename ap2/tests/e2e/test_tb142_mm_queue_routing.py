"""TB-142: end-to-end check that the MM handler RESTRICTED toolset routes
board mutations through `operator_queue_append` instead of `board_edit` —
the load-bearing fix for the second instance of the false-positive
`task_state_violation` class.

Pre-TB-142 a chat command like `@claude-bot freeze TB-X` (or `@claude-bot
approve TB-Y`) called `mcp__autopilot__board_edit` directly. While a task
agent was in flight, that direct TASKS.md mutation tripped TB-110's
fenced-file snapshot check and rolled back the running task — same blast
radius as the operator-side `ap2 add` case TB-141 closed.

This test simulates the in-flight scenario: an Active task is running, the
MM handler appends an `add_backlog` and an `approve` to the operator queue
(mirroring what a real handler does under the new prompt), the task agent
completes via `report_result`, and the daemon finishes the tick. We
assert:
  (a) Both queued ops complete (drain landed at the next-tick boundary).
  (b) No `task_state_violation` event fired against the running task.
  (c) The approved task has its `@blocked:review` codespan stripped.
  (d) The approved task is dispatchable on the following tick (Backlog-
      auto-promoted to Ready, then run to Complete).
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import AsyncIterator

from ap2 import events, tools
from ap2.board import Board
from ap2.daemon import _tick

from ap2.tests.e2e._fakes import FakeSDK, tool_call_respond


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd)] + args,
        capture_output=True, text=True, check=True,
    )


def _git_init(cwd: Path) -> None:
    _git(["init", "--initial-branch=main"], cwd)
    _git(["config", "user.email", "test@example.com"], cwd)
    _git(["config", "user.name", "Test"], cwd)
    _git(["commit", "--allow-empty", "-m", "init"], cwd)
    _git(["add", "TASKS.md", "CLAUDE.md"], cwd)
    _git(["commit", "-m", "baseline"], cwd)


def test_mm_handler_queue_routes_around_state_violation(e2e_project):
    """Active task running. Mid-run, the MM handler appends `add_backlog`
    + `approve` to the operator queue (the queue-routed equivalent of
    `board_edit` calls). Task agent completes via report_result. We
    assert the running task is NOT rolled back, both queue ops apply at
    next tick, and the approved task becomes dispatchable.
    """
    cfg = e2e_project(ready_task=("TB-5", "running while operator types"))
    _git_init(cfg.project_root)

    # Seed a review-gated task that the MM handler will approve mid-run.
    board = Board.load(cfg.tasks_file)
    board.add(
        "Backlog",
        task_id="TB-50",
        title="ideation gated",
        meta={"blocked": "review"},
    )
    board.save()
    _git(["add", "TASKS.md"], cfg.project_root)
    _git(["commit", "-m", "seed ideation-gated task"], cfg.project_root)

    # Snapshot the renders so we can confirm TASKS.md DOESN'T mutate
    # under the running task's snapshot window — the queue defers all
    # mutation to the post-task drain.
    pre_render = cfg.tasks_file.read_text()

    # Agent factory: while the task agent is "running", a concurrent MM
    # handler appends to the operator queue. The agent finishes cleanly
    # via report_result. Pre-TB-142 the equivalent calls would have used
    # `board_edit` and tripped TB-110.
    def factory(prompt, options):  # noqa: ARG001
        async def _gen() -> AsyncIterator:
            # MM handler #1: queue an add_backlog (would have been
            # `board_edit({"action":"add_backlog",...})`).
            tools.do_operator_queue_append(
                cfg,
                {
                    "op": "add_backlog",
                    "title": "queued by MM handler",
                    "briefing": (
                        "# brief\n\n## Verification\n"
                        "- `uv run pytest -q` — gates pass\n"
                    ),
                },
            )
            # MM handler #2: queue an approve for TB-50 (would have been
            # `board_edit({"action":"approve","task_id":"TB-50"})`).
            tools.do_operator_queue_append(
                cfg, {"op": "approve", "task_id": "TB-50"}
            )
            yield SimpleNamespace(content=[
                SimpleNamespace(
                    name="report_result",
                    input={
                        "status": "complete",
                        "summary": "ran while MM handler queued ops",
                    },
                    id="t1",
                ),
            ])
        return _gen()

    sdk = FakeSDK()
    sdk.on("## Task\nTB-5", factory)

    # First tick: TB-5 runs, queue ops are appended mid-run (post-drain
    # for this tick). The drain stage runs FIRST in `_tick`, so this
    # tick's drain doesn't see the mid-run appends — they wait for the
    # next tick's drain. The crucial property here is that TB-5's
    # snapshot window does NOT see a `board_edit` mutation, so no
    # state-violation fires.
    asyncio.run(_tick(cfg, sdk, mcp_server=None))

    # (b) No state violation. The queue file is not fenced (TB-141), and
    # board_edit was NEVER called during the agent's snapshot window.
    evts1 = events.tail(cfg.events_file, 200)
    violations = [e for e in evts1 if e["type"] == "task_state_violation"]
    assert not violations, f"unexpected violation: {violations}"

    # The running task ran to Complete normally.
    assert Board.load(cfg.tasks_file).find("TB-5")[0] == "Complete"

    # The mid-run queue appends are still pending — neither has hit the
    # board yet. This is the structural guarantee that the running
    # task's TASKS.md snapshot was NOT mutated underneath it.
    pending = tools.operator_queue_pending_count(cfg)
    assert pending == 2, (
        f"expected 2 pending ops post-tick-1, got {pending}"
    )
    # And TB-50 still has its review-blocker, because the approve op is
    # in the queue but not yet drained.
    tb50_pre = Board.load(cfg.tasks_file).get("TB-50")
    assert tb50_pre is not None
    assert tb50_pre.meta.get("blocked") == "review"

    # (a) + (c) Run a second tick: drain stage applies BOTH queued ops
    # before any task / cron / ideation work. The approved TB-50's
    # `@blocked:review` codespan disappears; the add_backlog lands.
    # (d) Then auto-promotion lifts TB-50 from Backlog → Ready and the
    # tick dispatches it; we script the SDK to complete it cleanly.
    sdk2 = FakeSDK()
    sdk2.on(
        "## Task\nTB-50",
        tool_call_respond(
            "report_result",
            {
                "status": "complete",
                "commit": "feedface",
                "summary": "approved task dispatched cleanly",
                "tests_passed": "true",
            },
        ),
    )
    asyncio.run(_tick(cfg, sdk2, mcp_server=None))

    board3 = Board.load(cfg.tasks_file)

    # (a) Drain event landed.
    evts2 = events.tail(cfg.events_file, 300)
    drained = [e for e in evts2 if e["type"] == "operator_queue_drained"]
    assert drained, "drain never happened on second tick"
    # Both ops applied.
    assert sum(d["applied"] for d in drained) >= 2

    # The MM-queued add landed in Backlog (or Ready/Complete if it got
    # auto-promoted in the same tick — for TB-142 we only care that it
    # made it onto the board).
    all_titles = [t.title for s in (
        "Backlog", "Ready", "Active", "Complete"
    ) for t in board3.iter_tasks(s)]
    assert any("queued by MM handler" in title for title in all_titles), (
        f"add_backlog op never drained; titles: {all_titles}"
    )

    # (c) TB-50's `@blocked:review` codespan is gone.
    tb50 = board3.get("TB-50")
    assert tb50 is not None
    assert "blocked" not in tb50.meta, (
        f"approve op never drained; TB-50 still has meta {tb50.meta}"
    )
    assert tb50.blocked_on == []
    # `ideation_approved` audit event landed for TB-50.
    approved = [e for e in evts2 if e["type"] == "ideation_approved"]
    assert any(e.get("task") == "TB-50" for e in approved)

    # (d) TB-50 was dispatched on this tick (review-blocker stripped at
    # drain → Backlog auto-promote → Ready → dispatch in the same tick).
    assert board3.find("TB-50")[0] == "Complete", (
        "approved task should have been dispatchable on the next tick"
    )

    # And the snapshot-during-run pin: TASKS.md WAS modified by the
    # second tick's drain (which is fine — that drain runs between the
    # first task's window and the second's), but NOT during TB-5's
    # snapshot window (asserted above by the absence of state violations
    # and by the queue's pre-drain pending count).
    assert pre_render != cfg.tasks_file.read_text(), (
        "control: TASKS.md must reflect the drained ops on the post-state."
    )


def test_mm_handler_toolset_does_not_carry_board_edit(tmp_path):
    """Constant-shape pin: this whole exercise is moot if the toolset
    constant still ships `board_edit`. Pin it here so a refactor can't
    silently restore it. TB-145 collapsed FULL/RESTRICTED into a single
    `MM_HANDLER_TOOLS` set, so `board_edit` is now unconditionally
    absent (the previous TB-122 FULL variant kept it; that variant no
    longer exists)."""
    from ap2.tools import MM_HANDLER_TOOLS

    assert "mcp__autopilot__board_edit" not in MM_HANDLER_TOOLS
    assert "mcp__autopilot__operator_queue_append" in MM_HANDLER_TOOLS

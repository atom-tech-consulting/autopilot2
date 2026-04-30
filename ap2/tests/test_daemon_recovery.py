"""Tests for orphan recovery, SDK query timeout, and retry bounds in run_task.

The SDK is stubbed with a lightweight fake so these tests don't need the real
claude_agent_sdk installed.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from ap2 import events, retry
from ap2.board import Board
from ap2.config import Config
from ap2.daemon import _recover_orphans, run_task


# ---------- fixtures ----------


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    (tmp_path / "TASKS.md").write_text(
        "# Tasks\n\n"
        "## Active\n\n"
        "## Ready\n\n"
        "- [ ] **TB-5** **Victim** `#x` — Will be run. [→ brief](brief.md)\n\n"
        "## Backlog\n\n## Complete\n\n## Frozen\n"
    )
    (tmp_path / "CLAUDE.md").write_text(
        "## Autopilot\n\n- Task list: `TASKS.md`\n- Next task ID: TB-10\n"
    )
    # Keep retries low so the retry-exhaustion test is quick.
    # Clear AP2_VERIFY_CMD so a project-level verify setting in the caller's
    # environment doesn't leak into these unit tests and cause run_task to run
    # the real test suite against a tmp-dir skeleton (which fails and moves
    # tasks to Frozen instead of Complete, breaking all completion assertions).
    import os
    _saved_verify = os.environ.pop("AP2_VERIFY_CMD", None)
    os.environ["AP2_MAX_RETRIES"] = "2"
    os.environ["AP2_TASK_TIMEOUT_S"] = "60"
    cfg_ = Config.load(tmp_path)
    cfg_.ensure_dirs()
    yield cfg_
    os.environ.pop("AP2_MAX_RETRIES", None)
    os.environ.pop("AP2_TASK_TIMEOUT_S", None)
    if _saved_verify is not None:
        os.environ["AP2_VERIFY_CMD"] = _saved_verify


# ---------- fake SDK ----------


class _FakeMsg:
    def __init__(self, text: str) -> None:
        self.content = [SimpleNamespace(text=text)]


class _FakeToolMsg:
    """Message with one tool_use block — what real SDK ToolUseBlock looks
    like to `daemon._log_message`'s walk."""

    def __init__(self, name: str, args: dict) -> None:
        self.content = [
            SimpleNamespace(name=name, input=args, id="tu-1"),
        ]


def _make_sdk(behavior):
    """Build a stub with the minimum surface run_task uses.

    `behavior` is a callable that returns an async iterator (or raises).
    """
    class _Options:
        def __init__(self, **kw):
            self.kw = kw

    def _query(prompt, options):  # noqa: ARG001
        return behavior()

    return SimpleNamespace(query=_query, ClaudeAgentOptions=_Options)


def _sdk_yielding(text: str):
    async def gen():
        yield _FakeMsg(text)

    return _make_sdk(gen)


def _sdk_yielding_report(args: dict):
    """Stub that yields a `report_result` tool_use block with `args`. The
    post-TB-104 replacement for `_sdk_yielding("RESULT:\\n...")`.
    """
    async def gen():
        yield _FakeToolMsg("report_result", args)

    return _make_sdk(gen)


def _sdk_hanging(sleep_s: float = 10.0):
    async def gen():
        await asyncio.sleep(sleep_s)
        yield _FakeMsg("(unreachable)")

    return _make_sdk(gen)


def _sdk_raising(exc: Exception):
    async def gen():
        if False:  # make it a generator
            yield None
        raise exc

    return _make_sdk(gen)


# ---------- orphan recovery ----------


def test_recover_orphans_moves_active_to_ready(cfg, tmp_path):
    board = Board.load(cfg.tasks_file)
    board.move("TB-5", "Active")
    board.save()
    assert board.find("TB-5")[0] == "Active"

    _recover_orphans(cfg)

    b2 = Board.load(cfg.tasks_file)
    assert b2.find("TB-5")[0] == "Ready"
    evts = events.tail(cfg.events_file, 10)
    assert any(e["type"] == "orphan_recovery" and e["task"] == "TB-5" for e in evts)


def test_recover_orphans_noop_when_no_active(cfg):
    _recover_orphans(cfg)
    b = Board.load(cfg.tasks_file)
    assert b.find("TB-5")[0] == "Ready"
    evts = events.tail(cfg.events_file, 10)
    assert not any(e["type"] == "orphan_recovery" for e in evts)


# ---------- timeout ----------


def test_task_timeout_moves_to_backlog(cfg, monkeypatch):
    cfg.task_timeout_s = 1  # force fast timeout
    board = Board.load(cfg.tasks_file)
    task = board.get("TB-5")

    sdk = _sdk_hanging(sleep_s=5)
    asyncio.run(run_task(cfg, sdk, None, task))

    b2 = Board.load(cfg.tasks_file)
    # After 1 failure (max_retries=2), task should be in Backlog, not Frozen.
    assert b2.find("TB-5")[0] == "Backlog"
    evts = events.tail(cfg.events_file, 20)
    assert any(e["type"] == "task_timeout" and e["task"] == "TB-5" for e in evts)
    assert retry.attempt_count(cfg.retry_state_file, "TB-5") == 1


# ---------- retry bound ----------


def test_retry_exhaustion_moves_to_frozen(cfg):
    cfg.task_timeout_s = 1
    board = Board.load(cfg.tasks_file)
    task = board.get("TB-5")

    # max_retries=2: first failure → Backlog, second → Frozen.
    sdk = _sdk_raising(RuntimeError("boom"))

    # Run once. Task goes to Backlog.
    asyncio.run(run_task(cfg, sdk, None, task))
    b = Board.load(cfg.tasks_file)
    assert b.find("TB-5")[0] == "Backlog"
    assert retry.attempt_count(cfg.retry_state_file, "TB-5") == 1

    # Move back to Ready (daemon would pick it off Ready next tick) and try again.
    from ap2.tools import do_board_edit
    do_board_edit(cfg, {"action": "move_to_ready", "task_id": "TB-5"})
    task2 = Board.load(cfg.tasks_file).get("TB-5")
    asyncio.run(run_task(cfg, sdk, None, task2))

    b2 = Board.load(cfg.tasks_file)
    assert b2.find("TB-5")[0] == "Frozen"
    evts = events.tail(cfg.events_file, 30)
    assert any(e["type"] == "retry_exhausted" and e["task"] == "TB-5" for e in evts)


def test_successful_run_resets_attempt_counter(cfg):
    board = Board.load(cfg.tasks_file)
    task = board.get("TB-5")

    # Pre-seed one prior failed attempt.
    retry.bump_attempt(cfg.retry_state_file, "TB-5")
    assert retry.attempt_count(cfg.retry_state_file, "TB-5") == 1

    sdk = _sdk_yielding_report(
        {"status": "complete", "commit": "abc12345", "summary": "did it"}
    )
    asyncio.run(run_task(cfg, sdk, None, task))

    b2 = Board.load(cfg.tasks_file)
    assert b2.find("TB-5")[0] == "Complete"
    assert retry.attempt_count(cfg.retry_state_file, "TB-5") == 0


# ---------- run_task invariants (TB-51) ----------


def test_run_task_emits_start_and_complete_events(cfg):
    board = Board.load(cfg.tasks_file)
    task = board.get("TB-5")
    sdk = _sdk_yielding_report(
        {"status": "complete", "commit": "deadbeef", "summary": "done"}
    )
    asyncio.run(run_task(cfg, sdk, None, task))
    evts = events.tail(cfg.events_file, 20)
    kinds = [e["type"] for e in evts]
    assert "task_start" in kinds
    assert "task_complete" in kinds
    start = next(e for e in evts if e["type"] == "task_start")
    end = next(e for e in reversed(evts) if e["type"] == "task_complete")
    assert start["task"] == "TB-5"
    assert start["title"] == "Victim"
    assert end["task"] == "TB-5"
    assert end["status"] == "complete"
    assert end["commit"] == "deadbeef"


def test_run_task_does_not_bump_next_task_id(cfg, tmp_path):
    before = (tmp_path / "CLAUDE.md").read_text()
    board = Board.load(cfg.tasks_file)
    task = board.get("TB-5")
    sdk = _sdk_yielding_report({"status": "complete", "summary": "ok"})
    asyncio.run(run_task(cfg, sdk, None, task))
    after = (tmp_path / "CLAUDE.md").read_text()
    assert "TB-10" in after
    assert before == after


def test_run_task_appends_progress_section_on_complete(cfg):
    board = Board.load(cfg.tasks_file)
    task = board.get("TB-5")
    sdk = _sdk_yielding_report({
        "status": "complete",
        "commit": "deadbeef1234",
        "summary": "Added X to Y",
        "files_changed": "a.py, b.py",
        "tests_passed": "true",
    })
    asyncio.run(run_task(cfg, sdk, None, task))

    text = cfg.progress_file.read_text()
    # Section header with task id + title — not a bare bullet.
    assert "## [" in text
    assert "TB-5: Victim" in text
    # Commit truncated to 8 chars.
    assert "deadbeef" in text
    assert "deadbeef1234" not in text
    # Summary + files + tests surfaced.
    assert "Added X to Y" in text
    assert "a.py, b.py" in text
    assert "Tests:** pass" in text


def test_run_task_progress_skips_empty_fields(cfg):
    """Only include fields that the RESULT actually populated."""
    board = Board.load(cfg.tasks_file)
    task = board.get("TB-5")
    sdk = _sdk_yielding_report({"status": "complete", "summary": "short"})
    asyncio.run(run_task(cfg, sdk, None, task))
    text = cfg.progress_file.read_text()
    assert "TB-5: Victim" in text
    assert "short" in text
    # No Commit / Files / Tests lines when those fields are absent.
    assert "Commit:" not in text
    assert "Files:" not in text
    assert "Tests:" not in text


def test_run_task_cron_propose_emits_event_with_proposed_by_task(cfg):
    """TB-123: a task agent calling `cron_propose(...)` mid-run emits a
    `cron_proposed` event with all four args populated AND a
    `proposed_by_task` field stamped from the daemon's contextvar plumb.
    The cron.yaml is NOT mutated — proposals are queued for operator
    review, not auto-applied.

    Pre-TB-123 the cron list piggybacked on `report_result(cron=...)` and
    the daemon called `do_cron_edit` directly to mutate the registry.
    Splitting the proposal off into its own MCP tool gives each proposal
    its own event with rationale, decouples failure isolation, and
    clarifies the privilege boundary (control agents mutate via
    `cron_edit`; task agents propose via `cron_propose`).
    """
    from ap2.cron import load_jobs
    from types import SimpleNamespace

    board = Board.load(cfg.tasks_file)
    task = board.get("TB-5")

    # Two messages: a `cron_propose` tool call followed by a `report_result`
    # tool call that ends the run. The fake SDK delivers both as ToolUseBlock
    # parts so the daemon's `_log_message` walker sees the cron_propose name
    # and `do_cron_propose` runs (which emits the event using the
    # contextvar's value of task.id).
    async def gen():
        # Real SDK exposes tool_use blocks via `name` + `input` attributes —
        # mirror that. We invoke `do_cron_propose` ourselves so the test
        # exercises the same handler the live MCP server would route to.
        from ap2 import tools

        # Simulate the agent calling cron_propose during the SDK query
        # window — call the handler directly under the contextvar that
        # run_task sets.
        tools.do_cron_propose(cfg, {
            "name": "weekly-perf",
            "schedule": "1d",
            "prompt": "run the perf suite",
            "rationale": "operator wanted weekly visibility",
        })
        yield _FakeToolMsg("report_result", {
            "status": "complete",
            "commit": "beefcafe",
            "summary": "wired it up",
        })

    sdk = _make_sdk(gen)
    asyncio.run(run_task(cfg, sdk, None, task))

    # cron.yaml stays empty — proposals don't auto-promote.
    assert load_jobs(cfg.cron_file) == []

    evts = events.tail(cfg.events_file, 30)
    proposals = [e for e in evts if e["type"] == "cron_proposed"]
    assert len(proposals) == 1, proposals
    p = proposals[0]
    assert p["name"] == "weekly-perf"
    assert p["schedule"] == "1d"
    assert p["prompt"] == "run the perf suite"
    assert p["rationale"] == "operator wanted weekly visibility"
    assert p["proposed_by_task"] == "TB-5"


def test_run_task_cron_propose_event_fires_regardless_of_status(cfg):
    """Pre-TB-123 the cron-directive dispatch only ran on `status=complete`.
    Post-TB-123 the proposal IS the event — there's no post-result
    dispatch step to gate. Even if the agent ends up reporting `blocked`,
    a `cron_propose` call made earlier in the run still records a
    proposal (the operator can decide whether to follow up). The "do not
    apply on incomplete" semantic moves to the operator review surface,
    not the daemon's event-emission gate.
    """
    board = Board.load(cfg.tasks_file)
    task = board.get("TB-5")

    async def gen():
        from ap2 import tools
        tools.do_cron_propose(cfg, {
            "name": "midwork-proposal",
            "schedule": "1h",
            "prompt": "noop",
            "rationale": "noticed during partial work",
        })
        yield _FakeToolMsg("report_result", {
            "status": "blocked",
            "summary": "stuck",
        })

    sdk = _make_sdk(gen)
    asyncio.run(run_task(cfg, sdk, None, task))

    evts = events.tail(cfg.events_file, 30)
    proposals = [e for e in evts if e["type"] == "cron_proposed"]
    assert any(p.get("name") == "midwork-proposal" for p in proposals)
    # Stamped with the calling task even though report_result said blocked.
    assert proposals[-1]["proposed_by_task"] == "TB-5"


def test_run_task_cron_propose_supports_multiple_proposals(cfg):
    """Each `cron_propose` call gets its own event with its own rationale.
    Pre-TB-123 the agent had to bundle all proposals into one JSON list
    inside `report_result(cron=...)`; post-TB-123 each call is
    independent so the operator's review surface sees one row per
    proposal."""
    board = Board.load(cfg.tasks_file)
    task = board.get("TB-5")

    async def gen():
        from ap2 import tools
        for i, name in enumerate(("alpha", "beta", "gamma")):
            tools.do_cron_propose(cfg, {
                "name": name,
                "schedule": f"{i + 1}h",
                "prompt": f"prompt for {name}",
                "rationale": f"why {name}",
            })
        yield _FakeToolMsg("report_result", {
            "status": "complete",
            "commit": "feedface",
            "summary": "three proposals filed",
        })

    sdk = _make_sdk(gen)
    asyncio.run(run_task(cfg, sdk, None, task))

    evts = events.tail(cfg.events_file, 30)
    proposals = [e for e in evts if e["type"] == "cron_proposed"]
    names = [p["name"] for p in proposals]
    assert names == ["alpha", "beta", "gamma"]
    # All stamped with the same task id.
    for p in proposals:
        assert p["proposed_by_task"] == "TB-5"


def test_run_task_cron_propose_requires_all_four_fields(cfg):
    """`do_cron_propose` returns an error result when any of name /
    schedule / prompt / rationale is missing, and does NOT emit a
    `cron_proposed` event. Failure-isolation pin: a malformed proposal
    must not crash result reporting (the whole reason TB-123 split it
    out of `report_result`)."""
    from ap2 import tools

    res = tools.do_cron_propose(cfg, {
        "name": "x", "schedule": "1h", "prompt": "p",
        # rationale missing
    })
    assert res.get("isError"), res
    assert "rationale" in res["content"][0]["text"]

    evts = events.tail(cfg.events_file, 30)
    assert not any(e["type"] == "cron_proposed" for e in evts)


def test_run_task_blocked_moves_to_backlog_and_writes_attempts(cfg, tmp_path):
    # Swap the fixture briefing for a real file so _append_attempts can write.
    brief = tmp_path / "brief.md"
    brief.write_text("# Existing\n")
    tasks_text = cfg.tasks_file.read_text().replace(
        "[→ brief](brief.md)", f"[→ brief]({brief.name})"
    )
    cfg.tasks_file.write_text(tasks_text)
    board = Board.load(cfg.tasks_file)
    task = board.get("TB-5")

    sdk = _sdk_yielding_report(
        {"status": "blocked", "summary": "needs human input"}
    )
    asyncio.run(run_task(cfg, sdk, None, task))

    b2 = Board.load(cfg.tasks_file)
    # max_retries=2 → first failure should park in Backlog, not Frozen.
    assert b2.find("TB-5")[0] == "Backlog"
    text = brief.read_text()
    assert "## Attempts" in text
    assert "blocked" in text
    assert "needs human input" in text
    # TB-114: every Attempts entry carries the per-run debug-dump paths
    # so the next attempt's agent can `Read` them when the truncated
    # summary isn't enough.
    assert "Debug dumps" in text
    assert "prompt:" in text
    assert "stream:" in text
    assert "messages:" in text


# ---------------------------------------------------------------------------
# TB-114: every failure mode appends an `## Attempts` entry. Pre-TB-114
# `_append_attempts` only fired when `parsed is not None`, so timeout /
# error / state_violation / verification_failed left no narrative trail
# in the briefing — agents retrying a Frozen task had nothing to read.

def _swap_briefing(cfg, tmp_path):
    """Replace the cfg fixture's `brief.md` placeholder with a real file
    on disk so `_append_attempts` can write to it. Returns the path."""
    brief = tmp_path / "brief.md"
    brief.write_text("# Existing\n")
    tasks_text = cfg.tasks_file.read_text().replace(
        "[→ brief](brief.md)", f"[→ brief]({brief.name})"
    )
    cfg.tasks_file.write_text(tasks_text)
    return brief


def test_timeout_appends_attempts_with_debug_paths(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("AP2_TASK_TIMEOUT_S", "1")
    brief = _swap_briefing(cfg, tmp_path)
    cfg2 = Config.load(cfg.project_root)
    task = Board.load(cfg2.tasks_file).get("TB-5")

    sdk = _sdk_hanging(sleep_s=5.0)
    asyncio.run(run_task(cfg2, sdk, None, task))

    text = brief.read_text()
    assert "## Attempts" in text
    assert "timeout" in text
    assert "timeout_s" in text  # kw extra rendered as `- **timeout_s:** 1`
    assert "Debug dumps" in text
    assert "prompt:" in text


def test_sdk_error_appends_attempts_with_debug_paths(cfg, tmp_path):
    brief = _swap_briefing(cfg, tmp_path)
    cfg2 = Config.load(cfg.project_root)
    task = Board.load(cfg2.tasks_file).get("TB-5")

    sdk = _sdk_raising(RuntimeError("Command failed with exit code 1"))
    asyncio.run(run_task(cfg2, sdk, None, task))

    text = brief.read_text()
    assert "## Attempts" in text
    assert "error" in text
    assert "RuntimeError" in text
    assert "Debug dumps" in text


def test_state_violation_appends_attempts_with_fenced_files(cfg, tmp_path):
    """An agent that touches CLAUDE.md (fenced) → TB-110 violation →
    Attempts entry includes the fenced-file list + debug paths.
    """
    brief = _swap_briefing(cfg, tmp_path)
    cfg2 = Config.load(cfg.project_root)
    task = Board.load(cfg2.tasks_file).get("TB-5")

    # FakeSDK that mutates CLAUDE.md before yielding report_result.
    project_root = cfg2.project_root

    class _MutatingSDK:
        class ClaudeAgentOptions:
            def __init__(self, **kw):
                self.kw = kw

        def query(self, *, prompt, options):  # noqa: ARG002
            async def _gen():
                # Dirty a fenced file mid-run.
                (project_root / "CLAUDE.md").write_text("rewritten by agent\n")
                yield _FakeToolMsg(
                    "report_result", {"status": "complete", "summary": "ok"},
                )
            return _gen()

    asyncio.run(run_task(cfg2, _MutatingSDK(), None, task))

    text = brief.read_text()
    assert "## Attempts" in text
    assert "state_violation" in text
    assert "fenced_files" in text
    assert "CLAUDE.md" in text
    assert "Debug dumps" in text


def test_verification_failed_project_wide_appends_attempts(
    cfg, tmp_path, monkeypatch,
):
    """Project-wide verifier (`AP2_VERIFY_CMD`) failure → Attempts entry
    captures kind + verify_command + exit_code + stderr_tail.
    """
    monkeypatch.setenv("AP2_VERIFY_CMD", "false")
    brief = _swap_briefing(cfg, tmp_path)
    cfg2 = Config.load(cfg.project_root)
    task = Board.load(cfg2.tasks_file).get("TB-5")

    sdk = _sdk_yielding_report({"status": "complete", "summary": "did it"})
    asyncio.run(run_task(cfg2, sdk, None, task))

    text = brief.read_text()
    assert "## Attempts" in text
    assert "verification_failed" in text
    assert "kind" in text and "project_wide" in text
    assert "verify_command" in text
    assert "exit_code" in text
    assert "Debug dumps" in text


def test_incomplete_status_appends_attempts(cfg, tmp_path):
    """The pre-TB-114 path (parsed is not None) still works — `incomplete`
    status appends just like `blocked`/`failed`. Pin so the existing
    coverage doesn't regress alongside the new failure modes."""
    brief = _swap_briefing(cfg, tmp_path)
    cfg2 = Config.load(cfg.project_root)
    task = Board.load(cfg2.tasks_file).get("TB-5")

    sdk = _sdk_yielding_report(
        {"status": "incomplete", "summary": "did half the scope"},
    )
    asyncio.run(run_task(cfg2, sdk, None, task))

    text = brief.read_text()
    assert "## Attempts" in text
    assert "incomplete" in text
    assert "did half the scope" in text
    assert "Debug dumps" in text

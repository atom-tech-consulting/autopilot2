"""Tests for the cli-prefixed review-channel verbs (TB-266 split from
`test_cli.py`).

Mirrors `ap2/cli_review.py` (TB-264 split): cmd_audit / cmd_ack /
cmd_rollback / cmd_ideate / cmd_update_goal / cmd_backfill_proposals.
Verb groupings preserved from the pre-split section headers — see the
divider comments below for the TB-N each block traces back to.
"""
from __future__ import annotations

from argparse import Namespace
from pathlib import Path

from ap2 import tools
from ap2.board import Board
from ap2.cli import cmd_ideate
from ap2.tests.conftest import _drain, _project


# ---------------------------------------------------------------------------
# cmd_ideate (TB-159) — manual ideation trigger that bypasses the natural
# empty-board / cooldown / `AP2_IDEATION_DISABLED` gates by routing through
# the operator queue. The actual SDK invocation lives on the daemon side
# (`force_ideate` runs after `drain_operator_queue` returns
# `force_ideate=True`); the CLI just appends the queue record and exits.


def test_cmd_ideate_appends_queue_record_with_force_false_default(tmp_path: Path):
    """Default invocation (`ap2 ideate`, no `--force`) writes one
    `{"op":"ideate","force":false,...}` queue record and returns 0
    immediately."""
    cfg = _project(tmp_path)
    rc = cmd_ideate(cfg, Namespace(force=False))
    assert rc == 0

    queue_path = tools.operator_queue_path(cfg)
    lines = [
        ln for ln in queue_path.read_text().splitlines() if ln.strip()
    ]
    assert len(lines) == 1
    import json as _json

    rec = _json.loads(lines[0])
    assert rec["op"] == "ideate"
    assert rec["args"] == {"force": False}


def test_cmd_ideate_queues_when_active_task_present_no_force(
    tmp_path: Path, capsys
):
    """TB-194: with a task in Active, `ap2 ideate` (no `--force`) now
    queues successfully — the at-append-time Active gate has been
    removed (the loop topology already prevents the race the gate was
    guarding). Pre-TB-194 this was a hard rc=1 reject."""
    cfg = _project(tmp_path)
    board = Board.load(cfg.tasks_file)
    board.add("Active", task_id="TB-2000", title="in flight")
    board.save()

    rc = cmd_ideate(cfg, Namespace(force=False))
    assert rc == 0
    err = capsys.readouterr().err
    assert err == ""

    queue_path = tools.operator_queue_path(cfg)
    import json as _json

    lines = [ln for ln in queue_path.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1
    rec = _json.loads(lines[0])
    assert rec["op"] == "ideate"
    assert rec["args"] == {"force": False}


def test_cmd_ideate_force_still_queues_with_active(tmp_path: Path):
    """TB-194: `--force` is now a no-op for the routing decision but
    still rides the queue payload as audit metadata. `ap2 ideate
    --force` queues a record with `args.force=true` regardless of
    board state — same outcome as no-force, with the operator-intent
    flag preserved on the queue record."""
    cfg = _project(tmp_path)
    board = Board.load(cfg.tasks_file)
    board.add("Active", task_id="TB-2010", title="forced through")
    board.save()

    rc = cmd_ideate(cfg, Namespace(force=True))
    assert rc == 0

    queue_path = tools.operator_queue_path(cfg)
    import json as _json

    lines = [ln for ln in queue_path.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1
    rec = _json.loads(lines[0])
    assert rec["op"] == "ideate"
    assert rec["args"]["force"] is True


def test_cmd_ideate_is_non_blocking_no_sdk_invocation(
    tmp_path: Path, monkeypatch
):
    """The CLI MUST NOT spin up the SDK / control-agent itself — the
    daemon owns that. Pin the contract by stubbing `_run_control_agent`,
    `force_ideate`, and `_maybe_ideate` to raise if invoked, then
    asserting the call returns within a tight wallclock budget."""
    import time as _time

    cfg = _project(tmp_path)

    from ap2 import daemon as _daemon
    from ap2 import ideation as _ideation

    def boom(*a, **kw):
        raise AssertionError(
            "cmd_ideate must NOT invoke the SDK / ideation directly — "
            "the daemon picks up the queue signal on the next tick."
        )

    monkeypatch.setattr(_daemon, "_run_control_agent", boom)
    monkeypatch.setattr(_ideation, "force_ideate", boom)
    monkeypatch.setattr(_ideation, "_maybe_ideate", boom)
    monkeypatch.setattr(_ideation, "_run_ideation", boom)

    t0 = _time.monotonic()
    rc = cmd_ideate(cfg, Namespace(force=False))
    elapsed = _time.monotonic() - t0

    assert rc == 0
    # 2s is generous; in practice the queue append is sub-millisecond.
    assert elapsed < 2.0, (
        f"cmd_ideate should return immediately; took {elapsed:.3f}s"
    )


# ---------------------------------------------------------------------------
# TB-193: cmd_update_goal — refresh goal.md via the operator queue so
# focus rotations don't require `ap2 daemon-control --pause`. The CLI
# reads from --file <path> or --file - (stdin), then dispatches via
# `do_operator_queue_append({"op":"update_goal", ...})`.


_UPDATE_GOAL_PAYLOAD = (
    "# Project Goals\n\n"
    "## Mission\nShip ap2 hands-off-by-default.\n\n"
    "## Done when\n- Operators can refresh goal.md without pausing.\n\n"
    "## Current focus: ideation quality\n\nSignal collection.\n"
)


def test_cmd_update_goal_file_path_dispatches(tmp_path: Path):
    """`--file <path>` reads from disk and queues an `update_goal` op.
    Drain applies the write; goal.md on disk matches the payload."""
    from ap2.cli import cmd_update_goal

    cfg = _project(tmp_path)
    payload_path = tmp_path / "new_goal.md"
    payload_path.write_text(_UPDATE_GOAL_PAYLOAD)

    rc = cmd_update_goal(
        cfg,
        Namespace(file=str(payload_path), reason=None),
    )
    assert rc == 0
    # Pre-drain: queue line written but goal.md unchanged.
    qpath = tools.operator_queue_path(cfg)
    assert qpath.exists() and qpath.read_text().strip()

    res = tools.drain_operator_queue(cfg)
    assert res["applied"] == 1
    assert (cfg.project_root / "goal.md").read_text() == _UPDATE_GOAL_PAYLOAD


def test_cmd_update_goal_stdin_dispatches(tmp_path: Path, monkeypatch):
    """`--file -` reads the goal payload from stdin (mirrors `ap2 add
    --briefing-file -`)."""
    from ap2.cli import cmd_update_goal
    import io
    import sys

    cfg = _project(tmp_path)
    monkeypatch.setattr(sys, "stdin", io.StringIO(_UPDATE_GOAL_PAYLOAD))

    rc = cmd_update_goal(
        cfg,
        Namespace(file="-", reason="rotated focus from migration"),
    )
    assert rc == 0
    tools.drain_operator_queue(cfg)
    assert (cfg.project_root / "goal.md").read_text() == _UPDATE_GOAL_PAYLOAD


def test_cmd_update_goal_reason_plumbed_through(tmp_path: Path):
    """`--reason "..."` rides on the queue payload and lands in the
    operator-log audit line `<ts> — operator updated goal.md (<reason>)`."""
    from ap2.cli import cmd_update_goal

    cfg = _project(tmp_path)
    payload_path = tmp_path / "goal_payload.md"
    payload_path.write_text(_UPDATE_GOAL_PAYLOAD)

    rc = cmd_update_goal(
        cfg,
        Namespace(
            file=str(payload_path),
            reason="rotate focus to ideation quality",
        ),
    )
    assert rc == 0
    tools.drain_operator_queue(cfg)

    log = (cfg.project_root / ".cc-autopilot" / "operator_log.md").read_text()
    assert (
        "operator updated goal.md (rotate focus to ideation quality)" in log
    )


def test_cmd_update_goal_empty_payload_rejected(tmp_path: Path, capsys):
    """Whitespace-only file is rejected with non-zero exit + a hint
    message — the CLI defends against a path-vs-content typo before
    reaching the queue-append validator."""
    from ap2.cli import cmd_update_goal

    cfg = _project(tmp_path)
    empty = tmp_path / "empty.md"
    empty.write_text("   \n\n")

    rc = cmd_update_goal(
        cfg, Namespace(file=str(empty), reason=None)
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "empty" in err.lower()
    # No queue line written.
    qpath = tools.operator_queue_path(cfg)
    assert not qpath.exists() or qpath.read_text() == ""


def test_cmd_update_goal_too_large_rejected(tmp_path: Path, capsys):
    """Soft 100KB cap surfaces a path-vs-content mistake (e.g. operator
    pointed at a log file). Refused before the queue-append call."""
    from ap2.cli import cmd_update_goal

    cfg = _project(tmp_path)
    big = tmp_path / "big.md"
    big.write_text("# huge\n" + ("x" * 200_000))

    rc = cmd_update_goal(
        cfg, Namespace(file=str(big), reason=None)
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "100000" in err or "cap" in err.lower()


def test_cmd_update_goal_missing_file_errors(tmp_path: Path, capsys):
    """Bad path → non-zero exit + readable error. We don't want a stray
    OSError traceback bubbling up to the operator."""
    from ap2.cli import cmd_update_goal

    cfg = _project(tmp_path)
    rc = cmd_update_goal(
        cfg,
        Namespace(file=str(tmp_path / "does_not_exist.md"), reason=None),
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "ap2 update-goal" in err


# ---------------------------------------------------------------------------
# TB-201: cmd_ack — operator-decision channel for ideation. Routes
# through the operator queue rather than writing operator_log.md
# synchronously (the pre-TB-201 in-place write tripped TB-110's
# post-hoc fenced-file snapshot check on any task agent running
# concurrently with the operator's `ap2 ack`).


def test_ack_queues_and_does_not_write_operator_log(tmp_path: Path, capsys):
    """Briefing-spec verification: `cmd_ack` exits 0, queues exactly one
    record with `op="ack"` carrying the supplied note + task_id, leaves
    `operator_log.md` UNMODIFIED (write happens at drain time), and prints
    the documented "queued ack" shape (≤200 chars, contains 'queued')."""
    from ap2.cli import cmd_ack
    import json as _json

    cfg = _project(tmp_path)
    log_path = cfg.project_root / ".cc-autopilot" / "operator_log.md"
    pre_existed = log_path.exists()
    pre_text = log_path.read_text() if pre_existed else ""

    rc = cmd_ack(
        cfg,
        Namespace(
            note="closed TB-200 follow-up — retried on dev box, no repro",
            task=None,
        ),
    )
    assert rc == 0

    # operator_log.md is unchanged (regression bar — the whole point of
    # the TB-201 retrofit).
    assert log_path.exists() == pre_existed
    if pre_existed:
        assert log_path.read_text() == pre_text

    # The queue carries exactly one `ack` record with the note verbatim.
    queue_path = tools.operator_queue_path(cfg)
    recs = [
        _json.loads(ln)
        for ln in queue_path.read_text().splitlines() if ln.strip()
    ]
    ack_recs = [r for r in recs if r.get("op") == "ack"]
    assert len(ack_recs) == 1
    assert ack_recs[0]["args"]["note"].startswith("closed TB-200 follow-up")
    assert ack_recs[0]["args"]["task_id"] == ""

    # CLI output matches the "queued ack" UX shape.
    out = capsys.readouterr().out.strip()
    assert "queued" in out
    assert len(out) <= 200


def test_ack_with_task_id_records_task_id_on_queue_record(tmp_path: Path):
    """Briefing-spec verification: `-t TB-N` rides on the queued
    record's args as `task_id`; the drain-side picks it up to render
    the `[TB-N]` tag in operator_log.md."""
    from ap2.cli import cmd_ack
    import json as _json

    cfg = _project(tmp_path)
    rc = cmd_ack(
        cfg,
        Namespace(note="LaunchAgent installed", task="TB-139"),
    )
    assert rc == 0
    queue_path = tools.operator_queue_path(cfg)
    recs = [
        _json.loads(ln)
        for ln in queue_path.read_text().splitlines() if ln.strip()
    ]
    ack_recs = [r for r in recs if r.get("op") == "ack"]
    assert ack_recs[0]["args"]["task_id"] == "TB-139"

    # Drain → operator_log.md gets the bullet with `[TB-139]`.
    _drain(cfg)
    log = (cfg.project_root / ".cc-autopilot" / "operator_log.md").read_text()
    assert "[TB-139] — LaunchAgent installed" in log


def test_ack_drain_writes_operator_log_and_event(tmp_path: Path):
    """Briefing-spec verification: synthesize a queued ack, run
    `drain_operator_queue`, assert (a) `operator_log.md` carries the
    bullet line in the documented `- <ts> [TB-N] — <note>` shape, (b)
    events.jsonl carries an `operator_ack` event with the note + task
    fields, (c) the audit line `applied operator-queued ack → TB-N`
    is also written to operator_log.md (the verb-vs-other-ops audit
    pointer that other queue-routed ops also emit)."""
    from ap2 import events as _events
    cfg = _project(tmp_path)
    res = tools.enqueue_operator_ack(
        cfg, {"note": "operator ate the frog", "task_id": "TB-9"}
    )
    assert not res.get("isError"), res

    _drain(cfg)

    log = (
        cfg.project_root / ".cc-autopilot" / "operator_log.md"
    ).read_text()
    # (a) ack bullet line in the historical shape.
    import re
    bullet_re = re.compile(
        r"^- \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z \[TB-9\] — operator ate the frog$",
        re.MULTILINE,
    )
    assert bullet_re.search(log), (
        f"ack bullet not found in operator_log.md:\n{log}"
    )
    # (c) the standard `applied operator-queued ack → TB-9` audit line
    # is ALSO present — same shape as every other queue-routed op.
    audit_re = re.compile(
        r"^- \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z — "
        r"applied operator-queued ack → TB-9$",
        re.MULTILINE,
    )
    assert audit_re.search(log), (
        f"applied-audit line not found in operator_log.md:\n{log}"
    )

    # (b) events.jsonl carries the `operator_ack` event with the
    # note + task fields.
    evts = _events.tail(cfg.events_file, 20)
    ack_evts = [e for e in evts if e["type"] == "operator_ack"]
    assert len(ack_evts) == 1
    assert ack_evts[0]["note"] == "operator ate the frog"
    assert ack_evts[0]["task"] == "TB-9"


def test_ack_mcp_tool_path_queues_same_record(tmp_path: Path):
    """Briefing-spec verification: invoke the `operator_log_append` MCP
    tool body (`enqueue_operator_ack`) — the chat-handler entry point —
    and assert it produces the same queue-append record as the CLI
    path. Verifies the post-TB-201 MCP tool body queues rather than
    writing operator_log.md synchronously."""
    import json as _json

    cfg = _project(tmp_path)
    res = tools.enqueue_operator_ack(
        cfg,
        {"note": "chat-handler ack from claude-bot", "task_id": "TB-77"},
    )
    assert not res.get("isError"), res

    queue_path = tools.operator_queue_path(cfg)
    recs = [
        _json.loads(ln)
        for ln in queue_path.read_text().splitlines() if ln.strip()
    ]
    ack_recs = [r for r in recs if r.get("op") == "ack"]
    assert len(ack_recs) == 1
    assert ack_recs[0]["args"]["note"] == "chat-handler ack from claude-bot"
    assert ack_recs[0]["args"]["task_id"] == "TB-77"

    # Pre-drain: operator_log.md is NOT written. The TB-201 regression
    # bar — chat-driven acks (the MCP path) must defer the write the
    # same way the CLI path does, since the same false-positive
    # state_violation cascade applies regardless of which surface
    # initiated the ack.
    log_path = cfg.project_root / ".cc-autopilot" / "operator_log.md"
    if log_path.exists():
        assert "chat-handler ack from claude-bot" not in log_path.read_text()


def test_ack_no_state_violation_during_in_flight_task(tmp_path: Path):
    """TB-201 regression pin (the load-bearing test): synthesize a
    state where a task agent's run window encloses an `ap2 ack` call.
    Pre-TB-201 this rolled the task back via TB-110's post-hoc
    fenced-file snapshot check. Post-TB-201 the ack is in the queue,
    NOT in operator_log.md — the file's hash is unchanged until the
    drain runs (which the daemon runs at tick boundary, BEFORE
    dispatching the next task).

    Concretely: take `rollback.snapshot_fenced_files` (the daemon's
    pre-task snapshot), run `cmd_ack`, take the post snapshot via
    `rollback.detect_fenced_violations` — assert NO change to
    `.cc-autopilot/operator_log.md` is detected."""
    from ap2 import rollback
    from ap2.cli import cmd_ack
    from ap2.board import Board

    cfg = _project(tmp_path)
    # Seed a fake Active task to make the scenario realistic — the
    # operator's "I just acked while TB-77 is running" case.
    board = Board.load(cfg.tasks_file)
    board.add("Active", task_id="TB-77", title="running task")
    board.save()

    # Pre-task snapshot — what `daemon._run_task` takes before
    # dispatching the agent.
    pre = rollback.snapshot_fenced_files(cfg)

    # Operator runs `ap2 ack` while TB-77 is running. Pre-TB-201 this
    # would write operator_log.md synchronously; post-TB-201 it
    # queues an op instead.
    rc = cmd_ack(
        cfg,
        Namespace(
            note="thinking out loud while TB-77 runs — looks fine so far",
            task=None,
        ),
    )
    assert rc == 0

    # Post-task snapshot — the violation detector compares the two.
    violations = rollback.detect_fenced_violations(cfg, pre)

    # operator_log.md is the file the pre-TB-201 path would dirty.
    # Assert it's NOT in the violation list — the regression bar.
    assert ".cc-autopilot/operator_log.md" not in violations, (
        f"operator_log.md tripped TB-110's post-hoc snapshot check "
        f"even though the ack went through the queue (violations={violations})"
    )


def test_operator_log_append_mcp_name_unchanged(tmp_path: Path):
    """TB-201 backwards-compat pin: the `operator_log_append` MCP
    tool's external name (the string chat handlers send when invoking
    `@claude-bot done: ...`) stays unchanged from pre-TB-201. Only
    the tool's body changes — to call `enqueue_operator_ack` instead
    of `do_operator_log_append`. Pinning the name keeps chat-side
    callers working without recompiling their tool-call dispatch."""
    # The tool name lands in CONTROL_AGENT_TOOLS prefixed with
    # `mcp__autopilot__`. The bare tool name (what `@tool(...)`
    # registers) is what chat handlers see.
    assert "mcp__autopilot__operator_log_append" in tools.CONTROL_AGENT_TOOLS
    # And it's NOT in TASK_AGENT_TOOLS — operator-mediated only.
    assert "mcp__autopilot__operator_log_append" not in tools.TASK_AGENT_TOOLS


# ---------------------------------------------------------------------------
# TB-202: `ap2 backfill-proposals` writes fenced files synchronously
# (bypassing the operator-queue routing pattern). If the operator
# runs it while a task agent is in flight, the TB-110 post-hoc
# snapshot diff detects the fenced-file mutation and rolls the task
# back — same false-positive cascade as the pre-TB-201 `ap2 ack`
# path. TB-202's cheaper-than-queue-routing mitigation is a
# pre-flight refuse-if-active check on the verb; these tests pin the
# refusal text, the exit code, and the "fenced state untouched on
# refuse" invariant. The companion `cmd_cron_edit` half of TB-202
# lives in `test_cli_diagnostic.py` since `cmd_cron_edit` is a
# diagnostic-surface verb (TB-264 source split).


def test_backfill_proposals_refuses_when_active_task_present(tmp_path: Path, capsys):
    """TB-202: with a synthetic Active task on the board,
    `cmd_backfill_proposals` exits non-zero and stderr names the
    refuse-if-active rationale. Mirrors the pattern of TB-201's
    `test_ack_no_state_violation_during_in_flight_task` — except here
    the gate is at the CLI entry, not via queue deferral."""
    from ap2.cli import cmd_backfill_proposals

    cfg = _project(tmp_path)
    board = Board.load(cfg.tasks_file)
    board.add("Active", task_id="TB-77", title="running task")
    board.save()

    rc = cmd_backfill_proposals(cfg, Namespace(dry_run=False))
    assert rc == 1

    err = capsys.readouterr().err
    # Briefing's verification pin: message names the verb, the
    # current-task state, and the refusal verb.
    assert "backfill-proposals" in err
    assert "active" in err.lower()
    assert "refusing" in err.lower()
    # The Active task's TB-N is surfaced so the operator can map the
    # refusal back to a concrete in-flight task.
    assert "TB-77" in err


def test_backfill_proposals_refuse_does_not_mutate_fenced_dir(tmp_path: Path):
    """TB-202 invariant: the refuse path leaves
    `.cc-autopilot/ideation_proposals/` untouched — no new records, no
    file mtimes bumped. Pin captures the directory's file list before
    and after the refuse fires and asserts it's identical.

    Why this matters: the whole point of the gate is to prevent the
    fenced-path write from racing the in-flight task's snapshot
    window. A regression that bypasses the gate (e.g. running
    `backfill_proposals` AFTER the refuse-fires check) would
    re-introduce the rollback cascade."""
    from ap2.cli import cmd_backfill_proposals

    cfg = _project(tmp_path)
    board = Board.load(cfg.tasks_file)
    board.add("Active", task_id="TB-77", title="running task")
    board.save()

    proposals_dir = cfg.project_root / ".cc-autopilot" / "ideation_proposals"
    proposals_dir.mkdir(parents=True, exist_ok=True)
    before = sorted(p.name for p in proposals_dir.iterdir())

    rc = cmd_backfill_proposals(cfg, Namespace(dry_run=False))
    assert rc == 1

    after = sorted(p.name for p in proposals_dir.iterdir())
    assert before == after, (
        f"backfill-proposals refuse path mutated "
        f".cc-autopilot/ideation_proposals/: before={before} after={after}"
    )


def test_backfill_proposals_succeeds_with_empty_active(tmp_path: Path):
    """TB-202 happy path: when board's Active section is empty, the
    refuse-if-active gate falls through and `cmd_backfill_proposals`
    runs normally. Uses the TB-195 zero-records baseline (no
    operator_log entries / no briefings) so the underlying
    `backfill_proposals` call is a no-op; what we pin is the gate not
    blocking."""
    from ap2.cli import cmd_backfill_proposals

    cfg = _project(tmp_path)
    # init_project leaves Active empty; explicit assertion documents
    # the precondition.
    board = Board.load(cfg.tasks_file)
    assert list(board.iter_tasks("Active")) == []

    rc = cmd_backfill_proposals(cfg, Namespace(dry_run=False))
    assert rc == 0

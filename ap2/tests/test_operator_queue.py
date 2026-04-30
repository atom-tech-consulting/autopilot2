"""Tests for the operator queue (TB-131).

Covers: `do_operator_queue_append` (the queue-append handler), the daemon's
`drain_operator_queue` step, idempotent state-file bookkeeping, the
`ap2 status` queue-depth surface, and the privilege-pinning of the
`operator_queue_append` MCP tool + the `.cc-autopilot/operator_queue.*`
fenced paths.

The model: CLI / MM-handler appends to `.cc-autopilot/operator_queue.jsonl`,
the daemon's `_tick` first stage drains it under `board_file_lock`,
applied uuids are recorded in `operator_queue_state.json` so a crash
mid-drain doesn't double-apply, and an audit line lands in
`operator_log.md` per drained op.
"""
from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

import pytest

from ap2 import events, retry, tools
from ap2.board import Board
from ap2.config import Config
from ap2.init import init_project


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    init_project(tmp_path)
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()
    return cfg


def _unwrap(res: dict) -> dict:
    assert not res.get("isError"), res
    return json.loads(res["content"][0]["text"])


# ---------------------------------------------------------------------------
# do_operator_queue_append — write path shared by CLI + MCP


def test_queue_append_add_backlog_preallocates_id(cfg: Config):
    """`add_backlog` allocates a TB-N synchronously (so the operator can
    print it immediately) and writes the briefing file. Only the
    TASKS.md insertion is deferred until the daemon's drain.
    """
    res = tools.do_operator_queue_append(
        cfg,
        {
            "op": "add_backlog",
            "title": "deferred work",
            "description": "do the thing",
        },
    )
    body = _unwrap(res)
    # ID looks pre-allocated.
    assert body["task_id"].startswith("TB-")
    # Queue file has exactly one record.
    queue_path = tools.operator_queue_path(cfg)
    lines = [
        json.loads(ln)
        for ln in queue_path.read_text().splitlines()
        if ln.strip()
    ]
    assert len(lines) == 1
    rec = lines[0]
    assert rec["op"] == "add_backlog"
    assert rec["preallocated_task_id"] == body["task_id"]
    # TASKS.md is unchanged — the insertion is deferred.
    assert Board.load(cfg.tasks_file).find(body["task_id"]) is None
    # Briefing was written so the daemon's drain doesn't have to.
    brief_rel = rec["args"]["briefing_path"]
    assert brief_rel
    assert (cfg.project_root / brief_rel).exists()


def test_queue_append_emits_event(cfg: Config):
    tools.do_operator_queue_append(
        cfg, {"op": "add_backlog", "title": "evented"}
    )
    evts = events.tail(cfg.events_file, 5)
    appends = [e for e in evts if e["type"] == "operator_queue_append"]
    assert len(appends) == 1
    assert appends[0]["op"] == "add_backlog"
    assert appends[0]["task"].startswith("TB-")


def test_queue_append_rejects_unknown_op(cfg: Config):
    res = tools.do_operator_queue_append(cfg, {"op": "bogus"})
    assert res.get("isError")
    assert "unknown op" in res["content"][0]["text"]


def test_queue_append_rejects_unfreeze_on_non_frozen(cfg: Config):
    """Snapshot validation rejects obvious operator errors at append
    time — same UX as the pre-TB-131 direct-write path."""
    board = Board.load(cfg.tasks_file)
    board.add("Backlog", task_id="TB-77", title="not frozen")
    board.save()
    res = tools.do_operator_queue_append(
        cfg, {"op": "unfreeze", "task_id": "TB-77"}
    )
    assert res.get("isError")
    assert "not Frozen" in res["content"][0]["text"]


def test_queue_append_rejects_delete_active_without_force(cfg: Config):
    board = Board.load(cfg.tasks_file)
    board.add("Active", task_id="TB-40", title="running")
    board.save()
    res = tools.do_operator_queue_append(
        cfg, {"op": "delete", "task_id": "TB-40"}
    )
    assert res.get("isError")
    text = res["content"][0]["text"]
    assert "Active" in text
    assert "force" in text


def test_queue_append_allows_delete_active_with_force(cfg: Config):
    board = Board.load(cfg.tasks_file)
    board.add("Active", task_id="TB-41", title="zombie active")
    board.save()
    res = tools.do_operator_queue_append(
        cfg, {"op": "delete", "task_id": "TB-41", "force": True}
    )
    body = _unwrap(res)
    assert body["op"] == "delete"


def test_queue_append_rejects_unknown_task_id(cfg: Config):
    res = tools.do_operator_queue_append(
        cfg, {"op": "move_to_backlog", "task_id": "TB-9999"}
    )
    assert res.get("isError")
    assert "not on board" in res["content"][0]["text"]


# ---------------------------------------------------------------------------
# drain_operator_queue — daemon-side replay


def test_drain_applies_pending_ops(cfg: Config):
    """Multiple queued ops drain in append order; TASKS.md ends up in
    the expected post-state."""
    r1 = _unwrap(tools.do_operator_queue_append(
        cfg, {"op": "add_backlog", "title": "first"}
    ))
    r2 = _unwrap(tools.do_operator_queue_append(
        cfg, {"op": "add_backlog", "title": "second"}
    ))
    summary = tools.drain_operator_queue(cfg)
    assert summary["applied"] == 2

    board = Board.load(cfg.tasks_file)
    assert board.find(r1["task_id"])[0] == "Backlog"
    assert board.find(r2["task_id"])[0] == "Backlog"


def test_drain_is_idempotent_via_uuid(cfg: Config):
    """Replaying the same record (e.g. crash mid-drain that resumed)
    does not double-apply: the second drain finds the uuid already in
    operator_queue_state.json and skips it.
    """
    # Stage a single op.
    body = _unwrap(tools.do_operator_queue_append(
        cfg, {"op": "add_backlog", "title": "once"}
    ))
    tid = body["task_id"]

    # First drain applies it.
    s1 = tools.drain_operator_queue(cfg)
    assert s1["applied"] == 1

    # Re-append the same record (simulating a crash that left the
    # queue file un-compacted before applied state was persisted —
    # but the state file IS persisted, so the uuid lookup catches it).
    queue_path = tools.operator_queue_path(cfg)
    state_path = tools.operator_queue_state_path(cfg)
    assert state_path.exists()
    # Find the original record by uuid in the events stream.
    appends = [
        e for e in events.tail(cfg.events_file, 10)
        if e["type"] == "operator_queue_append"
    ]
    original_uuid = appends[0]["uuid"]
    # Replay the SAME uuid by hand — would be a no-op.
    rec = {
        "uuid": original_uuid,
        "op": "add_backlog",
        "args": {
            "task_id": tid,
            "title": "replay",
            "tags": [],
            "description": "",
            "briefing_path": None,
        },
        "ts": "2025-01-01T00:00:00Z",
    }
    with queue_path.open("a") as f:
        f.write(json.dumps(rec) + "\n")

    s2 = tools.drain_operator_queue(cfg)
    # Skipped — uuid was already applied.
    assert s2["applied"] == 0

    # Board still has exactly one task.
    board = Board.load(cfg.tasks_file)
    backlog = list(board.iter_tasks("Backlog"))
    assert sum(1 for t in backlog if t.id == tid) == 1


def test_drain_unfreeze_resets_retry_counter_and_emits_event(cfg: Config):
    """Drain replays the unfreeze semantic: move-to-Backlog +
    reset_attempt + task_unfrozen event."""
    board = Board.load(cfg.tasks_file)
    board.add("Frozen", task_id="TB-200", title="frozen w retries")
    board.save()
    retry.bump_attempt(cfg.retry_state_file, "TB-200")
    retry.bump_attempt(cfg.retry_state_file, "TB-200")
    retry.bump_attempt(cfg.retry_state_file, "TB-200")
    assert retry.attempt_count(cfg.retry_state_file, "TB-200") == 3

    tools.do_operator_queue_append(
        cfg, {"op": "unfreeze", "task_id": "TB-200"}
    )
    tools.drain_operator_queue(cfg)

    assert Board.load(cfg.tasks_file).find("TB-200")[0] == "Backlog"
    assert retry.attempt_count(cfg.retry_state_file, "TB-200") == 0
    evts = events.tail(cfg.events_file, 10)
    unfrozen = [e for e in evts if e["type"] == "task_unfrozen"]
    assert len(unfrozen) == 1
    assert unfrozen[0]["task"] == "TB-200"


def test_drain_delete_emits_audit_event(cfg: Config):
    """Drain emits `task_deleted` so the audit trail matches the pre-
    TB-131 synchronous-delete behavior."""
    board = Board.load(cfg.tasks_file)
    board.add("Frozen", task_id="TB-220", title="goner")
    board.save()
    tools.do_operator_queue_append(
        cfg, {"op": "delete", "task_id": "TB-220"}
    )
    tools.drain_operator_queue(cfg)

    assert Board.load(cfg.tasks_file).find("TB-220") is None
    evts = events.tail(cfg.events_file, 10)
    deleted = [e for e in evts if e["type"] == "task_deleted"]
    assert len(deleted) == 1
    assert deleted[0]["task"] == "TB-220"
    assert deleted[0]["section"] == "Frozen"
    assert deleted[0]["title"] == "goner"


def test_drain_appends_audit_line_to_operator_log(cfg: Config):
    """Each drained op appends `applied operator-queued <op> → TB-N`
    to operator_log.md so the operator-decisions surface tracks
    queue activity without bloating the queue file."""
    body = _unwrap(tools.do_operator_queue_append(
        cfg, {"op": "add_backlog", "title": "audited add"}
    ))
    tools.drain_operator_queue(cfg)
    log = (cfg.project_root / ".cc-autopilot" / "operator_log.md").read_text()
    assert "applied operator-queued add_backlog" in log
    assert body["task_id"] in log


def test_drain_emits_drained_event_with_count(cfg: Config):
    tools.do_operator_queue_append(cfg, {"op": "add_backlog", "title": "a"})
    tools.do_operator_queue_append(cfg, {"op": "add_backlog", "title": "b"})
    tools.drain_operator_queue(cfg)
    evts = events.tail(cfg.events_file, 10)
    drained = [e for e in evts if e["type"] == "operator_queue_drained"]
    assert len(drained) == 1
    assert drained[0]["applied"] == 2


def test_drain_compacts_queue_file_after_apply(cfg: Config):
    """After a successful drain the queue file shrinks — applied uuids
    are dropped so the file doesn't grow unboundedly. (We don't
    truncate the state file: it's the durable applied-uuid record.)"""
    tools.do_operator_queue_append(cfg, {"op": "add_backlog", "title": "x"})
    tools.do_operator_queue_append(cfg, {"op": "add_backlog", "title": "y"})
    qpath = tools.operator_queue_path(cfg)
    assert len([ln for ln in qpath.read_text().splitlines() if ln.strip()]) == 2
    tools.drain_operator_queue(cfg)
    # Empty (or whitespace-only) — both records were applied.
    assert qpath.read_text().strip() == ""


def test_drain_no_pending_returns_zero(cfg: Config):
    summary = tools.drain_operator_queue(cfg)
    assert summary["applied"] == 0


def test_drain_failure_in_one_op_doesnt_halt_others(cfg: Config):
    """If a queued op references a task that vanished, the drain logs
    `operator_queue_error` and proceeds. The valid op behind it still
    applies."""
    # Queue one valid + one bogus op.
    tools.do_operator_queue_append(
        cfg, {"op": "add_backlog", "title": "valid"}
    )
    # Hand-write a bogus record referencing a non-existent task. We
    # bypass the snapshot validation that the public API would do
    # because we want to test the drain-side error path specifically.
    qpath = tools.operator_queue_path(cfg)
    bogus = {
        "uuid": "bogus-uuid-001",
        "op": "move_to_backlog",
        "args": {"task_id": "TB-99999"},
        "ts": "2025-01-01T00:00:00Z",
    }
    with qpath.open("a") as f:
        f.write(json.dumps(bogus) + "\n")
    # And one more valid op AFTER the bogus one.
    tools.do_operator_queue_append(
        cfg, {"op": "add_backlog", "title": "after bogus"}
    )

    summary = tools.drain_operator_queue(cfg)
    # Two valid ops applied; bogus one logged as error.
    assert summary["applied"] == 2
    evts = events.tail(cfg.events_file, 20)
    errors = [e for e in evts if e["type"] == "operator_queue_error"]
    assert any(e.get("uuid") == "bogus-uuid-001" for e in errors)


# ---------------------------------------------------------------------------
# operator_queue_pending_count — `ap2 status` surface


def test_pending_count_reflects_queue_depth(cfg: Config):
    assert tools.operator_queue_pending_count(cfg) == 0
    tools.do_operator_queue_append(cfg, {"op": "add_backlog", "title": "p1"})
    tools.do_operator_queue_append(cfg, {"op": "add_backlog", "title": "p2"})
    assert tools.operator_queue_pending_count(cfg) == 2
    tools.drain_operator_queue(cfg)
    assert tools.operator_queue_pending_count(cfg) == 0


# ---------------------------------------------------------------------------
# Privilege pinning — fences + tool wiring


def test_operator_queue_paths_are_fenced_from_task_agents():
    """Task agents must not write directly to the queue or its state
    file — they're operator-mediated daemon-drained surfaces."""
    assert ".cc-autopilot/operator_queue.jsonl" in tools.TASK_AGENT_FENCED_PATHS
    assert ".cc-autopilot/operator_queue_state.json" in tools.TASK_AGENT_FENCED_PATHS


def test_operator_queue_append_is_in_control_agent_tools_only():
    """The MCP tool is for control agents (mostly the MM handler when a
    task is in flight). Task agents go through `report_result`; they
    have no business mutating the operator's board.
    """
    assert "mcp__autopilot__operator_queue_append" in tools.CONTROL_AGENT_TOOLS
    assert "mcp__autopilot__operator_queue_append" not in tools.TASK_AGENT_TOOLS


def test_operator_queue_append_survives_mm_handler_restricted_filter():
    """The whole point of TB-131 from the MM-handler angle: this tool
    must remain available to the handler even during in-flight runs
    (where `cron_edit` and `ideation_state_write` are filtered out
    of MM_HANDLER_TOOLS_RESTRICTED to avoid mid-task interference).
    The queue is exactly the path that side-steps the rollback +
    stale-snapshot races, so it MUST stay in the restricted set.
    """
    assert "mcp__autopilot__operator_queue_append" in tools.MM_HANDLER_TOOLS_RESTRICTED


# ---------------------------------------------------------------------------
# CLI integration — `ap2 add` prints the pre-allocated id with "queued"


def test_cmd_add_prints_queued_message(cfg: Config, capsys):
    from ap2.cli import cmd_add

    rc = cmd_add(
        cfg,
        Namespace(
            title="hello world",
            section="Backlog",
            tags=[],
            description="",
            briefing_file=None,
            no_verify=False,
        ),
    )
    assert rc == 0
    out = capsys.readouterr().out
    # UX preserved per TB-131 scope (3): print TB-N + "(queued; will land at next tick)".
    assert "queued" in out
    assert "TB-" in out


def test_cmd_status_surfaces_pending_queue_depth(cfg: Config, capsys, monkeypatch):
    """`ap2 status` shows `pending: N operator op(s)` when ops are queued."""
    from ap2.cli import cmd_status

    tools.do_operator_queue_append(cfg, {"op": "add_backlog", "title": "p1"})
    tools.do_operator_queue_append(cfg, {"op": "add_backlog", "title": "p2"})
    rc = cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "pending:" in out
    assert "2 operator ops" in out


def test_cmd_status_omits_pending_line_when_queue_empty(cfg: Config, capsys):
    from ap2.cli import cmd_status

    rc = cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "pending:" not in out


def test_cmd_status_json_includes_operator_queue_pending(cfg: Config, capsys):
    from ap2.cli import cmd_status

    tools.do_operator_queue_append(cfg, {"op": "add_backlog", "title": "j"})
    rc = cmd_status(cfg, Namespace(json=True))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["operator_queue_pending"] == 1

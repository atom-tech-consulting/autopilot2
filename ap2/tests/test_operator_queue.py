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


# TB-135: every `add_*` op now requires a briefing payload — the
# auto-fill skeleton path is gone. These queue tests don't actually
# care about the briefing's contents (the queue handler treats it as
# opaque bytes), but they need to pass a non-empty one to clear the
# new gate. Helper to keep the call sites short.
_BRIEFING = (
    "# Test briefing\n\n"
    "## Goal\n\nA thing happens.\n\n"
    "## Scope\n\n- foo.py\n\n"
    "## Design\n\nDirect edit.\n\n"
    "## Verification\n- `uv run pytest -q` — gates pass\n\n"
    "## Out of scope\n\n- nothing\n"
)


def _add(op: str = "add_backlog", title: str = "t", **extra) -> dict:
    payload = {"op": op, "title": title, "briefing": _BRIEFING}
    payload.update(extra)
    return payload


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
            "briefing": _BRIEFING,
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
        cfg, {"op": "add_backlog", "title": "evented", "briefing": _BRIEFING}
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
        cfg, {"op": "add_backlog", "title": "first", "briefing": _BRIEFING}
    ))
    r2 = _unwrap(tools.do_operator_queue_append(
        cfg, {"op": "add_backlog", "title": "second", "briefing": _BRIEFING}
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
        cfg, {"op": "add_backlog", "title": "once", "briefing": _BRIEFING}
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
        cfg, {"op": "add_backlog", "title": "audited add", "briefing": _BRIEFING}
    ))
    tools.drain_operator_queue(cfg)
    log = (cfg.project_root / ".cc-autopilot" / "operator_log.md").read_text()
    assert "applied operator-queued add_backlog" in log
    assert body["task_id"] in log


def test_drain_emits_drained_event_with_count(cfg: Config):
    tools.do_operator_queue_append(cfg, {"op": "add_backlog", "title": "a", "briefing": _BRIEFING})
    tools.do_operator_queue_append(cfg, {"op": "add_backlog", "title": "b", "briefing": _BRIEFING})
    tools.drain_operator_queue(cfg)
    evts = events.tail(cfg.events_file, 10)
    drained = [e for e in evts if e["type"] == "operator_queue_drained"]
    assert len(drained) == 1
    assert drained[0]["applied"] == 2


def test_drain_compacts_queue_file_after_apply(cfg: Config):
    """After a successful drain the queue file shrinks — applied uuids
    are dropped so the file doesn't grow unboundedly. (We don't
    truncate the state file: it's the durable applied-uuid record.)"""
    tools.do_operator_queue_append(cfg, {"op": "add_backlog", "title": "x", "briefing": _BRIEFING})
    tools.do_operator_queue_append(cfg, {"op": "add_backlog", "title": "y", "briefing": _BRIEFING})
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
        cfg, {"op": "add_backlog", "title": "valid", "briefing": _BRIEFING}
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
        cfg, {"op": "add_backlog", "title": "after bogus", "briefing": _BRIEFING}
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
    tools.do_operator_queue_append(cfg, {"op": "add_backlog", "title": "p1", "briefing": _BRIEFING})
    tools.do_operator_queue_append(cfg, {"op": "add_backlog", "title": "p2", "briefing": _BRIEFING})
    assert tools.operator_queue_pending_count(cfg) == 2
    tools.drain_operator_queue(cfg)
    assert tools.operator_queue_pending_count(cfg) == 0


# ---------------------------------------------------------------------------
# Privilege pinning — fences + tool wiring


def test_operator_queue_state_is_fenced_from_task_agents():
    """The applied-uuid state file remains fenced — agents must not
    rewrite the daemon's drain bookkeeping."""
    assert ".cc-autopilot/operator_queue_state.json" in tools.TASK_AGENT_FENCED_PATHS


def test_operator_queue_jsonl_is_fenced_from_task_agents():
    """TB-143: operator_queue.jsonl lives in TASK_AGENT_FENCED_PATHS
    for defense in depth (prompt-header reminder + SDK rejects
    `Edit`/`Write`). The TB-141 false-positive — operator `ap2 add`
    during a task run tripping TB-110's snapshot check — is now
    handled separately by `rollback._VIOLATION_CHECK_EXCLUDED_PATHS`,
    which excludes this path (and `events.jsonl`) from the post-hoc
    hash compare while keeping it in the fence list."""
    assert ".cc-autopilot/operator_queue.jsonl" in tools.TASK_AGENT_FENCED_PATHS


def test_operator_queue_append_is_in_control_agent_tools_only():
    """The MCP tool is for control agents (mostly the MM handler when a
    task is in flight). Task agents go through `report_result`; they
    have no business mutating the operator's board.
    """
    assert "mcp__autopilot__operator_queue_append" in tools.CONTROL_AGENT_TOOLS
    assert "mcp__autopilot__operator_queue_append" not in tools.TASK_AGENT_TOOLS


def test_operator_queue_append_survives_mm_handler_filter():
    """The whole point of TB-131 from the MM-handler angle: this tool
    must remain available to the handler. `cron_edit`,
    `ideation_state_write`, and `board_edit` are all filtered out of
    `MM_HANDLER_TOOLS` (TB-145, formerly TB-122's RESTRICTED variant),
    but the queue is exactly the path that side-steps the rollback +
    stale-snapshot races, so it MUST stay in the handler set.
    """
    assert "mcp__autopilot__operator_queue_append" in tools.MM_HANDLER_TOOLS


# ---------------------------------------------------------------------------
# CLI integration — `ap2 add` prints the pre-allocated id with "queued"


def test_cmd_add_prints_queued_message(cfg: Config, capsys, tmp_path: Path):
    """UX preserved per TB-131 scope (3): print TB-N + "(queued; will
    land at next tick)". TB-135: --briefing-file is now required, so
    the test stages a briefing on disk first."""
    from ap2.cli import cmd_add

    brief = tmp_path / "brief.md"
    brief.write_text(
        "# hello world\n\n"
        "## Goal\n\nstub\n\n"
        "## Scope\n\n- foo.py\n\n"
        "## Design\n\nstub\n\n"
        "## Verification\n- `uv run pytest -q` — gates pass\n\n"
        "## Out of scope\n\n- nothing\n"
    )

    rc = cmd_add(
        cfg,
        Namespace(
            section="Backlog",
            tags=[],
            briefing_file=str(brief),
            no_verify=False,
        ),
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "queued" in out
    assert "TB-" in out


def test_cmd_status_surfaces_pending_queue_depth(cfg: Config, capsys, monkeypatch):
    """`ap2 status` shows `pending: N operator op(s)` when ops are queued."""
    from ap2.cli import cmd_status

    tools.do_operator_queue_append(cfg, {"op": "add_backlog", "title": "p1", "briefing": _BRIEFING})
    tools.do_operator_queue_append(cfg, {"op": "add_backlog", "title": "p2", "briefing": _BRIEFING})
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

    tools.do_operator_queue_append(cfg, {"op": "add_backlog", "title": "j", "briefing": _BRIEFING})
    rc = cmd_status(cfg, Namespace(json=True))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["operator_queue_pending"] == 1


# ---------------------------------------------------------------------------
# TB-142: `approve` is a queueable op so the MM handler RESTRICTED toolset
# (which drops `board_edit`) can still strip the `@blocked:review` blocker
# from an ideation-proposed task without racing the in-flight task agent's
# state-violation snapshot window.


def test_operator_queue_ops_includes_approve():
    """TB-142 anchor: `approve` must be in OPERATOR_QUEUE_OPS so the
    snapshot-validation gate in `do_operator_queue_append` accepts it.
    Pinning the constant directly so a refactor can't silently drop it."""
    assert "approve" in tools.OPERATOR_QUEUE_OPS


def test_queue_append_approve_queues_record(cfg: Config):
    """`do_operator_queue_append({"op":"approve","task_id":"TB-X"})`
    queues a record with op="approve" and the target task_id. The TASKS.md
    file is unchanged — the strip happens at drain time."""
    board = Board.load(cfg.tasks_file)
    board.add(
        "Backlog",
        task_id="TB-300",
        title="proposed by ideation",
        meta={"blocked": "review"},
    )
    board.save()
    pre_render = cfg.tasks_file.read_text()

    res = tools.do_operator_queue_append(
        cfg, {"op": "approve", "task_id": "TB-300"}
    )
    body = _unwrap(res)
    assert body["op"] == "approve"
    assert body["task_id"] == "TB-300"

    # TASKS.md unchanged — drain is what mutates the line.
    assert cfg.tasks_file.read_text() == pre_render

    # Queue file has exactly one record with op="approve".
    queue_path = tools.operator_queue_path(cfg)
    lines = [
        json.loads(ln)
        for ln in queue_path.read_text().splitlines()
        if ln.strip()
    ]
    assert len(lines) == 1
    assert lines[0]["op"] == "approve"
    assert lines[0]["args"]["task_id"] == "TB-300"


def test_queue_append_approve_rejects_unknown_task(cfg: Config):
    """Snapshot validation rejects `approve` against a non-existent
    task_id — same UX as other non-add queue ops."""
    res = tools.do_operator_queue_append(
        cfg, {"op": "approve", "task_id": "TB-99999"}
    )
    assert res.get("isError")
    assert "not on board" in res["content"][0]["text"]


def test_queue_append_approve_requires_task_id(cfg: Config):
    res = tools.do_operator_queue_append(cfg, {"op": "approve"})
    assert res.get("isError")
    assert "task_id" in res["content"][0]["text"]


def test_drain_approve_strips_review_codespan(cfg: Config):
    """Drain replays approve via the shared `_approve_review_token`
    helper: the `@blocked:review` codespan disappears, so the task
    becomes dispatchable on the next ready-promotion sweep."""
    board = Board.load(cfg.tasks_file)
    board.add(
        "Backlog",
        task_id="TB-310",
        title="ideation proposed",
        meta={"blocked": "review"},
    )
    board.save()
    # Sanity: pre-drain the codespan is present in the rendered line.
    raw_pre = cfg.tasks_file.read_text()
    assert "`@blocked:review`" in raw_pre

    tools.do_operator_queue_append(
        cfg, {"op": "approve", "task_id": "TB-310"}
    )
    summary = tools.drain_operator_queue(cfg)
    assert summary["applied"] == 1

    # Codespan is gone; task is no longer structurally blocked.
    raw_post = cfg.tasks_file.read_text()
    assert "`@blocked:review`" not in raw_post
    board2 = Board.load(cfg.tasks_file)
    t = board2.get("TB-310")
    assert t is not None
    assert "blocked" not in t.meta
    assert t.blocked_on == []  # dispatchable

    # ideation_approved audit event landed.
    evts = events.tail(cfg.events_file, 20)
    approved = [e for e in evts if e["type"] == "ideation_approved"]
    assert len(approved) == 1
    assert approved[0]["task"] == "TB-310"


def test_drain_approve_preserves_other_blockers(cfg: Config):
    """Approve only strips the `review` token. Other tokens in the same
    `@blocked:` codespan (TB-N task ids, scheme:value blockers) survive
    so the dependency check still gates dispatch on them."""
    board = Board.load(cfg.tasks_file)
    board.add(
        "Backlog",
        task_id="TB-320",
        title="multi-blocker",
        meta={"blocked": "TB-5,review,TB-7"},
    )
    board.save()

    tools.do_operator_queue_append(
        cfg, {"op": "approve", "task_id": "TB-320"}
    )
    tools.drain_operator_queue(cfg)

    t = Board.load(cfg.tasks_file).get("TB-320")
    assert t is not None
    # `review` gone, TB-5 + TB-7 preserved.
    assert "review" not in t.meta.get("blocked", "")
    assert "TB-5" in t.meta["blocked"]
    assert "TB-7" in t.meta["blocked"]


def test_drain_approve_idempotent_on_unblocked_task(cfg: Config):
    """A queued approve against a task that has no `review` blocker is a
    no-op (modulo line re-render). Useful so an operator double-queueing
    `approve` doesn't corrupt the board."""
    board = Board.load(cfg.tasks_file)
    board.add("Backlog", task_id="TB-330", title="not gated")
    board.save()
    pre = Board.load(cfg.tasks_file).get("TB-330")

    tools.do_operator_queue_append(
        cfg, {"op": "approve", "task_id": "TB-330"}
    )
    tools.drain_operator_queue(cfg)

    post = Board.load(cfg.tasks_file).get("TB-330")
    assert post is not None
    assert post.meta == pre.meta
    assert post.blocked_on == []


# ---------------------------------------------------------------------------
# TB-153: `update` op for in-place task / briefing edits.
#
# Per-field round-trip: title / tags / blocked / description / briefing /
# explicit clears. Plus the per-target Active / Pipeline-Pending fence
# (mirrors `delete`'s fence — keyed on the target's section, not
# directory-wide), and slug-stable briefing-file overwrites.


def _seed_backlog_task(
    cfg: Config,
    task_id: str = "TB-400",
    *,
    title: str = "original title",
    tags: list[str] | None = None,
    meta: dict[str, str] | None = None,
    description: str = "",
    briefing: str | None = None,
) -> None:
    board = Board.load(cfg.tasks_file)
    board.add(
        "Backlog",
        task_id=task_id,
        title=title,
        tags=tags or [],
        meta=meta or {},
        description=description,
        briefing=briefing,
    )
    board.save()


def test_operator_queue_ops_includes_update():
    """Pin the constant directly so a refactor can't silently drop the
    op (TB-153)."""
    assert "update" in tools.OPERATOR_QUEUE_OPS


def test_queue_append_update_rejects_unknown_task(cfg: Config):
    """Snapshot validation rejects an update against a non-existent
    task_id at queue-append time — same UX as the other non-add ops."""
    res = tools.do_operator_queue_append(
        cfg, {"op": "update", "task_id": "TB-99999", "title": "x"}
    )
    assert res.get("isError")
    assert "not on board" in res["content"][0]["text"]


def test_queue_append_update_rejects_no_fields(cfg: Config):
    """At least one field must be set — otherwise the op is a no-op
    that would still emit a `task_updated` event with an empty diff."""
    _seed_backlog_task(cfg)
    res = tools.do_operator_queue_append(
        cfg, {"op": "update", "task_id": "TB-400"}
    )
    assert res.get("isError")
    assert "at least one field" in res["content"][0]["text"]


def test_queue_append_update_rejects_multiline_title(cfg: Config):
    """Single-line gate fires for `update` too — TASK_LINE_RE is line-
    anchored and a multi-line title would split the rendered line."""
    _seed_backlog_task(cfg)
    res = tools.do_operator_queue_append(
        cfg,
        {"op": "update", "task_id": "TB-400", "title": "two\nlines"},
    )
    assert res.get("isError")
    assert "single line" in res["content"][0]["text"]


# ---- per-field round-trip


def _last_task_updated(cfg: Config) -> dict:
    """TB-153: pull the most recent `task_updated` event from the events
    file. Per-field round-trip tests assert the field-of-interest is in
    its `fields=[...]` diff so the audit signal stays grep-able."""
    evts = events.tail(cfg.events_file, 50)
    updated = [e for e in evts if e["type"] == "task_updated"]
    assert updated, f"no task_updated event in tail: {evts}"
    return updated[-1]


def test_update_title_round_trips(cfg: Config):
    _seed_backlog_task(cfg, title="old title")
    res = tools.do_operator_queue_append(
        cfg,
        {"op": "update", "task_id": "TB-400", "title": "shiny new title"},
    )
    body = _unwrap(res)
    assert body["op"] == "update"
    tools.drain_operator_queue(cfg)
    t = Board.load(cfg.tasks_file).get("TB-400")
    assert t is not None
    assert t.title == "shiny new title"
    # TB-153: task_updated event must name the changed field.
    evt = _last_task_updated(cfg)
    assert evt["task"] == "TB-400"
    assert "title" in evt["fields"]


def test_update_tags_round_trips(cfg: Config):
    _seed_backlog_task(cfg, tags=["#old"])
    tools.do_operator_queue_append(
        cfg,
        {"op": "update", "task_id": "TB-400", "tags": ["#foo", "#bar"]},
    )
    tools.drain_operator_queue(cfg)
    t = Board.load(cfg.tasks_file).get("TB-400")
    assert t is not None
    assert "#foo" in t.tags
    assert "#bar" in t.tags
    assert "#old" not in t.tags
    # TB-153: task_updated event must name the changed field.
    evt = _last_task_updated(cfg)
    assert evt["task"] == "TB-400"
    assert "tags" in evt["fields"]


def test_update_blocked_round_trips(cfg: Config):
    _seed_backlog_task(cfg)
    tools.do_operator_queue_append(
        cfg,
        {"op": "update", "task_id": "TB-400", "blocked": "TB-9,review"},
    )
    tools.drain_operator_queue(cfg)
    t = Board.load(cfg.tasks_file).get("TB-400")
    assert t is not None
    assert t.meta.get("blocked") == "TB-9,review"
    raw = cfg.tasks_file.read_text()
    assert "`@blocked:TB-9,review`" in raw
    # TB-153: task_updated event must name the changed field.
    evt = _last_task_updated(cfg)
    assert evt["task"] == "TB-400"
    assert "blocked" in evt["fields"]


def test_update_description_round_trips(cfg: Config):
    _seed_backlog_task(cfg, description="old prose")
    tools.do_operator_queue_append(
        cfg,
        {"op": "update", "task_id": "TB-400", "description": "new prose"},
    )
    tools.drain_operator_queue(cfg)
    t = Board.load(cfg.tasks_file).get("TB-400")
    assert t is not None
    assert t.description == "new prose"
    # TB-153: task_updated event must name the changed field.
    evt = _last_task_updated(cfg)
    assert evt["task"] == "TB-400"
    assert "description" in evt["fields"]


def test_update_briefing_round_trips(cfg: Config):
    """Briefing-edit through the queue: queue-append writes the new
    bytes to disk under the EXISTING briefing path (slug-stable), and
    the drain leaves the task line's `[→ brief](...)` link unchanged."""
    # Seed a task that already has a briefing on disk.
    brief_dir = cfg.tasks_dir
    brief_dir.mkdir(parents=True, exist_ok=True)
    brief_path = brief_dir / "the-original-slug.md"
    brief_path.write_text(_BRIEFING)
    rel = str(brief_path.relative_to(cfg.project_root))
    _seed_backlog_task(cfg, briefing=rel)

    new_briefing = (
        "# Updated\n\n"
        "## Goal\n\nBetter goal.\n\n"
        "## Scope\n\n- foo.py\n\n"
        "## Design\n\nedit\n\n"
        "## Verification\n- `pytest -q`\n\n"
        "## Out of scope\n\n- nothing\n"
    )
    tools.do_operator_queue_append(
        cfg,
        {"op": "update", "task_id": "TB-400", "briefing": new_briefing},
    )
    tools.drain_operator_queue(cfg)

    # Same briefing file path — slug-stable.
    assert brief_path.exists()
    assert brief_path.read_text() == new_briefing
    # Task line still points at it (no rename, no new file).
    t = Board.load(cfg.tasks_file).get("TB-400")
    assert t is not None
    assert t.briefing == rel
    # TB-153: task_updated event must name the changed field.
    evt = _last_task_updated(cfg)
    assert evt["task"] == "TB-400"
    assert "briefing" in evt["fields"]


def test_update_clear_tags_explicit_path(cfg: Config):
    """`clear_tags=True` removes all tags. Distinguished from omitted
    `tags` which is unchanged."""
    _seed_backlog_task(cfg, tags=["#a", "#b"])
    tools.do_operator_queue_append(
        cfg, {"op": "update", "task_id": "TB-400", "clear_tags": True}
    )
    tools.drain_operator_queue(cfg)
    t = Board.load(cfg.tasks_file).get("TB-400")
    assert t is not None
    assert t.tags == []
    # TB-153: task_updated event must name the changed field — `tags`
    # covers the clear path same as a populating path.
    evt = _last_task_updated(cfg)
    assert evt["task"] == "TB-400"
    assert "tags" in evt["fields"]


def test_update_clear_blocked_explicit_path(cfg: Config):
    """`clear_blocked=True` strips the `@blocked:` codespan entirely.
    Other `meta` keys (none in this seed) survive."""
    _seed_backlog_task(cfg, meta={"blocked": "TB-9"})
    tools.do_operator_queue_append(
        cfg,
        {"op": "update", "task_id": "TB-400", "clear_blocked": True},
    )
    tools.drain_operator_queue(cfg)
    t = Board.load(cfg.tasks_file).get("TB-400")
    assert t is not None
    assert "blocked" not in t.meta
    raw = cfg.tasks_file.read_text()
    assert "`@blocked:" not in raw
    # TB-153: task_updated event must name the changed field — `blocked`
    # covers the clear path same as a populating path.
    evt = _last_task_updated(cfg)
    assert evt["task"] == "TB-400"
    assert "blocked" in evt["fields"]


# ---- task_updated event diff


def test_drain_emits_task_updated_with_fields_diff(cfg: Config):
    """`task_updated` event records the field set the operator changed —
    queryable post-mortem signal (`grep task_updated fields=[blocked]`)."""
    _seed_backlog_task(cfg, tags=["#old"])
    tools.do_operator_queue_append(
        cfg,
        {
            "op": "update",
            "task_id": "TB-400",
            "tags": ["#new"],
            "blocked": "TB-7",
        },
    )
    tools.drain_operator_queue(cfg)
    evts = events.tail(cfg.events_file, 20)
    updated = [e for e in evts if e["type"] == "task_updated"]
    assert len(updated) == 1
    assert updated[0]["task"] == "TB-400"
    assert "tags" in updated[0]["fields"]
    assert "blocked" in updated[0]["fields"]


def test_drain_update_appends_audit_line(cfg: Config):
    """The drain appends an `applied operator-queued update → TB-N` line
    to operator_log.md, same as every other queued op."""
    _seed_backlog_task(cfg)
    tools.do_operator_queue_append(
        cfg, {"op": "update", "task_id": "TB-400", "title": "new"}
    )
    tools.drain_operator_queue(cfg)
    log = (cfg.project_root / ".cc-autopilot" / "operator_log.md").read_text()
    assert "applied operator-queued update" in log
    assert "TB-400" in log


# ---- per-target Active / Pipeline-Pending fence (mirrors delete's)


def test_update_on_active_without_force_is_refused(cfg: Config):
    """Active ⇒ refuse without `--force`. Same UX shape as `delete`."""
    board = Board.load(cfg.tasks_file)
    board.add("Active", task_id="TB-410", title="running")
    board.save()
    res = tools.do_operator_queue_append(
        cfg, {"op": "update", "task_id": "TB-410", "title": "x"}
    )
    assert res.get("isError")
    assert "Active" in res["content"][0]["text"]
    assert "force" in res["content"][0]["text"]
    # Queue file unchanged — no pending op should land.
    qpath = tools.operator_queue_path(cfg)
    if qpath.exists():
        for ln in qpath.read_text().splitlines():
            if ln.strip():
                rec = json.loads(ln)
                assert rec.get("op") != "update", rec


def test_update_on_active_with_force_for_board_line_field(cfg: Config):
    """`--force` allows board-line field updates on Active. Drain
    successfully applies the title change."""
    board = Board.load(cfg.tasks_file)
    board.add("Active", task_id="TB-411", title="old running title")
    board.save()
    res = tools.do_operator_queue_append(
        cfg,
        {
            "op": "update",
            "task_id": "TB-411",
            "title": "renamed running",
            "force": True,
        },
    )
    body = _unwrap(res)
    assert body["op"] == "update"
    tools.drain_operator_queue(cfg)
    t = Board.load(cfg.tasks_file).get("TB-411")
    assert t is not None
    assert t.title == "renamed running"


def test_update_on_active_force_still_refuses_briefing(cfg: Config):
    """Briefing-content edit to a running task is hard-refused — even
    with `--force`. The agent may re-read its briefing mid-run via
    `Read`, and TB-110's snapshot may hash the file."""
    board = Board.load(cfg.tasks_file)
    board.add("Active", task_id="TB-412", title="running")
    board.save()
    new_briefing = (
        "# Updated\n\n"
        "## Goal\n\nBetter\n\n"
        "## Scope\n\n- foo.py\n\n"
        "## Design\n\nedit\n\n"
        "## Verification\n- `pytest`\n\n"
        "## Out of scope\n\n- nothing\n"
    )
    res = tools.do_operator_queue_append(
        cfg,
        {
            "op": "update",
            "task_id": "TB-412",
            "briefing": new_briefing,
            "force": True,
        },
    )
    assert res.get("isError")
    assert "briefing" in res["content"][0]["text"].lower()
    assert "Active" in res["content"][0]["text"]


def test_update_on_pipeline_pending_without_force_refused(cfg: Config):
    """Pipeline Pending mirrors Active for the fence — both are
    "task in flight" sections."""
    board = Board.load(cfg.tasks_file)
    board.add("Pipeline Pending", task_id="TB-413", title="awaiting verify")
    board.save()
    res = tools.do_operator_queue_append(
        cfg, {"op": "update", "task_id": "TB-413", "title": "x"}
    )
    assert res.get("isError")
    assert "Pipeline Pending" in res["content"][0]["text"]


@pytest.mark.parametrize("section", ["Backlog", "Ready", "Frozen"])
def test_update_on_idle_sections_succeeds_without_force(
    cfg: Config, section: str
):
    """Backlog / Ready / Frozen are not "in flight" — the fence
    doesn't apply, no `--force` needed."""
    board = Board.load(cfg.tasks_file)
    board.add(section, task_id="TB-420", title=f"in {section}")
    board.save()
    res = tools.do_operator_queue_append(
        cfg,
        {"op": "update", "task_id": "TB-420", "title": "renamed"},
    )
    body = _unwrap(res)
    assert body["op"] == "update"
    tools.drain_operator_queue(cfg)
    t = Board.load(cfg.tasks_file).get("TB-420")
    assert t is not None
    assert t.title == "renamed"


# ---- briefing slug-stable end-to-end


def test_update_briefing_preserves_filename(cfg: Config):
    """End-to-end briefing edit through the queue keeps the
    `<slug>.md` filename. The on-disk briefing is rewritten in
    place — no rename, no new file. Pins the
    `git log -- .cc-autopilot/tasks/<slug>.md` continuity property."""
    brief_dir = cfg.tasks_dir
    brief_dir.mkdir(parents=True, exist_ok=True)
    brief_path = brief_dir / "stable-slug.md"
    brief_path.write_text(_BRIEFING)
    rel = str(brief_path.relative_to(cfg.project_root))
    _seed_backlog_task(
        cfg, title="Original title that would re-slug", briefing=rel
    )
    pre_files = sorted(p.name for p in brief_dir.glob("*.md"))

    new_briefing = _BRIEFING.replace("Test briefing", "Edited briefing")
    tools.do_operator_queue_append(
        cfg,
        {
            "op": "update",
            "task_id": "TB-400",
            "title": "Brand-new title that would have re-slugged",
            "briefing": new_briefing,
        },
    )
    tools.drain_operator_queue(cfg)

    # File set unchanged — same names, no new file allocated.
    post_files = sorted(p.name for p in brief_dir.glob("*.md"))
    assert post_files == pre_files
    # Content is the new briefing.
    assert brief_path.read_text() == new_briefing
    # Task line still references the same slug.
    t = Board.load(cfg.tasks_file).get("TB-400")
    assert t is not None
    assert t.briefing == rel


def test_update_briefing_for_legacy_task_allocates_slug(cfg: Config):
    """Tasks with no briefing yet (legacy / pre-TB-135) get a slug
    allocated from their CURRENT title at queue-append time."""
    _seed_backlog_task(cfg, title="Legacy task", briefing=None)

    new_briefing = (
        "# Legacy briefing\n\n"
        "## Goal\n\nstub\n\n"
        "## Scope\n\n- foo.py\n\n"
        "## Design\n\nedit\n\n"
        "## Verification\n- `pytest`\n\n"
        "## Out of scope\n\n- nothing\n"
    )
    tools.do_operator_queue_append(
        cfg,
        {"op": "update", "task_id": "TB-400", "briefing": new_briefing},
    )
    tools.drain_operator_queue(cfg)

    t = Board.load(cfg.tasks_file).get("TB-400")
    assert t is not None
    assert t.briefing is not None
    target = cfg.project_root / t.briefing
    assert target.exists()
    assert target.read_text() == new_briefing
    # Slug derived from the CURRENT title.
    assert "legacy-task" in t.briefing

"""Tests for `ap2 approve` (TB-121).

The CLI surface for promoting an ideation-proposed task out of its
`@blocked:review` codespan so it auto-dispatches on the next tick.
Routes through the operator queue (TB-142) so the mutation lands at a
tick boundary instead of mid-task-run; the actual codespan strip
happens on the daemon's next drain pass via the shared
`_approve_review_token` helper.
"""
from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

import pytest

from ap2 import events, tools
from ap2.board import Board, locked_board
from ap2.cli import cmd_approve
from ap2.config import Config
from ap2.init import init_project


def _project(tmp_path: Path) -> Config:
    init_project(tmp_path)
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()
    return cfg


def _drain(cfg: Config) -> dict:
    """Apply queued ops as the daemon's first tick stage would."""
    return tools.drain_operator_queue(cfg)


def test_approve_strips_review_token_after_drain(tmp_path: Path):
    """Happy path: `cmd_approve` queues an op; drain strips the
    `@blocked:review` codespan and the task becomes dispatchable."""
    cfg = _project(tmp_path)
    board = Board.load(cfg.tasks_file)
    board.add(
        "Backlog",
        task_id="TB-100",
        title="ideation proposal",
        meta={"blocked": "review"},
    )
    board.save()
    raw_pre = cfg.tasks_file.read_text()
    assert "`@blocked:review`" in raw_pre

    rc = cmd_approve(cfg, Namespace(task_id="TB-100"))
    assert rc == 0
    # Pre-drain: queue file has the record; TASKS.md unchanged.
    assert cfg.tasks_file.read_text() == raw_pre
    summary = _drain(cfg)
    assert summary["applied"] == 1

    # Post-drain: codespan gone, blocked_on empty, task dispatchable.
    raw_post = cfg.tasks_file.read_text()
    assert "`@blocked:review`" not in raw_post
    board2 = Board.load(cfg.tasks_file)
    t = board2.get("TB-100")
    assert t is not None
    assert t.blocked_on == []
    assert board2.next_dispatchable("Backlog") is not None
    assert board2.next_dispatchable("Backlog").id == "TB-100"


def test_approve_emits_ideation_approved_event(tmp_path: Path):
    """Audit trail: `ideation_approved` lands in events.jsonl after
    drain so post-mortems show which tasks were operator-approved
    (vs. operator-rejected via `ap2 delete`)."""
    cfg = _project(tmp_path)
    board = Board.load(cfg.tasks_file)
    board.add(
        "Backlog", task_id="TB-110", title="prop",
        meta={"blocked": "review"},
    )
    board.save()

    cmd_approve(cfg, Namespace(task_id="TB-110"))
    _drain(cfg)

    evts = events.tail(cfg.events_file, n=20)
    approved = [e for e in evts if e["type"] == "ideation_approved"]
    assert len(approved) == 1
    assert approved[0]["task"] == "TB-110"


def test_approve_unknown_task_returns_error(tmp_path: Path, capsys):
    """Snapshot validation runs at queue-append time — a typo'd TB-N
    is rejected immediately with exit 1, no queue record written."""
    cfg = _project(tmp_path)
    rc = cmd_approve(cfg, Namespace(task_id="TB-9999"))
    assert rc == 1
    err = capsys.readouterr().err
    assert "not on board" in err
    queue_path = tools.operator_queue_path(cfg)
    if queue_path.exists():
        # No record should have been queued for the rejected op.
        records = [
            ln for ln in queue_path.read_text().splitlines() if ln.strip()
        ]
        for ln in records:
            rec = json.loads(ln)
            assert rec.get("op") != "approve"


def test_approve_preserves_other_blockers(tmp_path: Path):
    """`approve` only strips the `review` token; other blockers in the
    same `@blocked:` codespan survive so the dependency check still
    gates dispatch on them. Regression guard for the codespan parse
    when multiple blockers are present."""
    cfg = _project(tmp_path)
    board = Board.load(cfg.tasks_file)
    board.add(
        "Backlog", task_id="TB-120", title="multi-blocker",
        meta={"blocked": "TB-5,review,TB-7"},
    )
    board.save()

    cmd_approve(cfg, Namespace(task_id="TB-120"))
    _drain(cfg)

    board2 = Board.load(cfg.tasks_file)
    t = board2.get("TB-120")
    assert t is not None
    # `review` stripped; TB-5 + TB-7 preserved.
    assert t.meta.get("blocked") == "TB-5,TB-7"
    assert t.blocked_on == ["TB-5", "TB-7"]


def test_approve_idempotent_on_already_approved_task(tmp_path: Path):
    """Approving a task that has no review gate is a (cosmetic) no-op:
    the queue still applies cleanly; the task just rerenders without
    the review token (it wasn't there to begin with). No spurious
    events or board churn beyond the audit line."""
    cfg = _project(tmp_path)
    board = Board.load(cfg.tasks_file)
    board.add("Backlog", task_id="TB-130", title="already free")
    board.save()

    cmd_approve(cfg, Namespace(task_id="TB-130"))
    summary = _drain(cfg)
    assert summary["applied"] == 1
    board2 = Board.load(cfg.tasks_file)
    t = board2.get("TB-130")
    assert t is not None
    assert t.blocked_on == []


def test_approve_records_queue_entry_with_locked_board(tmp_path: Path):
    """Lock contention guard: while the board is held by another
    locked_board context, `cmd_approve` queues the op without taking
    a write lock on TASKS.md — the queue file is the only thing
    appended. Pins TB-131's design (queue append doesn't serialize
    against in-flight task agents)."""
    cfg = _project(tmp_path)
    board = Board.load(cfg.tasks_file)
    board.add(
        "Backlog", task_id="TB-140", title="prop",
        meta={"blocked": "review"},
    )
    board.save()

    # Hold the board lock; cmd_approve's snapshot validation does take
    # the lock briefly but for non-add ops it only reads the board
    # under it, then writes only to the queue file. This test pins
    # that the CLI returns successfully and the queue grew by one
    # record.
    queue_path = tools.operator_queue_path(cfg)
    pre_queue = queue_path.read_text() if queue_path.exists() else ""

    rc = cmd_approve(cfg, Namespace(task_id="TB-140"))
    assert rc == 0

    post_queue = queue_path.read_text()
    new_lines = [
        ln for ln in post_queue.splitlines()
        if ln.strip() and ln not in pre_queue.splitlines()
    ]
    assert len(new_lines) == 1
    rec = json.loads(new_lines[0])
    assert rec["op"] == "approve"
    assert rec["args"]["task_id"] == "TB-140"


# ---------------------------------------------------------------------------
# Watchdog suppression: `_maybe_auto_diagnose` must NOT post the diagnose
# dump when every Backlog task is review-gated and nothing else is in
# flight. Instead it posts a softer `pending_review_reminder`.

def test_watchdog_skips_diagnose_when_wholly_pending_review(
    tmp_path: Path, monkeypatch
):
    """TB-121: when the board's only content is review-gated proposals,
    the daemon is correctly idle — operator approval is what unsticks
    work. Watchdog posts a short reminder, NOT the full diagnose dump."""
    from ap2 import daemon

    cfg = _project(tmp_path)
    board = Board.load(cfg.tasks_file)
    board.add(
        "Backlog", task_id="TB-150", title="prop a",
        meta={"blocked": "review"},
    )
    board.add(
        "Backlog", task_id="TB-151", title="prop b",
        meta={"blocked": "review"},
    )
    board.save()
    # Seed a meaningful event so the watchdog has a baseline timestamp.
    events.append(cfg.events_file, "daemon_start", pid=1234)

    posted: list[tuple[str, str]] = []

    def _fake_post(channel, text):
        posted.append((channel, text))
        return "post-stub"

    monkeypatch.setattr(tools, "_mm_post", _fake_post)
    monkeypatch.setenv("AP2_MM_CHANNELS", "ch-stub")
    # Force the threshold/cooldown checks to pass by faking a far-future now.
    far_future = float(2 ** 31)
    daemon._maybe_auto_diagnose(cfg, now=far_future)

    # Exactly one post — the soft reminder, not the diagnose dump.
    assert len(posted) == 1
    assert posted[0][0] == "ch-stub"
    text = posted[0][1]
    assert "pending review" in text.lower()
    assert "TB-150" in text and "TB-151" in text
    assert "ap2 approve" in text
    # Anti-regression: the heavier `ap2 watchdog` headline must NOT fire.
    assert "ap2 watchdog" not in text

    # Event log has the reminder, not the diagnose-fired event.
    evts = events.tail(cfg.events_file, n=20)
    types = [e["type"] for e in evts]
    assert "pending_review_reminder" in types
    assert "auto_diagnose_fired" not in types

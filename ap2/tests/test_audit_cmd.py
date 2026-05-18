"""Tests for `ap2 audit` (TB-248).

Covers:
- State-derivation helpers in `ap2/audit.py` — cursor parse from
  `<ts> — ran audit (...)` lines in operator_log.md; reviewed-set
  parse from the union of `classified TB-N` / `audit-skipped TB-N` /
  `rejected TB-N` lines; the unreviewed list (TASKS.md Complete +
  Frozen minus the reviewed set, filtered against the cursor).
- The new `audit_skip` operator-queue op-shape — queue-append
  validation + drain-side line-append to operator_log.md.
- The `ap2 audit` CLI surface — default listing, JSON shape, the
  `--frozen-only` / `--auto-approved-only` filters, and the `ran
  audit (N unreviewed)` cursor-line write via the existing `ack`
  op-shape.

State design promise: no new state file is introduced. All persistence
goes through operator_log.md (already daemon-owned, already
`board_file_lock`-serialized at drain time). The audit cursor + the
reviewed-set are both grep-derived; the audit command WRITES nothing
to disk directly — every mutation routes through
`do_operator_queue_append`.
"""
from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

import pytest

from ap2 import audit, events, tools
from ap2.board import Board
from ap2.config import Config
from ap2.init import init_project


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    init_project(tmp_path)
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()
    return cfg


# ---------------------------------------------------------------------------
# helpers: seed TASKS.md + operator_log.md + events.jsonl


def _add_task(cfg: Config, *, task_id: str, title: str, section: str) -> None:
    board = Board.load(cfg.tasks_file)
    board.add(section, task_id=task_id, title=title)
    board.save()


def _append_log(cfg: Config, line: str) -> None:
    """Append a raw bullet line to operator_log.md. Used to seed the
    `classified` / `audit-skipped` / `rejected` / `ran audit` lines
    without going through the full operator-queue drain — the audit
    state-derivation helpers care only about the text shape, so a
    direct write is the cheapest unit-test seed.
    """
    log_path = cfg.project_root / ".cc-autopilot" / "operator_log.md"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if not log_path.exists():
        log_path.write_text(
            "# Operator log\n\n"
            "_Operator decisions and action acknowledgements. "
            "Append-only._\n\n"
        )
    with log_path.open("a") as f:
        f.write(line.rstrip("\n") + "\n")


def _emit_task_complete(
    cfg: Config,
    *,
    task: str,
    ts: str,
    summary: str = "shipped",
    commit: str = "abc1234",
    status: str = "complete",
) -> None:
    """Write a `task_complete` event directly with a fixed `ts` so the
    unreviewed-list cursor compare is deterministic. We bypass
    `events.append`'s `now()` stamp by writing the line ourselves.
    """
    cfg.events_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ts": ts,
        "type": "task_complete",
        "task": task,
        "status": status,
        "commit": commit,
        "summary": summary,
    }
    with cfg.events_file.open("a") as f:
        f.write(json.dumps(payload) + "\n")


def _emit_auto_approved(cfg: Config, *, task: str, ts: str) -> None:
    cfg.events_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {"ts": ts, "type": "auto_approved", "task": task}
    with cfg.events_file.open("a") as f:
        f.write(json.dumps(payload) + "\n")


# ---------------------------------------------------------------------------
# (Scope §4 #1) cursor + reviewed-set: lists only unreviewed since cursor


def test_audit_lists_unreviewed_since_cursor(cfg: Config):
    """Seed a `ran audit` cursor at T0, two complete tasks at T1<T2
    (both after T0), one with a classified line at t∈(T0,T2). Audit
    must list only the un-classified task."""
    _append_log(cfg, "- 2026-04-01T00:00:00Z — ran audit (0 unreviewed)")
    _add_task(cfg, task_id="TB-501", title="first", section="Complete")
    _add_task(cfg, task_id="TB-502", title="second", section="Complete")
    _emit_task_complete(cfg, task="TB-501", ts="2026-04-02T00:00:00Z")
    _emit_task_complete(cfg, task="TB-502", ts="2026-04-03T00:00:00Z")
    _append_log(
        cfg,
        "- 2026-04-02T01:00:00Z — classified TB-501 impact=advanced-goal: ok",
    )

    rows = audit.list_unreviewed(cfg)
    ids = [r.task_id for r in rows]
    assert ids == ["TB-502"], rows


# ---------------------------------------------------------------------------
# (Scope §4 #2) no prior cursor → list ALL Complete + Frozen


def test_audit_lists_all_when_no_prior_cursor(cfg: Config):
    """Empty operator_log → cursor is None → all Complete + Frozen
    tasks surface (even those with no `task_complete` event in the
    tail — they sort to the end via the missing-ts fallback)."""
    _add_task(cfg, task_id="TB-510", title="c1", section="Complete")
    _add_task(cfg, task_id="TB-511", title="c2", section="Complete")
    _add_task(cfg, task_id="TB-512", title="f1", section="Frozen")
    _emit_task_complete(cfg, task="TB-510", ts="2026-03-01T00:00:00Z")
    _emit_task_complete(
        cfg, task="TB-511", ts="2026-03-02T00:00:00Z", status="complete"
    )
    _emit_task_complete(
        cfg, task="TB-512", ts="2026-03-03T00:00:00Z",
        status="verification_failed",
    )

    assert audit.parse_audit_cursor(cfg) is None
    rows = audit.list_unreviewed(cfg)
    ids = sorted(r.task_id for r in rows)
    assert ids == ["TB-510", "TB-511", "TB-512"]


# ---------------------------------------------------------------------------
# (Scope §4 #3) --frozen-only filter


def test_audit_filter_frozen_only(cfg: Config):
    """With `--frozen-only`, Complete tasks are excluded even if
    unreviewed."""
    _add_task(cfg, task_id="TB-520", title="done", section="Complete")
    _add_task(cfg, task_id="TB-521", title="frozen", section="Frozen")
    _emit_task_complete(cfg, task="TB-520", ts="2026-03-04T00:00:00Z")
    _emit_task_complete(
        cfg, task="TB-521", ts="2026-03-05T00:00:00Z",
        status="verification_failed",
    )

    rows = audit.list_unreviewed(cfg, frozen_only=True)
    ids = [r.task_id for r in rows]
    assert ids == ["TB-521"]


# ---------------------------------------------------------------------------
# (Scope §4 #4) --auto-approved-only filter


def test_audit_filter_auto_approved_only(cfg: Config):
    """With `--auto-approved-only`, only tasks with an `auto_approved`
    event in events.jsonl are listed."""
    _add_task(cfg, task_id="TB-530", title="manual", section="Complete")
    _add_task(cfg, task_id="TB-531", title="auto", section="Complete")
    _emit_auto_approved(cfg, task="TB-531", ts="2026-03-05T23:59:00Z")
    _emit_task_complete(cfg, task="TB-530", ts="2026-03-06T00:00:00Z")
    _emit_task_complete(cfg, task="TB-531", ts="2026-03-06T00:01:00Z")

    rows = audit.list_unreviewed(cfg, auto_approved_only=True)
    ids = [r.task_id for r in rows]
    assert ids == ["TB-531"]
    assert rows[0].auto_approved is True


# ---------------------------------------------------------------------------
# (Scope §4 #5) [s]kip queues the correct op-shape


def test_audit_skip_queues_correct_op(cfg: Config):
    """The `do_operator_queue_append({"op":"audit_skip",...})` write
    path lands a record with the expected op + task_id + reason on
    the queue file."""
    _add_task(cfg, task_id="TB-540", title="auditme", section="Complete")
    res = tools.do_operator_queue_append(
        cfg,
        {
            "op": "audit_skip",
            "task_id": "TB-540",
            "reason": "will revisit after release",
        },
    )
    assert not res.get("isError"), res

    queue_path = tools.operator_queue_path(cfg)
    lines = [
        json.loads(ln) for ln in queue_path.read_text().splitlines() if ln.strip()
    ]
    assert len(lines) == 1
    rec = lines[0]
    assert rec["op"] == "audit_skip"
    assert rec["args"]["task_id"] == "TB-540"
    assert rec["args"]["reason"] == "will revisit after release"


# ---------------------------------------------------------------------------
# (Scope §4 #6) drain handler appends operator_log.md line


def test_audit_skip_drain_appends_operator_log(cfg: Config):
    """After `drain_operator_queue` runs, operator_log.md gains the
    rich `<ts> — audit-skipped TB-N: <reason>` line AND the standard
    `applied operator-queued audit_skip → TB-N` line."""
    _add_task(cfg, task_id="TB-550", title="skipme", section="Complete")
    tools.do_operator_queue_append(
        cfg,
        {"op": "audit_skip", "task_id": "TB-550", "reason": "low impact"},
    )
    summary = tools.drain_operator_queue(cfg)
    assert summary["applied"] == 1

    log_text = (
        cfg.project_root / ".cc-autopilot" / "operator_log.md"
    ).read_text()
    assert "audit-skipped TB-550: low impact" in log_text
    assert "applied operator-queued audit_skip → TB-550" in log_text

    # Reviewed-set parser picks it up.
    reviewed = audit.parse_reviewed_set(cfg)
    assert "TB-550" in reviewed

    # Event emitted with structured payload.
    evts = events.tail(cfg.events_file, 10)
    audit_evts = [e for e in evts if e["type"] == "task_audit_skipped"]
    assert len(audit_evts) == 1
    assert audit_evts[0]["task"] == "TB-550"
    assert audit_evts[0]["reason"] == "low impact"


# ---------------------------------------------------------------------------
# (Scope §4 #7) `ap2 audit` invocation queues cursor line


def test_audit_run_appends_cursor_line(cfg: Config, capsys):
    """`cmd_audit` (default mode) queues a `ran audit (N unreviewed)`
    note via the existing `ack` op-shape. After drain the line lands
    in operator_log.md and the cursor parser picks it up."""
    from ap2.cli import cmd_audit

    _add_task(cfg, task_id="TB-560", title="todo", section="Complete")
    _emit_task_complete(cfg, task="TB-560", ts="2026-03-07T00:00:00Z")

    rc = cmd_audit(
        cfg,
        Namespace(
            interactive=False,
            json=False,
            since=None,
            frozen_only=False,
            auto_approved_only=False,
        ),
    )
    assert rc == 0

    # Queue has an `ack` record with the `ran audit (...)` note.
    queue_path = tools.operator_queue_path(cfg)
    lines = [
        json.loads(ln) for ln in queue_path.read_text().splitlines() if ln.strip()
    ]
    acks = [ln for ln in lines if ln["op"] == "ack"]
    assert len(acks) == 1
    assert "ran audit" in acks[0]["args"]["note"]
    assert "1 unreviewed" in acks[0]["args"]["note"]

    # After drain the operator_log.md line lands and the cursor
    # parser picks it up.
    tools.drain_operator_queue(cfg)
    cursor = audit.parse_audit_cursor(cfg)
    assert cursor is not None
    log_text = (
        cfg.project_root / ".cc-autopilot" / "operator_log.md"
    ).read_text()
    assert "ran audit (1 unreviewed)" in log_text


# ---------------------------------------------------------------------------
# (Scope §4 #8) cursor uses the MOST RECENT `ran audit` line


def test_audit_cursor_derives_from_most_recent_ran_audit(cfg: Config):
    """Two `ran audit (...)` lines in operator_log.md; the parser
    returns the most recent timestamp."""
    _append_log(cfg, "- 2026-04-01T00:00:00Z — ran audit (5 unreviewed)")
    _append_log(cfg, "- 2026-04-15T12:00:00Z — ran audit (2 unreviewed)")
    assert audit.parse_audit_cursor(cfg) == "2026-04-15T12:00:00Z"


# ---------------------------------------------------------------------------
# (Scope §4 #9) classified task is excluded from unreviewed set


def test_audit_classified_task_excluded(cfg: Config):
    """A task with a `classified TB-N impact=...` line in
    operator_log.md is in the reviewed set and so doesn't surface."""
    _add_task(cfg, task_id="TB-570", title="classified", section="Complete")
    _emit_task_complete(cfg, task="TB-570", ts="2026-03-09T00:00:00Z")
    _append_log(
        cfg,
        "- 2026-03-09T12:00:00Z — classified TB-570 impact=advanced-goal: "
        "moved goal forward",
    )

    rows = audit.list_unreviewed(cfg)
    ids = [r.task_id for r in rows]
    assert "TB-570" not in ids
    assert "TB-570" in audit.parse_reviewed_set(cfg)


# ---------------------------------------------------------------------------
# (Scope §4 #10) rejected task is excluded from unreviewed set


def test_audit_rejected_task_excluded(cfg: Config):
    """A task with a `rejected ideation proposal → TB-N` line in
    operator_log.md counts as reviewed (operator made an explicit
    decision)."""
    _add_task(cfg, task_id="TB-580", title="rejected-then-resurrected",
              section="Complete")
    _emit_task_complete(cfg, task="TB-580", ts="2026-03-10T00:00:00Z")
    _append_log(
        cfg,
        "- 2026-03-10T11:00:00Z — rejected ideation proposal → TB-580 "
        "(some title): out of focus",
    )

    rows = audit.list_unreviewed(cfg)
    ids = [r.task_id for r in rows]
    assert "TB-580" not in ids
    assert "TB-580" in audit.parse_reviewed_set(cfg)


# ---------------------------------------------------------------------------
# Bonus pinning: operator-queue contract + briefing promise


def test_audit_skip_in_operator_queue_ops():
    """Anchor: `audit_skip` is registered in OPERATOR_QUEUE_OPS so the
    snapshot-validation gate in `do_operator_queue_append` accepts it.
    """
    assert "audit_skip" in tools.OPERATOR_QUEUE_OPS


def test_audit_skip_rejects_missing_task_id(cfg: Config):
    """Queue-append validation rejects an audit_skip op without a
    task_id (operator error)."""
    res = tools.do_operator_queue_append(cfg, {"op": "audit_skip"})
    assert res.get("isError")
    assert "task_id is required" in res["content"][0]["text"]


def test_audit_skip_rejects_unknown_task(cfg: Config):
    """Snapshot validation rejects unknown TB-N at append time."""
    res = tools.do_operator_queue_append(
        cfg, {"op": "audit_skip", "task_id": "TB-99999"}
    )
    assert res.get("isError")
    assert "not on board" in res["content"][0]["text"]


def test_audit_cmd_writes_nothing_directly_to_operator_log(
    cfg: Config, capsys
):
    """Briefing promise: `cmd_audit` mutates state ONLY via
    `do_operator_queue_append` — never via a direct write to
    operator_log.md. Verified by running the default-mode audit, then
    asserting operator_log.md is unchanged on disk and the queue file
    grew by exactly one record (the cursor `ack`).
    """
    from ap2.cli import cmd_audit

    _add_task(cfg, task_id="TB-590", title="trace", section="Complete")
    log_path = cfg.project_root / ".cc-autopilot" / "operator_log.md"
    pre = log_path.read_text() if log_path.exists() else ""

    rc = cmd_audit(
        cfg,
        Namespace(
            interactive=False,
            json=False,
            since=None,
            frozen_only=False,
            auto_approved_only=False,
        ),
    )
    assert rc == 0
    post = log_path.read_text() if log_path.exists() else ""
    assert pre == post, (
        "cmd_audit must not write to operator_log.md directly — only the "
        "drain handler does (via the queued `ack` cursor line)."
    )
    queue_path = tools.operator_queue_path(cfg)
    lines = [
        ln for ln in queue_path.read_text().splitlines() if ln.strip()
    ]
    assert len(lines) == 1


def test_audit_json_shape_is_machine_readable(cfg: Config, capsys):
    """`--json` emits a top-level dict with `cursor`, `filter`, and
    `unreviewed` keys; each row mirrors `UnreviewedTask`."""
    from ap2.cli import cmd_audit

    _add_task(cfg, task_id="TB-600", title="jsonme", section="Complete")
    _emit_task_complete(
        cfg, task="TB-600", ts="2026-03-11T00:00:00Z", commit="deadbee",
    )
    rc = cmd_audit(
        cfg,
        Namespace(
            interactive=False,
            json=True,
            since=None,
            frozen_only=False,
            auto_approved_only=False,
        ),
    )
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert "cursor" in payload
    assert "filter" in payload
    assert "unreviewed" in payload
    assert len(payload["unreviewed"]) == 1
    row = payload["unreviewed"][0]
    assert row["task_id"] == "TB-600"
    assert row["status"] == "Complete"
    assert row["commit"] == "deadbee"
    assert row["auto_approved"] is False
    assert row["completed_at"] == "2026-03-11T00:00:00Z"


def test_audit_no_unreviewed_prints_nothing_to_review(cfg: Config, capsys):
    """Empty unreviewed set prints the "0 unreviewed" line + still
    queues the cursor ack (so future invocations show the operator
    looked even when nothing was pending)."""
    from ap2.cli import cmd_audit

    rc = cmd_audit(
        cfg,
        Namespace(
            interactive=False,
            json=False,
            since=None,
            frozen_only=False,
            auto_approved_only=False,
        ),
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "0 unreviewed" in out
    assert "nothing to review" in out

    queue_path = tools.operator_queue_path(cfg)
    lines = [
        json.loads(ln) for ln in queue_path.read_text().splitlines() if ln.strip()
    ]
    assert len(lines) == 1
    assert lines[0]["op"] == "ack"
    assert "ran audit (0 unreviewed)" in lines[0]["args"]["note"]

"""Tests for `ap2.board_edits` — relocated from `ap2/tests/test_tools.py`
as part of TB-268 (test-file mirror of the TB-262 source split).

Covers: `do_board_edit` (the SDK-free board mutation surface) across
its `add_ready` / `add_backlog` / `add_frozen` / `move_to_*` / `approve`
actions, plus the surface-level rejection gates (TB-134 newlines in
title/description/tags, TB-216 asterisk-in-title via TASK_LINE_RE,
TB-135 briefing-required for every add_*). Tests are pure mechanical
relocations from `test_tools.py` — identical bodies, no logic edits —
per the TB-268 briefing's "relocation only" rule. The validator
gate-firing tests (TB-154 / TB-161 / TB-164 / TB-171) live in
`test_briefing_validators.py`; what stays here is the board-edit
mutation contract.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ap2.board import Board
from ap2.config import Config
from ap2 import tools
from ap2.tests._briefing_fixtures import (
    canonical_briefing,
)


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    (tmp_path / "TASKS.md").write_text(
        "# Tasks\n\n## Active\n\n## Ready\n\n## Backlog\n\n"
        "- [ ] **TB-5** **Existing** `#x` — An old task.\n\n"
        "## Complete\n\n## Frozen\n"
    )
    (tmp_path / "CLAUDE.md").write_text(
        "## Autopilot\n\n- Task list: `TASKS.md`\n- Next task ID: TB-10\n"
    )
    return Config.load(tmp_path)


def _unwrap(res: dict) -> dict:
    assert not res.get("isError"), res
    return json.loads(res["content"][0]["text"])


_DEFAULT_BRIEFING = canonical_briefing("TB-200", title="Brand new")


def test_board_edit_add_ready_assigns_id(cfg, tmp_path):
    res = tools.do_board_edit(
        cfg,
        {
            "action": "add_ready", "title": "Brand new", "tags": ["#auto"],
            # TB-135: briefing payload is now required for every add_*.
            "briefing": _DEFAULT_BRIEFING,
        },
    )
    body = _unwrap(res)
    assert body["task_id"] == "TB-10"
    b = Board.load(cfg.tasks_file)
    assert b.find("TB-10") == ("Ready", 0)
    # CLAUDE.md next_task_id bumped to 11
    assert "TB-11" in (tmp_path / "CLAUDE.md").read_text()


def test_board_edit_add_writes_briefing(cfg):
    res = tools.do_board_edit(
        cfg,
        {
            "action": "add_backlog",
            "title": "With brief",
            "briefing": _DEFAULT_BRIEFING,
        },
    )
    body = _unwrap(res)
    brief_path = cfg.project_root / body["briefing_path"]
    assert brief_path.exists()
    # Briefing bytes round-trip onto disk verbatim.
    assert brief_path.read_text() == _DEFAULT_BRIEFING


def test_board_edit_move(cfg):
    tools.do_board_edit(cfg, {"action": "move_to_ready", "task_id": "TB-5"})
    b = Board.load(cfg.tasks_file)
    assert b.find("TB-5")[0] == "Ready"


def test_board_edit_invalid_action(cfg):
    res = tools.do_board_edit(cfg, {"action": "bogus"})
    assert res.get("isError")


def test_board_edit_move_missing_id(cfg):
    res = tools.do_board_edit(cfg, {"action": "move_to_complete", "task_id": "TB-999"})
    assert res.get("isError")


def test_board_edit_add_ready_honors_blocked_on(cfg):
    """TB-132: blocked_on now lands on the task line as a `@blocked:<csv>`
    codespan (in `meta`), not as a `(blocked on: ...)` clause baked into
    the description prose. The blocker semantic is identical
    (`Task.blocked_on` returns the same tokens) — what changed is where
    the parser reads them from."""
    res = tools.do_board_edit(
        cfg,
        {
            "action": "add_ready", "title": "Waiter", "blocked_on": "TB-5",
            "briefing": _DEFAULT_BRIEFING,
        },
    )
    body = _unwrap(res)
    b = Board.load(cfg.tasks_file)
    t = b.get(body["task_id"])
    assert t is not None
    assert t.meta.get("blocked") == "TB-5"
    assert t.blocked_on == ["TB-5"]
    # Description prose is no longer the blocker carrier — TB-132 ended
    # the regex-on-description failure mode (TB-121's prose collision).
    assert "blocked on" not in t.description.lower()


def test_board_edit_add_backlog_honors_blocked_on(cfg):
    res = tools.do_board_edit(
        cfg,
        {
            "action": "add_backlog", "title": "Waiter", "blocked_on": "TB-5",
            "briefing": _DEFAULT_BRIEFING,
        },
    )
    body = _unwrap(res)
    b = Board.load(cfg.tasks_file)
    t = b.get(body["task_id"])
    assert t is not None
    assert t.meta.get("blocked") == "TB-5"
    assert t.blocked_on == ["TB-5"]
    assert "blocked on" not in t.description.lower()


def test_board_edit_add_frozen_still_honors_blocked_on(cfg):
    res = tools.do_board_edit(
        cfg,
        {
            "action": "add_frozen", "title": "Waiter", "blocked_on": "TB-5",
            "briefing": _DEFAULT_BRIEFING,
        },
    )
    body = _unwrap(res)
    b = Board.load(cfg.tasks_file)
    t = b.get(body["task_id"])
    assert t is not None
    assert t.meta.get("blocked") == "TB-5"
    assert t.blocked_on == ["TB-5"]
    assert "blocked on" not in t.description.lower()


# ---------------------------------------------------------------------------
# TB-142 (TB-121 cross-ref): `approve` action on do_board_edit. The idle-path
# entry shared with the queue-routed `_apply_operator_op` for `op="approve"`.


def test_board_edit_approve_strips_review_codespan(cfg):
    """`do_board_edit({"action":"approve",...})` strips the `@blocked:review`
    codespan from a Backlog task so the task is dispatchable."""
    b = Board.load(cfg.tasks_file)
    b.add(
        "Backlog",
        task_id="TB-400",
        title="ideation gated",
        meta={"blocked": "review"},
    )
    b.save()

    res = tools.do_board_edit(
        cfg, {"action": "approve", "task_id": "TB-400"}
    )
    body = _unwrap(res)
    assert body["task_id"] == "TB-400"
    assert body["section"] == "Backlog"

    t = Board.load(cfg.tasks_file).get("TB-400")
    assert t is not None
    assert "blocked" not in t.meta
    assert t.blocked_on == []


def test_board_edit_approve_emits_ideation_approved_event(cfg):
    from ap2 import events

    b = Board.load(cfg.tasks_file)
    b.add(
        "Backlog",
        task_id="TB-401",
        title="audit me",
        meta={"blocked": "review"},
    )
    b.save()

    tools.do_board_edit(cfg, {"action": "approve", "task_id": "TB-401"})
    evts = events.tail(cfg.events_file, 5)
    approved = [e for e in evts if e["type"] == "ideation_approved"]
    assert len(approved) == 1
    assert approved[0]["task"] == "TB-401"


def test_board_edit_approve_preserves_other_blockers(cfg):
    """Only the `review` token is stripped — sibling TB-N blockers stay."""
    b = Board.load(cfg.tasks_file)
    b.add(
        "Backlog",
        task_id="TB-402",
        title="multi",
        meta={"blocked": "TB-5,review"},
    )
    b.save()

    tools.do_board_edit(cfg, {"action": "approve", "task_id": "TB-402"})
    t = Board.load(cfg.tasks_file).get("TB-402")
    assert t is not None
    assert t.meta.get("blocked") == "TB-5"
    assert t.blocked_on == ["TB-5"]


def test_board_edit_approve_strips_legacy_description_prose(cfg):
    """Pre-TB-132 transition: tasks authored before the codespan format
    landed may still carry `(blocked on: review)` as description prose.
    Approve scrubs it so the rendered line stays tidy."""
    b = Board.load(cfg.tasks_file)
    b.add(
        "Backlog",
        task_id="TB-403",
        title="legacy",
        description="legacy ideation task (blocked on: review)",
    )
    b.save()

    tools.do_board_edit(cfg, {"action": "approve", "task_id": "TB-403"})
    t = Board.load(cfg.tasks_file).get("TB-403")
    assert t is not None
    assert "blocked on: review" not in t.description.lower()


def test_board_edit_approve_requires_task_id(cfg):
    res = tools.do_board_edit(cfg, {"action": "approve"})
    assert res.get("isError")
    assert "task_id" in res["content"][0]["text"]


def test_board_edit_approve_rejects_unknown_task(cfg):
    res = tools.do_board_edit(
        cfg, {"action": "approve", "task_id": "TB-99999"}
    )
    assert res.get("isError")
    assert "not on board" in res["content"][0]["text"]


def test_board_edit_approve_idempotent_on_unblocked_task(cfg):
    """Already-approved task: approve is a no-op (modulo render). Useful
    so a second `ap2 approve TB-N` doesn't corrupt the line."""
    b = Board.load(cfg.tasks_file)
    b.add("Backlog", task_id="TB-404", title="not gated")
    b.save()

    res = tools.do_board_edit(
        cfg, {"action": "approve", "task_id": "TB-404"}
    )
    body = _unwrap(res)
    assert body["task_id"] == "TB-404"
    t = Board.load(cfg.tasks_file).get("TB-404")
    assert t is not None
    assert t.blocked_on == []


# ---------------------------------------------------------------------------
# TB-134: do_board_edit rejects multi-line input on title / description /
# tag fields. The MCP-driven path (ideation, MM handler) needs the same
# gate as the CLI — otherwise an MCP caller can still write a multi-line
# task line into TASKS.md and break the line-oriented parser. _err /
# isError lets the calling agent retry with a rephrasing.


def test_board_edit_rejects_newline_in_description(cfg, tmp_path):
    """do_board_edit({description: 'a\\nb'}) → isError; nothing landed
    on the board, no briefing file under .cc-autopilot/tasks."""
    tasks_dir = tmp_path / ".cc-autopilot" / "tasks"
    before_briefings = (
        sorted(p.name for p in tasks_dir.iterdir()) if tasks_dir.exists() else []
    )
    before_tasks = (tmp_path / "TASKS.md").read_text()

    res = tools.do_board_edit(
        cfg,
        {"action": "add_backlog", "title": "valid", "description": "a\nb"},
    )

    assert res.get("isError")
    msg = res["content"][0]["text"]
    assert "single line" in msg
    assert "briefing" in msg  # nudge, not a silent auto-collapse
    # Board untouched.
    assert (tmp_path / "TASKS.md").read_text() == before_tasks
    after_briefings = (
        sorted(p.name for p in tasks_dir.iterdir()) if tasks_dir.exists() else []
    )
    assert after_briefings == before_briefings


def test_board_edit_rejects_newline_in_description_add_ready(cfg, tmp_path):
    """add_ready hits the same gate as add_backlog."""
    before = (tmp_path / "TASKS.md").read_text()
    res = tools.do_board_edit(
        cfg,
        {"action": "add_ready", "title": "valid", "description": "a\nb"},
    )
    assert res.get("isError")
    assert "single line" in res["content"][0]["text"]
    assert (tmp_path / "TASKS.md").read_text() == before


def test_board_edit_rejects_newline_in_description_add_frozen(cfg, tmp_path):
    """add_frozen hits the same gate."""
    before = (tmp_path / "TASKS.md").read_text()
    res = tools.do_board_edit(
        cfg,
        {"action": "add_frozen", "title": "valid", "description": "a\nb"},
    )
    assert res.get("isError")
    assert "single line" in res["content"][0]["text"]
    assert (tmp_path / "TASKS.md").read_text() == before


def test_board_edit_rejects_carriage_return_in_description(cfg, tmp_path):
    """\\r is the same hazard — reject with the same message."""
    res = tools.do_board_edit(
        cfg,
        {"action": "add_backlog", "title": "valid", "description": "a\rb"},
    )
    assert res.get("isError")
    assert "single line" in res["content"][0]["text"]


def test_board_edit_rejects_newline_in_title(cfg, tmp_path):
    res = tools.do_board_edit(
        cfg,
        {"action": "add_backlog", "title": "title with\nnewline"},
    )
    assert res.get("isError")
    assert "single line" in res["content"][0]["text"]


def test_board_edit_rejects_newline_in_tag(cfg, tmp_path):
    res = tools.do_board_edit(
        cfg,
        {
            "action": "add_backlog",
            "title": "valid",
            "tags": ["#cli", "#bro\nken"],
        },
    )
    assert res.get("isError")
    assert "single line" in res["content"][0]["text"]


def test_board_edit_accepts_single_line_description(cfg):
    """Regression: single-line descriptions still go through."""
    res = tools.do_board_edit(
        cfg,
        {
            "action": "add_backlog", "title": "ok",
            "description": "one line", "briefing": _DEFAULT_BRIEFING,
        },
    )
    assert not res.get("isError"), res


# ---------------------------------------------------------------------------
# TB-216: do_board_edit rejects titles containing `*`. The validator-level
# helper test lives in test_briefing_validators.py; what's here is the
# board-edit integration gate (asterisk rejected, no briefing leaked) plus
# the symmetric description-still-accepted check (field-specific gate).


def test_board_edit_rejects_asterisk_in_title(cfg, tmp_path):
    """do_board_edit({title: 'has * asterisk'}) → isError; nothing
    lands on the board, no briefing file written."""
    tasks_dir = tmp_path / ".cc-autopilot" / "tasks"
    before_briefings = (
        sorted(p.name for p in tasks_dir.iterdir()) if tasks_dir.exists() else []
    )
    before_tasks = (tmp_path / "TASKS.md").read_text()

    res = tools.do_board_edit(
        cfg,
        {
            "action": "add_backlog",
            "title": "has * asterisk",
            "briefing": _DEFAULT_BRIEFING,
        },
    )

    assert res.get("isError")
    msg = res["content"][0]["text"]
    assert "*" in msg
    assert "TASK_LINE_RE" in msg or "bold-fence" in msg
    # Board untouched, no briefing file written.
    assert (tmp_path / "TASKS.md").read_text() == before_tasks
    after_briefings = (
        sorted(p.name for p in tasks_dir.iterdir()) if tasks_dir.exists() else []
    )
    assert after_briefings == before_briefings


def test_board_edit_accepts_asterisk_in_description(cfg):
    """Field-specific gate: description with `*` is still allowed.
    The parser only chokes on the title group; descriptions are
    free-form prose past the `—` and don't collapse the regex."""
    res = tools.do_board_edit(
        cfg,
        {
            "action": "add_backlog", "title": "clean title",
            "description": "wildcard char * is fine here",
            "briefing": _DEFAULT_BRIEFING,
        },
    )
    assert not res.get("isError"), res


# ---------------------------------------------------------------------------
# TB-135: do_board_edit requires an explicit briefing payload for every
# add_* op. The skeleton-template auto-fill that used to land for
# add_backlog is gone — a briefing whose `## Verification` was just a
# `(additional shell or prose bullets)` placeholder bypassed the per-task
# verifier (TB-131 hit this on 2026-04-30, "passed" on regression gate
# alone with zero scope-specific scoring). MCP-driven callers (ideation,
# MM handler) already construct the payload, so the gate doesn't break
# them.


def test_board_edit_add_backlog_requires_briefing(cfg, tmp_path):
    """Empty/missing briefing on add_backlog → isError; nothing landed
    on the board, no briefing file under .cc-autopilot/tasks."""
    tasks_dir = tmp_path / ".cc-autopilot" / "tasks"
    before_briefings = (
        sorted(p.name for p in tasks_dir.iterdir()) if tasks_dir.exists() else []
    )
    before_tasks = (tmp_path / "TASKS.md").read_text()

    res = tools.do_board_edit(
        cfg,
        {"action": "add_backlog", "title": "needs briefing", "briefing": ""},
    )

    assert res.get("isError")
    msg = res["content"][0]["text"]
    assert "briefing is required" in msg
    # Board untouched, no briefing file written.
    assert (tmp_path / "TASKS.md").read_text() == before_tasks
    after_briefings = (
        sorted(p.name for p in tasks_dir.iterdir()) if tasks_dir.exists() else []
    )
    assert after_briefings == before_briefings


def test_board_edit_add_ready_requires_briefing(cfg, tmp_path):
    """Same gate fires on add_ready — pre-TB-135 only add_backlog
    auto-filled, but the new requirement covers every add_* action."""
    before = (tmp_path / "TASKS.md").read_text()
    res = tools.do_board_edit(
        cfg,
        {"action": "add_ready", "title": "no briefing"},
    )
    assert res.get("isError")
    assert "briefing is required" in res["content"][0]["text"]
    assert (tmp_path / "TASKS.md").read_text() == before


def test_board_edit_add_frozen_requires_briefing(cfg, tmp_path):
    """add_frozen also gated. Operators sometimes seed Frozen with
    superseded ideas; the briefing requirement prevents the same
    placeholder-verifier hole from showing up there."""
    before = (tmp_path / "TASKS.md").read_text()
    res = tools.do_board_edit(
        cfg,
        {"action": "add_frozen", "title": "no briefing"},
    )
    assert res.get("isError")
    assert "briefing is required" in res["content"][0]["text"]
    assert (tmp_path / "TASKS.md").read_text() == before


def test_board_edit_add_with_briefing_text_succeeds(cfg, tmp_path):
    """Daemon-internal callers (ideation, MM handler) construct the
    briefing payload themselves — they're unaffected by TB-135 as long
    as they pass a non-empty `briefing`. Pin the happy path.
    """
    body = canonical_briefing("TB-201", title="Real briefing")
    res = tools.do_board_edit(
        cfg,
        {"action": "add_backlog", "title": "ideation-style", "briefing": body},
    )
    out = _unwrap(res)
    assert out["task_id"].startswith("TB-")
    # Briefing bytes round-trip into .cc-autopilot/tasks/<slug>.md.
    brief_path = cfg.project_root / out["briefing_path"]
    assert brief_path.exists()
    assert brief_path.read_text() == body


def test_board_edit_non_empty_briefing_payload_unaffected_for_daemon_callers(cfg):
    """TB-135 explicit pin: passing a non-empty `briefing` text payload
    still succeeds for every add_* op so daemon-internal callers
    (ideation, MM handler, operator-queue drain reconstructing add_*
    ops) keep working. The new requirement only rejects empty/missing
    briefing — non-empty briefings on add_ready / add_backlog /
    add_frozen all land normally.

    This is the symmetric happy-path companion to the three
    `*_requires_briefing` tests above: they prove empty briefings are
    rejected; this one proves non-empty briefings still go through.
    """
    body = canonical_briefing("TB-202", title="Daemon-built briefing")
    for action, expected_section in (
        ("add_ready", "Ready"),
        ("add_backlog", "Backlog"),
        ("add_frozen", "Frozen"),
    ):
        res = tools.do_board_edit(
            cfg,
            {
                "action": action,
                "title": f"daemon-style {action}",
                "briefing": body,
            },
        )
        out = _unwrap(res)
        # TB-N issued, task lands in the expected section, briefing
        # round-trips to disk under .cc-autopilot/tasks/.
        assert out["task_id"].startswith("TB-"), (action, out)
        brief_path = cfg.project_root / out["briefing_path"]
        assert brief_path.exists(), (action, out)
        assert brief_path.read_text() == body, action
        b = Board.load(cfg.tasks_file)
        assert b.find(out["task_id"])[0] == expected_section, action

"""Tests for `ap2 backfill-proposals` (TB-195).

Covers the operator-log audit-line parser (`parse_operator_log_lines`),
the backfill driver (`backfill_proposals`), and the dry-run + idempotency
guarantees the briefing pins.

Fixture model: each test sets up a fresh ap2-init project, writes a
goal.md whose `## Current focus` heading the briefing fixtures cite, and
writes briefing files + a TASKS.md with the relevant TB-Ns linked. The
briefing fixture mirrors `_BRIEFING` from `test_ideation_proposals.py` so
the structural test (anchor + Why-now both present) passes — mimicking a
real ideation-authored briefing.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ap2 import backfill, tools
from ap2.board import Board
from ap2.config import Config
from ap2.init import init_project


# A goal.md whose `## Current focus` heading the briefing fixtures cite,
# so `extract_goal_anchor` matches against a real anchor. Mirrors the
# project's own goal.md shape — keeping the test fixture and the live
# anchor surface in lockstep so a future tweak to the validator doesn't
# silently de-fang the backfill detection.
_GOAL_MD = (
    "# Project Goals\n\n"
    "## Mission\n\n"
    "Drive the project toward its operator-stated goal.\n\n"
    "## Done when\n\n"
    "- An operator can point ap2 at a fresh project, paste a goal.md, "
    "and walk away for a week without intervention.\n\n"
    "## Current focus: ideation quality signal collection\n\n"
    "Some prose explaining the focus area.\n\n"
    "## Non-goals\n\n"
    "- something out of scope.\n"
)


# Briefing whose `## Goal` body cites the `## Current focus` heading
# verbatim AND carries a TB-164-shaped `Why now:` rationale — both
# extracts return non-None, so backfill classifies it as ideation-
# authored.
_IDEATION_BRIEFING = (
    "# A test briefing\n\n"
    "## Goal\n\n"
    "Closes the ideation quality signal collection gap that the "
    "current focus calls out (cites the `## Current focus: ideation "
    "quality signal collection` heading).\n\n"
    "Why now: closes the goal-shaped pro-forma compliance failure "
    "mode that goal.md's delete-test names; without it the loop "
    "can't detect drift.\n\n"
    "## Scope\n\n- foo.py\n\n"
    "## Design\n\nDirect edit.\n\n"
    "## Verification\n- `uv run pytest -q` — gates pass\n\n"
    "## Out of scope\n\n- nothing\n"
)


# Briefing missing the Why-now paragraph — typical of `ap2 add
# --skip-goal-alignment` operator adds. `extract_why_now` returns None,
# so backfill skips it (skipped_non_ideation).
_OPERATOR_BRIEFING_NO_WHY_NOW = (
    "# Operator-authored briefing\n\n"
    "## Goal\n\n"
    "Closes the ideation quality signal collection gap that the "
    "current focus calls out — but no Why-now paragraph here.\n\n"
    "## Scope\n\n- foo.py\n\n"
    "## Design\n\nDirect edit.\n\n"
    "## Verification\n- `uv run pytest -q` — gates pass\n\n"
    "## Out of scope\n\n- nothing\n"
)


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    """Project root with a real goal.md (so anchors are derivable)."""
    init_project(tmp_path)
    (tmp_path / "goal.md").write_text(_GOAL_MD)
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()
    return cfg


def _write_briefing(cfg: Config, slug: str, content: str) -> str:
    """Write a briefing file under `.cc-autopilot/tasks/<slug>.md` and
    return the relative path string for the board's `[→ brief]` link.
    """
    rel = f".cc-autopilot/tasks/{slug}.md"
    (cfg.project_root / rel).write_text(content)
    return rel


def _add_to_board(
    cfg: Config,
    *,
    tb_id: str,
    section: str,
    title: str,
    briefing_rel: str,
) -> None:
    """Insert a task line in the given board section linked to a
    briefing file. Mirrors how a real ideation `add_backlog` would land
    on disk minus the prospective record write (which the test exercises
    directly).
    """
    board = Board.load(cfg.tasks_file)
    board.add(
        section,
        task_id=tb_id,
        title=title,
        briefing=briefing_rel,
    )
    board.save()


def _append_operator_log(cfg: Config, line: str) -> None:
    """Append a single audit line (without the leading `- ` prefix —
    the helper supplies it) to operator_log.md, creating the file with
    the standard header if missing. Matches the shape
    `_append_operator_audit_line` writes.
    """
    log = cfg.project_root / ".cc-autopilot" / "operator_log.md"
    log.parent.mkdir(parents=True, exist_ok=True)
    if not log.exists():
        log.write_text(
            "# Operator log\n\n"
            "_Operator decisions and action acknowledgements._\n\n"
        )
    with log.open("a") as f:
        f.write(f"- {line}\n")


def _append_event(cfg: Config, evt: dict) -> None:
    """Append a JSON event line to events.jsonl (creates the file)."""
    cfg.events_file.parent.mkdir(parents=True, exist_ok=True)
    with cfg.events_file.open("a") as f:
        f.write(json.dumps(evt) + "\n")


# ---------------------------------------------------------------------------
# parse_operator_log_lines regex pin: every audit-line shape currently
# emitted by `_append_operator_audit_line` (TB-195 design bullet).


def test_parse_operator_log_lines_handles_every_audit_shape(tmp_path: Path):
    """Pins the regex against every audit-line shape currently in the
    file: applied verbs (add_backlog / approve / reject / delete /
    unfreeze / update / add_ready / move_to_backlog / ideate / classify
    / update_goal), the bare `(forced)` ideate target, the
    `(goal-alignment check skipped)` decoration, and the richer TB-152
    `rejected ideation proposal` line. Free-form pivot notes and
    operator acks should NOT match either shape.
    """
    log = tmp_path / "operator_log.md"
    log.write_text(
        "# Operator log\n\n"
        "- 2026-05-01T00:46:23Z — applied operator-queued add_backlog → TB-138\n"
        "- 2026-05-01T01:46:23Z — applied operator-queued add_ready → TB-159\n"
        "- 2026-05-01T02:46:23Z — applied operator-queued approve → TB-153\n"
        "- 2026-05-01T03:46:23Z — applied operator-queued reject → TB-172\n"
        "- 2026-05-01T03:46:23Z — rejected ideation proposal → TB-172 "
        "(Briefing validator: lint shell-fenced bullets (with parens)): "
        "wack-a-mole fix\n"
        "- 2026-05-01T04:46:23Z — applied operator-queued delete → TB-150\n"
        "- 2026-05-01T05:46:23Z — applied operator-queued unfreeze → TB-122\n"
        "- 2026-05-01T06:46:23Z — applied operator-queued update → TB-152\n"
        "- 2026-05-01T07:46:23Z — applied operator-queued move_to_backlog → TB-166\n"
        "- 2026-05-01T08:46:23Z — applied operator-queued ideate → (forced)\n"
        "- 2026-05-01T09:46:23Z — applied operator-queued classify → TB-189\n"
        "- 2026-05-01T10:46:23Z — applied operator-queued update_goal → \n"
        "- 2026-05-01T11:46:23Z — applied operator-queued add_backlog → TB-191 "
        "(goal-alignment check skipped)\n"
        "- 2026-05-06T18:07:11Z — Pivoted Current focus to "
        "\"ideation quality signal collection\".\n"
        "- 2026-05-07T01:57:58Z [TB-175] — operator note about TB-175.\n"
    )

    entries = backfill.parse_operator_log_lines(log)
    by_op_tb = {(e.op, e.tb_id, e.kind): e for e in entries}

    # Every applied verb parses with its TB-N (or None for `ideate`).
    assert ("add_backlog", "TB-138", "applied") in by_op_tb
    assert ("add_ready", "TB-159", "applied") in by_op_tb
    assert ("approve", "TB-153", "applied") in by_op_tb
    assert ("reject", "TB-172", "applied") in by_op_tb
    assert ("delete", "TB-150", "applied") in by_op_tb
    assert ("unfreeze", "TB-122", "applied") in by_op_tb
    assert ("update", "TB-152", "applied") in by_op_tb
    assert ("move_to_backlog", "TB-166", "applied") in by_op_tb
    assert ("ideate", None, "applied") in by_op_tb
    assert ("classify", "TB-189", "applied") in by_op_tb
    # TB-191 with the `(goal-alignment check skipped)` decoration —
    # tb_id is still extracted from the start of the target.
    assert ("add_backlog", "TB-191", "applied") in by_op_tb

    # `rejected ideation proposal` shape parses with the inner-paren
    # title and trailing reason intact even when the title itself
    # contains parens.
    rej = by_op_tb[("reject", "TB-172", "rejected_proposal")]
    assert rej.reason == "wack-a-mole fix"
    assert "Briefing validator" in rej.title
    assert "(with parens)" in rej.title

    # Free-form notes and operator acks don't appear as parsed entries
    # (no false positive on prose lines).
    assert all("Pivoted" not in e.raw for e in entries)
    assert all("operator note" not in e.raw for e in entries)


# ---------------------------------------------------------------------------
# Verification bullets: the five tests the briefing pins.


def test_backfill_writes_record_for_ideation_authored_complete(cfg: Config):
    """Fixture: operator_log line `applied operator-queued add_backlog →
    TB-X`, briefing file with anchor + Why-now, events.jsonl with
    `task_complete` for TB-X status=complete; after `backfill_proposals(cfg)`,
    `.cc-autopilot/ideation_proposals/TB-X.json` exists with both base
    fields AND `outcome` block (`decision_kind=completed`).
    """
    tb_id = "TB-501"
    rel = _write_briefing(cfg, "ideation-authored-complete", _IDEATION_BRIEFING)
    _add_to_board(
        cfg, tb_id=tb_id, section="Complete",
        title="ideation authored complete", briefing_rel=rel,
    )
    _append_operator_log(
        cfg,
        f"2026-05-01T00:46:23Z — applied operator-queued add_backlog → {tb_id}",
    )
    _append_event(
        cfg,
        {
            "ts": "2026-05-01T02:00:00Z",
            "type": "task_complete",
            "task": tb_id,
            "status": "complete",
            "commit": "abc1234",
            "summary": "shipped",
        },
    )

    report = backfill.backfill_proposals(cfg)
    assert tb_id in report.written

    record_path = tools.proposal_record_path(cfg, tb_id)
    assert record_path.exists()

    record = json.loads(record_path.read_text())
    # Base fields populated from briefing + operator log.
    assert record["tb_id"] == tb_id
    assert record["proposed_at"] == "2026-05-01T00:46:23Z"
    assert record["focus_anchor"] is not None
    assert "ideation quality signal collection" in record["focus_anchor"]
    assert record["why_now"] is not None
    assert record["briefing_path"] == rel
    assert record["blocked_on"] == "review"
    # Outcome reconciled from the task_complete event.
    outcome = record["outcome"]
    assert outcome["decision_kind"] == "completed"
    assert outcome["decision_actor"] == "daemon"
    assert outcome["commit"] == "abc1234"


def test_backfill_writes_outcome_for_rejected_proposal(cfg: Config):
    """Fixture: operator_log lines `applied operator-queued add_backlog →
    TB-Y` AND `rejected ideation proposal → TB-Y (...): <reason>`; after
    backfill, the outcome block has `decision_kind=rejected` and `reason`
    populated from the log line.
    """
    tb_id = "TB-502"
    rel = _write_briefing(cfg, "ideation-authored-rejected", _IDEATION_BRIEFING)
    # In a real reject the briefing would be deleted, but legacy data
    # (and the briefing's verification fixture) keeps it on disk so the
    # structural test passes. The reason text is the operator's
    # rationale captured in operator_log.md.
    _add_to_board(
        cfg, tb_id=tb_id, section="Backlog",
        title="rejected proposal", briefing_rel=rel,
    )
    _append_operator_log(
        cfg,
        f"2026-05-01T00:46:23Z — applied operator-queued add_backlog → {tb_id}",
    )
    reason_text = "out of scope for the current focus"
    _append_operator_log(
        cfg,
        f"2026-05-01T01:00:00Z — applied operator-queued reject → {tb_id}",
    )
    _append_operator_log(
        cfg,
        f"2026-05-01T01:00:00Z — rejected ideation proposal → "
        f"{tb_id} (rejected proposal): {reason_text}",
    )

    backfill.backfill_proposals(cfg)
    record = json.loads(tools.proposal_record_path(cfg, tb_id).read_text())
    outcome = record["outcome"]
    assert outcome["decision_kind"] == "rejected"
    assert outcome["decision_actor"] == "operator"
    assert outcome["reason"] == reason_text


def test_backfill_skips_operator_authored_briefings(cfg: Config):
    """Fixture briefing missing the Why-now paragraph; backfill writes
    no record for that TB-N (operator-authored adds via
    `--skip-goal-alignment` aren't ideation proposals).
    """
    tb_id = "TB-503"
    rel = _write_briefing(
        cfg, "operator-authored", _OPERATOR_BRIEFING_NO_WHY_NOW,
    )
    _add_to_board(
        cfg, tb_id=tb_id, section="Backlog",
        title="operator-authored add", briefing_rel=rel,
    )
    _append_operator_log(
        cfg,
        f"2026-05-01T00:46:23Z — applied operator-queued add_backlog → "
        f"{tb_id} (goal-alignment check skipped)",
    )

    report = backfill.backfill_proposals(cfg)
    assert tb_id not in report.written
    assert tb_id in report.skipped_non_ideation
    assert not tools.proposal_record_path(cfg, tb_id).exists()


def test_backfill_is_idempotent(cfg: Config):
    """Running twice in a row produces identical disk state on the
    second pass; second pass's report names zero new records.
    """
    tb_id = "TB-504"
    rel = _write_briefing(cfg, "idempotent-fixture", _IDEATION_BRIEFING)
    _add_to_board(
        cfg, tb_id=tb_id, section="Backlog",
        title="idempotent fixture", briefing_rel=rel,
    )
    _append_operator_log(
        cfg,
        f"2026-05-01T00:46:23Z — applied operator-queued add_backlog → {tb_id}",
    )

    first_report = backfill.backfill_proposals(cfg)
    assert tb_id in first_report.written
    record_path = tools.proposal_record_path(cfg, tb_id)
    first_state = record_path.read_bytes()

    second_report = backfill.backfill_proposals(cfg)
    assert second_report.written == []
    assert tb_id in second_report.skipped_existing
    second_state = record_path.read_bytes()
    assert first_state == second_state


def test_dry_run_writes_nothing(cfg: Config):
    """After `backfill_proposals(cfg, dry_run=True)`, the records
    directory is unchanged; the function's report still names TB-Ns it
    would have written.
    """
    tb_id = "TB-505"
    rel = _write_briefing(cfg, "dry-run-fixture", _IDEATION_BRIEFING)
    _add_to_board(
        cfg, tb_id=tb_id, section="Backlog",
        title="dry run fixture", briefing_rel=rel,
    )
    _append_operator_log(
        cfg,
        f"2026-05-01T00:46:23Z — applied operator-queued add_backlog → {tb_id}",
    )

    proposals_dir = tools.ideation_proposals_dir(cfg)
    # Snapshot the dir contents before the dry-run.
    before = sorted(p.name for p in proposals_dir.glob("*.json")) \
        if proposals_dir.exists() else []

    report = backfill.backfill_proposals(cfg, dry_run=True)
    assert tb_id in report.written
    assert any(tb_id in s for s in report.summaries)

    after = sorted(p.name for p in proposals_dir.glob("*.json")) \
        if proposals_dir.exists() else []
    assert before == after
    assert not tools.proposal_record_path(cfg, tb_id).exists()

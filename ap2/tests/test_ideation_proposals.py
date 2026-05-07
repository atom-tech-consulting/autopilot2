"""Tests for the per-proposal record layer (TB-188).

Covers the seed-write at ideation `add_backlog` time, the public
`extract_goal_anchor` / `extract_why_now` helpers, and the four
terminal-event reconciliation surfaces (task_complete completed,
task_complete verification_failed via the helper, operator-queue
reject with reason, operator-queue delete, operator-queue approve).

The model: `do_board_edit({"action": "add_backlog", "blocked_on":
"review", ...})` (the ideation path) emits a JSON record at
`.cc-autopilot/ideation_proposals/<TB-N>.json` with six keys
(`tb_id`, `proposed_at`, `focus_anchor`, `why_now`, `briefing_path`,
`blocked_on`). Subsequent terminal events for the same TB-N call
`reconcile_proposal_outcome` which appends a single `outcome` block
(decision_kind / decision_ts / decision_actor / commit / reason).
First-write-wins: a second reconcile call no-ops.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ap2 import tools
from ap2.board import Board
from ap2.config import Config
from ap2.init import init_project


# A goal.md that exposes a real `## Current focus` heading anchor + a
# Done-when bullet anchor so `extract_goal_anchor` has something to
# match against. Mirrors the project's own goal.md shape so the test
# isn't fragile to template tweaks.
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


# A briefing whose `## Goal` body cites the `## Current focus`
# heading verbatim AND carries a TB-164-shaped `Why now:` rationale.
# Used by every test that needs a record's seed-payload to contain
# real `focus_anchor` / `why_now` values.
_BRIEFING = (
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


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    """Project root with a real goal.md (so anchors are derivable) and
    the standard ap2 init layout. The default `init_project` writes a
    placeholder goal.md whose anchors filter to empty; we overwrite it
    with `_GOAL_MD` so `extract_goal_anchor` can match against real
    headings.
    """
    init_project(tmp_path)
    (tmp_path / "goal.md").write_text(_GOAL_MD)
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()
    return cfg


def _unwrap(res: dict) -> dict:
    assert not res.get("isError"), res
    return json.loads(res["content"][0]["text"])


# ---------------------------------------------------------------------------
# extract_goal_anchor / extract_why_now — public helpers for downstream
# signal-collection follow-ups (TB-189 retrospective verdict and beyond).


def test_extract_helpers_round_trip_briefing_to_substrings(tmp_path: Path):
    """The two public helpers exposed for the per-proposal record path
    must round-trip a representative briefing string to the expected
    substrings: `extract_goal_anchor` returns one of goal.md's anchors
    matched against the `## Goal` body, and `extract_why_now` returns
    the post-marker rationale paragraph.

    Pinned because TB-189 (`ap2 classify TB-N --delete-test`) and the
    future track-record-block prompt header both call these directly —
    a refactor that hides them again behind underscores would silently
    break the signal pipeline.
    """
    goal_md = tmp_path / "goal.md"
    goal_md.write_text(_GOAL_MD)

    anchor = tools.extract_goal_anchor(_BRIEFING, goal_md_path=goal_md)
    # The Goal body cites the `## Current focus: ideation quality signal
    # collection` heading verbatim, so the matched anchor is the
    # normalized form of that heading title.
    assert anchor is not None
    assert "ideation quality signal collection" in anchor

    why = tools.extract_why_now(_BRIEFING)
    assert why is not None
    # Rationale text post-marker / post-separator; the validator's
    # length check sees the same text.
    assert "goal-shaped pro-forma compliance" in why
    assert why.startswith("closes")  # leading separator stripped


def test_extract_helpers_return_none_on_missing_inputs(tmp_path: Path):
    """No briefing → both helpers return None. No goal.md (or all-
    placeholder goal.md) → `extract_goal_anchor` returns None even on
    a real briefing. `extract_why_now` returns None when the briefing
    has a `## Goal` section but no `Why now` marker.
    """
    # Empty briefing.
    assert tools.extract_goal_anchor("", goal_md_path=tmp_path / "goal.md") is None
    assert tools.extract_why_now("") is None

    # Briefing without a `Why now` marker → None from extract_why_now.
    no_marker = (
        "# x\n\n## Goal\n\nJust prose, no marker.\n\n"
        "## Scope\n\n- foo\n\n"
        "## Design\n\nx\n\n"
        "## Verification\n- `:` — y\n\n"
        "## Out of scope\n\n- z\n"
    )
    assert tools.extract_why_now(no_marker) is None

    # No goal_md_path → no anchors → None.
    assert tools.extract_goal_anchor(_BRIEFING, goal_md_path=None) is None


# ---------------------------------------------------------------------------
# Seed-write at proposal time.


def test_record_written_on_add_backlog_with_review_blocker(cfg: Config):
    """Calling `do_board_edit({"action": "add_backlog", "blocked_on":
    "review", ...})` produces a JSON file at
    `.cc-autopilot/ideation_proposals/<TB-N>.json` with the six
    required keys: `tb_id`, `proposed_at`, `focus_anchor`, `why_now`,
    `briefing_path`, `blocked_on`. (TB-188 verification bullet.)
    """
    res = tools.do_board_edit(
        cfg,
        {
            "action": "add_backlog",
            "title": "an ideation proposal",
            "blocked_on": "review",
            "briefing": _BRIEFING,
        },
    )
    body = _unwrap(res)
    tb_id = body["task_id"]

    record_path = tools.proposal_record_path(cfg, tb_id)
    assert record_path.exists(), (
        f"expected {record_path} to be written at proposal time"
    )
    record = json.loads(record_path.read_text())

    # Six required keys, present and correctly populated.
    assert record["tb_id"] == tb_id
    # ISO-Z timestamp.
    assert isinstance(record["proposed_at"], str)
    assert record["proposed_at"].endswith("Z")
    # Focus anchor matched against goal.md (the `## Current focus`
    # heading our `_BRIEFING` cites).
    assert record["focus_anchor"] is not None
    assert "ideation quality signal collection" in record["focus_anchor"]
    # Why-now extracted from the briefing's `## Goal` body.
    assert record["why_now"] is not None
    assert "goal-shaped pro-forma compliance" in record["why_now"]
    # Briefing path round-trips the relative path written under
    # `.cc-autopilot/tasks/`.
    assert record["briefing_path"] == body["briefing_path"]
    # Raw blocked_on body round-trips so legacy / mixed-blocker shapes
    # (TB-187: `review,TB-N`) are recoverable.
    assert record["blocked_on"] == "review"

    # Directory exists (per the briefing's `test -d` verification bullet).
    assert tools.ideation_proposals_dir(cfg).is_dir()


def test_no_record_for_non_review_add_backlog(cfg: Config):
    """`do_board_edit({"action": "add_backlog", "blocked_on": ""})` does
    NOT write a record file — the operator-driven path (e.g. `ap2 add
    TB-N` without the review marker) is not an ideation proposal and
    must skip silently. (TB-188 verification bullet.)
    """
    res = tools.do_board_edit(
        cfg,
        {
            "action": "add_backlog",
            "title": "an operator-driven add",
            "blocked_on": "",
            "briefing": _BRIEFING,
        },
    )
    body = _unwrap(res)
    tb_id = body["task_id"]

    record_path = tools.proposal_record_path(cfg, tb_id)
    assert not record_path.exists(), (
        f"unexpectedly wrote {record_path} for a non-review add_backlog"
    )


def test_record_round_trips_mixed_blocker_csv(cfg: Config):
    """`blocked_on: "review,TB-7"` (TB-187 mixed shape) still triggers
    a record write — the `review` token detection is csv-aware — and
    the `blocked_on` field round-trips the raw csv body so downstream
    consumers can recover the mixed-blocker semantics.
    """
    # Seed a Backlog task so the blocker reference doesn't matter
    # structurally; we only care about the record's `blocked_on` field.
    res = tools.do_board_edit(
        cfg,
        {
            "action": "add_backlog",
            "title": "mixed blocker proposal",
            "blocked_on": "review,TB-7",
            "briefing": _BRIEFING,
        },
    )
    body = _unwrap(res)
    record = json.loads(
        tools.proposal_record_path(cfg, body["task_id"]).read_text()
    )
    assert record["blocked_on"] == "review,TB-7"


# ---------------------------------------------------------------------------
# Terminal-event reconciliation.


def _seed_proposal_record(cfg: Config, tb_id: str = "TB-700") -> Path:
    """Write a seed-shaped record via the public helper. The record is
    what the proposal-time write produces — used by reconciliation
    tests as a starting point (avoids re-running `do_board_edit` for
    each terminal-event scenario).
    """
    return tools.write_ideation_proposal_record(
        cfg,
        tb_id=tb_id,
        blocked_on="review",
        briefing_text=_BRIEFING,
        briefing_rel=".cc-autopilot/tasks/seed.md",
    )


def test_outcome_reconciled_on_task_complete(cfg: Config):
    """A `task_complete`-equivalent event for the proposal's TB-N
    appends an `outcome` block with `decision_kind == "completed"`
    and the commit SHA populated. (TB-188 verification bullet.)
    """
    tb_id = "TB-701"
    _seed_proposal_record(cfg, tb_id)

    written = tools.reconcile_proposal_outcome(
        cfg, tb_id,
        decision_kind="completed",
        decision_actor="daemon",
        commit="abc1234",
    )
    assert written is not None

    record = json.loads(tools.proposal_record_path(cfg, tb_id).read_text())
    outcome = record["outcome"]
    assert outcome["decision_kind"] == "completed"
    assert outcome["decision_actor"] == "daemon"
    assert outcome["commit"] == "abc1234"
    assert isinstance(outcome["decision_ts"], str)
    assert outcome["decision_ts"].endswith("Z")
    # Reason is empty for completion (only reject / delete carry one).
    assert outcome["reason"] == ""


def test_outcome_reconciled_on_operator_reject(cfg: Config):
    """An operator-queue `reject TB-N --reason "..."` drains to an
    `outcome` block with `decision_kind == "rejected"` and the reason
    text from operator_log.md. (TB-188 verification bullet.)

    Exercised through the real op-queue surface (`do_operator_queue_append`
    → `drain_operator_queue`) so the wiring inside `_apply_operator_op`
    is exercised end-to-end, including the operator_log.md write that
    the reason field traces back to.
    """
    # Seed the proposal on the board (Backlog + @blocked:review) so
    # the reject queue-append snapshot validation accepts it.
    board = Board.load(cfg.tasks_file)
    board.add(
        "Backlog",
        task_id="TB-702",
        title="rejected proposal",
        meta={"blocked": "review"},
    )
    board.save()
    # Seed the per-proposal record (mimics what ideation's add_backlog
    # would have written at proposal time).
    _seed_proposal_record(cfg, "TB-702")

    # Real reject flow: queue-append + drain.
    reason_text = "no measurable signal in last 3 cycles"
    tools.do_operator_queue_append(
        cfg,
        {"op": "reject", "task_id": "TB-702", "reason": reason_text},
    )
    tools.drain_operator_queue(cfg)

    record = json.loads(
        tools.proposal_record_path(cfg, "TB-702").read_text()
    )
    outcome = record["outcome"]
    assert outcome["decision_kind"] == "rejected"
    assert outcome["decision_actor"] == "operator"
    assert outcome["reason"] == reason_text
    # The same reason text lives in operator_log.md (the canonical
    # human-readable surface) — proves the record's `reason` is the
    # SAME text the briefing's "from the matching operator_log.md
    # line" verification clause requires.
    log = (cfg.project_root / ".cc-autopilot" / "operator_log.md").read_text()
    assert reason_text in log


def test_outcome_reconciled_on_operator_delete(cfg: Config):
    """Delete is also a terminal event for proposals (operator decided
    against the task entirely, not just rejected the proposal shape).
    Decision_kind="deleted", actor="operator", reason="" (the delete
    audit line carries no free-text reason).
    """
    board = Board.load(cfg.tasks_file)
    board.add("Backlog", task_id="TB-703", title="to be deleted")
    board.save()
    _seed_proposal_record(cfg, "TB-703")

    tools.do_operator_queue_append(
        cfg, {"op": "delete", "task_id": "TB-703"}
    )
    tools.drain_operator_queue(cfg)

    record = json.loads(
        tools.proposal_record_path(cfg, "TB-703").read_text()
    )
    outcome = record["outcome"]
    assert outcome["decision_kind"] == "deleted"
    assert outcome["decision_actor"] == "operator"
    assert outcome["reason"] == ""


def test_outcome_reconciled_on_operator_approve(cfg: Config):
    """Drain-side `approve` reconciles `decision_kind="approved"`. The
    synchronous `do_board_edit({"action": "approve", ...})` surface
    behaves identically (covered separately below)."""
    board = Board.load(cfg.tasks_file)
    board.add(
        "Backlog",
        task_id="TB-704",
        title="approved proposal",
        meta={"blocked": "review"},
    )
    board.save()
    _seed_proposal_record(cfg, "TB-704")

    tools.do_operator_queue_append(
        cfg, {"op": "approve", "task_id": "TB-704"}
    )
    tools.drain_operator_queue(cfg)

    record = json.loads(
        tools.proposal_record_path(cfg, "TB-704").read_text()
    )
    outcome = record["outcome"]
    assert outcome["decision_kind"] == "approved"
    assert outcome["decision_actor"] == "operator"


def test_outcome_reconciled_on_synchronous_approve(cfg: Config):
    """`do_board_edit({"action": "approve", ...})` (the idle-path
    approve, used by direct CLI / control-agent callers) reconciles
    identically to the drain-side path. Both surfaces must land the
    same record-shape — otherwise the same operator action would
    produce different signals depending on which surface fired it.
    """
    board = Board.load(cfg.tasks_file)
    board.add(
        "Backlog",
        task_id="TB-705",
        title="approved via direct path",
        meta={"blocked": "review"},
    )
    board.save()
    _seed_proposal_record(cfg, "TB-705")

    tools.do_board_edit(
        cfg, {"action": "approve", "task_id": "TB-705"},
    )

    record = json.loads(
        tools.proposal_record_path(cfg, "TB-705").read_text()
    )
    outcome = record["outcome"]
    assert outcome["decision_kind"] == "approved"
    assert outcome["decision_actor"] == "operator"


# ---------------------------------------------------------------------------
# Idempotency + missing-record + bad-input safety nets.


def test_reconcile_no_op_when_record_missing(cfg: Config):
    """No record on disk → reconcile is a silent no-op. Pre-TB-188
    proposals have no record file; reconciling them must not crash and
    must not create a stub record (which would have no seed fields).
    """
    written = tools.reconcile_proposal_outcome(
        cfg, "TB-9999",
        decision_kind="completed",
        decision_actor="daemon",
    )
    assert written is None
    assert not tools.proposal_record_path(cfg, "TB-9999").exists()


def test_reconcile_first_write_wins(cfg: Config):
    """Records are append-once-then-amend (briefing's design): a second
    reconcile call against an already-reconciled record is a no-op.
    Defends against a daemon restart re-emitting a terminal event mid-
    drain — the second call must not clobber the first outcome.
    """
    tb_id = "TB-706"
    _seed_proposal_record(cfg, tb_id)

    first = tools.reconcile_proposal_outcome(
        cfg, tb_id,
        decision_kind="approved",
        decision_actor="operator",
    )
    assert first is not None
    record_after_first = json.loads(
        tools.proposal_record_path(cfg, tb_id).read_text()
    )

    # Second call: different decision_kind. Must no-op.
    second = tools.reconcile_proposal_outcome(
        cfg, tb_id,
        decision_kind="completed",
        decision_actor="daemon",
        commit="def5678",
    )
    assert second is None

    record_after_second = json.loads(
        tools.proposal_record_path(cfg, tb_id).read_text()
    )
    assert record_after_second == record_after_first


def test_reconcile_unknown_decision_kind_no_ops(cfg: Config):
    """A wrong / typo'd decision_kind silently no-ops rather than
    poisoning the record with a kind downstream consumers don't know
    how to interpret. The valid set is `_PROPOSAL_DECISION_KINDS`.
    """
    tb_id = "TB-707"
    _seed_proposal_record(cfg, tb_id)

    written = tools.reconcile_proposal_outcome(
        cfg, tb_id,
        decision_kind="bogus",  # not in the valid tuple
        decision_actor="daemon",
    )
    assert written is None
    record = json.loads(tools.proposal_record_path(cfg, tb_id).read_text())
    assert "outcome" not in record


# ---------------------------------------------------------------------------
# TASK_AGENT_FENCED_PATHS wiring (the briefing's grep-bullet check).


def test_ideation_proposals_dir_in_fenced_paths():
    """The directory must be in `TASK_AGENT_FENCED_PATHS` so the
    SDK's `disallowed_tools` rejects task-agent edits. Mirrors the
    `operator_queue.jsonl` precedent (TB-143). The directory itself
    is a single fence anchor — defense-in-depth, not airtight (the
    SDK can't glob into the dir's children), but the prompt-header
    fence covers the soft-enforcement gap.
    """
    assert ".cc-autopilot/ideation_proposals" in tools.TASK_AGENT_FENCED_PATHS

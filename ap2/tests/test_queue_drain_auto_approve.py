"""TB-293: queue-drain `add_backlog` must run the auto-approve gate chain.

Pin the gate-chain symmetry between the two `add_backlog` surfaces:

  - direct path: `ap2/board_edits.py:do_board_edit`'s `add_backlog`
    branch (TB-232 chain — tags → freeze-threshold → per-task-token-cap
    → window-token-cap → terminal strip-or-simulate).
  - queue-drain path: `ap2/operator_queue.py:_apply_operator_op`'s
    `add_backlog` branch (TB-293 mirror — same chain, lazy-import
    `daemon.evaluate_auto_approve_decision`, post-`board.add(...)`
    so the strip can find the row by task_id).

The 2026-05-26 TB-290 incident motivated this: an ideation-authored
proposal queued via `operator_queue_append op=add_backlog` sat
stranded in Backlog for ~10 hours despite `AP2_AUTO_APPROVE=1`
because the queue-drain branch never invoked the gate. The mirror
closes the asymmetry.

The cases below exercise the queue-drain path directly via
`tools.drain_operator_queue(cfg)` after an `operator_queue_append`
to keep the test focused on the new branch — not on `do_board_edit`'s
existing TB-223/TB-232 coverage.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ap2 import events, tools
from ap2.board import Board
from ap2.config import Config
from ap2.init import init_project
from ap2.tests._briefing_fixtures import canonical_briefing


# Minimal goal.md whose `## Current focus` heading + `## Done when` bullets
# expose anchors the briefing validator (`_validate_briefing_structure`
# step 4 / 5) can match against. Mirrors `test_tb223_auto_approve.py`'s
# fixture so the structural gate sees real anchors regardless of which
# surface the test exercises.
_GOAL_MD = (
    "# Project Goals\n\n"
    "## Mission\n\n"
    "Drive the project toward end-to-end automation.\n\n"
    "## Done when\n\n"
    "- An operator can point ap2 at a fresh project and walk away "
    "without intervention.\n\n"
    "## Current focus: end-to-end automation\n\n"
    "Close the manual-approval bottleneck.\n\n"
    "## Non-goals\n\n"
    "- something out of scope.\n"
)


# Briefing whose `## Goal` cites the `## Current focus` heading verbatim
# and carries a TB-164-shaped `Why now:` rationale.
_BRIEFING = (
    "# A queue-drain auto-approve test briefing\n\n"
    "## Goal\n\n"
    "Closes the end-to-end automation gap that the current focus "
    "names (cites the `## Current focus: end-to-end automation` "
    "heading).\n\n"
    "Why now: closes the manual-approval bottleneck — without this "
    "the walk-away promise breaks under typical proposal load.\n\n"
    "## Scope\n\n- foo.py\n\n"
    "## Design\n\nDirect edit.\n\n"
    "## Verification\n- `uv run pytest -q` — gates pass\n\n"
    "## Out of scope\n\n- nothing\n"
)


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    """Project root with the standard ap2 init layout and a real goal.md
    so the briefing-structural gate at queue-append time has anchors to
    match. `init_project` seeds the placeholder goal.md; we overwrite
    it with `_GOAL_MD`."""
    init_project(tmp_path)
    (tmp_path / "goal.md").write_text(_GOAL_MD)
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()
    return cfg


def _unwrap(res: dict) -> dict:
    assert not res.get("isError"), res
    return json.loads(res["content"][0]["text"])


def _queue_and_drain(cfg: Config, payload: dict) -> str:
    """Append `payload` (an `add_backlog` op) to the operator queue,
    drain it, and return the allocated TB-N. Common helper across the
    cases below — keeps each test focused on the post-drain assertions.
    """
    payload = {**payload, "op": payload.get("op", "add_backlog"),
               "briefing": payload.get("briefing", _BRIEFING)}
    res = tools.do_operator_queue_append(cfg, payload)
    body = _unwrap(res)
    drain_res = tools.drain_operator_queue(cfg)
    assert drain_res["applied"] == 1, drain_res
    return body["task_id"]


# ===========================================================================
# (1) Auto-approve gates passing → review token stripped + event fired.
# ===========================================================================


def test_drain_add_backlog_with_review_strips_when_auto_approve_on(
    cfg: Config, monkeypatch,
):
    """With `AP2_AUTO_APPROVE=1` and all gates passing, draining an
    `add_backlog` op whose meta carries `blocked=review` strips the
    review token from the landed task and emits `auto_approved` with
    `task=TB-N` + `knob=1`. Mirrors `do_board_edit`'s TB-223 set-knob
    case at the queue-drain surface."""
    monkeypatch.setenv("AP2_COMPONENTS_AUTO_APPROVE_ENABLED", "1")
    monkeypatch.delenv("AP2_COMPONENTS_AUTO_APPROVE_DRY_RUN", raising=False)
    monkeypatch.delenv("AP2_COMPONENTS_AUTO_APPROVE_GATE_TAGS", raising=False)

    tb_id = _queue_and_drain(cfg, {
        "title": "queue-drained auto-approved feature",
        "blocked_on": "review",
        "tags": ["#autopilot"],
    })

    board = Board.load(cfg.tasks_file)
    loc = board.find(tb_id)
    assert loc is not None
    section, idx = loc
    line = board.sections[section][idx]
    assert "@blocked:review" not in line, (
        f"AP2_AUTO_APPROVE=1 + queue-drain must strip `@blocked:review`; "
        f"got: {line!r}"
    )
    # The review-only blocker should leave NO `@blocked:` codespan
    # behind (matches `_approve_review_token` clean-line behavior).
    assert "@blocked:" not in line, (
        f"review-only blocker stripped → no leftover empty codespan; "
        f"got: {line!r}"
    )

    evts = events.tail(cfg.events_file, 50)
    auto = [e for e in evts if e.get("type") == "auto_approved"]
    assert len(auto) == 1, evts
    assert auto[0]["task"] == tb_id
    assert auto[0]["knob"] == "1"


# ===========================================================================
# (2) Knob unset → review token preserved + no event fires.
# ===========================================================================


def test_drain_add_backlog_with_review_preserves_when_auto_approve_unset(
    cfg: Config, monkeypatch,
):
    """Default behavior (`AP2_AUTO_APPROVE` unset): the queue-drain
    handler must NOT strip the review token, and no `auto_approved`
    event fires. Pins the safe default so a future refactor that
    silently flips the knob trips here."""
    monkeypatch.delenv("AP2_AUTO_APPROVE", raising=False)
    monkeypatch.delenv("AP2_AUTO_APPROVE_DRY_RUN", raising=False)

    tb_id = _queue_and_drain(cfg, {
        "title": "queue-drained default-mode add",
        "blocked_on": "review",
        "tags": ["#autopilot"],
    })

    board = Board.load(cfg.tasks_file)
    loc = board.find(tb_id)
    assert loc is not None
    section, idx = loc
    line = board.sections[section][idx]
    assert "@blocked:review" in line, (
        f"`AP2_AUTO_APPROVE` unset must preserve `@blocked:review` on "
        f"the queue-drained row; got: {line!r}"
    )

    evts = events.tail(cfg.events_file, 50)
    assert [e for e in evts if e.get("type") == "auto_approved"] == [], (
        "AP2_AUTO_APPROVE unset must NOT emit auto_approved events"
    )
    assert [e for e in evts if e.get("type") == "would_auto_approve"] == [], (
        "AP2_AUTO_APPROVE unset must NOT emit would_auto_approve events"
    )


# ===========================================================================
# (3) Dry-run mode → review preserved + `would_auto_approve dry_run=True`.
# ===========================================================================


def test_drain_add_backlog_dry_run_emits_simulated_event(
    cfg: Config, monkeypatch,
):
    """With `AP2_AUTO_APPROVE=1` AND `AP2_AUTO_APPROVE_DRY_RUN=1`, the
    queue-drain handler keeps the review token but emits a
    `would_auto_approve` event with `dry_run=True`. Mirrors
    `do_board_edit`'s TB-232 dry-run on-ramp at the queue surface."""
    monkeypatch.setenv("AP2_COMPONENTS_AUTO_APPROVE_ENABLED", "1")
    monkeypatch.setenv("AP2_COMPONENTS_AUTO_APPROVE_DRY_RUN", "1")
    monkeypatch.delenv("AP2_COMPONENTS_AUTO_APPROVE_GATE_TAGS", raising=False)

    tb_id = _queue_and_drain(cfg, {
        "title": "queue-drained dry-run feature",
        "blocked_on": "review",
        "tags": ["#autopilot"],
    })

    board = Board.load(cfg.tasks_file)
    loc = board.find(tb_id)
    assert loc is not None
    section, idx = loc
    line = board.sections[section][idx]
    assert "@blocked:review" in line, (
        f"dry-run mode must preserve `@blocked:review` on the queue-"
        f"drained row; got: {line!r}"
    )

    evts = events.tail(cfg.events_file, 50)
    would = [e for e in evts if e.get("type") == "would_auto_approve"]
    assert len(would) == 1, evts
    assert would[0]["task"] == tb_id
    assert would[0]["knob"] == "1"
    assert would[0]["dry_run"] is True
    # And `auto_approved` does NOT fire — dry-run path stays
    # simulation-only.
    assert [e for e in evts if e.get("type") == "auto_approved"] == [], (
        "dry-run mode must NOT emit auto_approved events"
    )


# ===========================================================================
# (4) No review blocker → no gate evaluation; row lands with its blockers.
# ===========================================================================


def test_drain_add_backlog_without_review_blocker_skips_gate(
    cfg: Config, monkeypatch,
):
    """When `meta` carries no `blocked` key (or it carries only TB-N
    dependency tokens), the queue-drain handler must NOT invoke the
    auto-approve gate at all — no `auto_approved` / `would_auto_approve`
    event fires, the row lands with its incoming blockers intact.
    Pins the cheap-skip behavior so the gate isn't paid for
    operator-driven adds."""
    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")
    monkeypatch.setenv("AP2_AUTO_APPROVE_DRY_RUN", "1")

    # Case A: no blockers at all.
    tb_id_a = _queue_and_drain(cfg, {
        "title": "queue-drained no-blocker add",
        "tags": ["#autopilot"],
    })
    board = Board.load(cfg.tasks_file)
    loc = board.find(tb_id_a)
    assert loc is not None
    section, idx = loc
    line_a = board.sections[section][idx]
    assert "@blocked:" not in line_a, (
        f"add without blocker must land clean; got: {line_a!r}"
    )

    # Case B: blocker that's a TB-N dependency, NOT review.
    tb_id_b = _queue_and_drain(cfg, {
        "title": "queue-drained TB-dep blocker",
        "blocked_on": "TB-9999",
        "tags": ["#autopilot"],
    })
    board = Board.load(cfg.tasks_file)
    loc = board.find(tb_id_b)
    assert loc is not None
    section, idx = loc
    line_b = board.sections[section][idx]
    assert "@blocked:TB-9999" in line_b, (
        f"TB-N dependency blocker must land intact; got: {line_b!r}"
    )

    evts = events.tail(cfg.events_file, 50)
    assert [e for e in evts if e.get("type") == "auto_approved"] == [], (
        "non-review-bearing adds must NOT emit auto_approved"
    )
    assert [e for e in evts if e.get("type") == "would_auto_approve"] == [], (
        "non-review-bearing adds must NOT emit would_auto_approve"
    )


# ===========================================================================
# (5) Freeze-threshold trip → review preserved, no event (gate noop).
# ===========================================================================


def test_drain_add_backlog_freeze_threshold_trip_blocks_strip(
    cfg: Config, monkeypatch,
):
    """In dry-run mode, when `_auto_approve_paused` returns True (TB-223
    freeze-threshold tripped), the gate evaluates to `"noop"` — no
    strip, no `would_auto_approve` (the simulation matches what the
    real path would do at dispatch time, including the halt). Seeded
    by pre-recording N consecutive `task_complete` + `retry_exhausted`
    failures so the threshold check returns True. The dry-run terminal
    branch is the surface where `evaluate_auto_approve_decision`
    *visibly* honors freeze-paused in its return value; real mode
    returns `"strip"` and defers enforcement to the dispatch-time gate
    in `_tick`, which a queue-drain unit test can't observe in
    isolation."""
    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")
    monkeypatch.setenv("AP2_AUTO_APPROVE_DRY_RUN", "1")
    monkeypatch.setenv("AP2_AUTO_APPROVE_FREEZE_THRESHOLD", "3")
    monkeypatch.delenv("AP2_AUTO_APPROVE_GATE_TAGS", raising=False)

    # Seed three consecutive task_complete-with-retry-exhausted failures
    # so `_auto_approve_paused` returns True. Mirrors the seeding pattern
    # in test_tb223_auto_approve.py:_seed_completions.
    for tid in ("TB-9001", "TB-9002", "TB-9003"):
        events.append(
            cfg.events_file, "task_complete",
            task=tid, status="failed", commit="", summary="seeded",
        )
    events.append(
        cfg.events_file, "retry_exhausted",
        task="TB-9003", attempts=3, last_status="failed",
    )

    tb_id = _queue_and_drain(cfg, {
        "title": "queue-drained freeze-tripped",
        "blocked_on": "review",
        "tags": ["#autopilot"],
    })

    board = Board.load(cfg.tasks_file)
    loc = board.find(tb_id)
    assert loc is not None
    section, idx = loc
    line = board.sections[section][idx]
    assert "@blocked:review" in line, (
        f"freeze-threshold trip + dry-run must keep `@blocked:review` "
        f"on the queue-drained row; got: {line!r}"
    )

    evts = events.tail(cfg.events_file, 50)
    assert [e for e in evts if e.get("type") == "auto_approved"] == [], (
        "freeze-threshold gate=noop must NOT emit auto_approved"
    )
    assert [e for e in evts if e.get("type") == "would_auto_approve"] == [], (
        "freeze-threshold gate=noop in dry-run must NOT emit "
        "would_auto_approve either (simulation matches real-path halt)"
    )


def test_drain_add_backlog_gate_tag_opt_out_skips_strip(
    cfg: Config, monkeypatch,
):
    """A proposal carrying any `AP2_AUTO_APPROVE_GATE_TAGS` tag (default
    `#breaking-change,#high-risk`) opts out of auto-approve entirely —
    `ideation.should_auto_approve(tags)` returns False, the gate
    short-circuits to `"noop"`, the review token is preserved, and no
    audit event fires. Pins the safety opt-out at the queue-drain
    surface so a future refactor that drops the tags-gate intersection
    trips here."""
    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")
    monkeypatch.delenv("AP2_AUTO_APPROVE_DRY_RUN", raising=False)
    monkeypatch.delenv("AP2_AUTO_APPROVE_GATE_TAGS", raising=False)

    tb_id = _queue_and_drain(cfg, {
        "title": "queue-drained breaking-change proposal",
        "blocked_on": "review",
        "tags": ["#breaking-change", "#autopilot"],
    })

    board = Board.load(cfg.tasks_file)
    loc = board.find(tb_id)
    assert loc is not None
    section, idx = loc
    line = board.sections[section][idx]
    assert "@blocked:review" in line, (
        f"tags opt-out must keep `@blocked:review` even with "
        f"AP2_AUTO_APPROVE=1; got: {line!r}"
    )

    evts = events.tail(cfg.events_file, 50)
    assert [e for e in evts if e.get("type") == "auto_approved"] == [], (
        "tags opt-out → gate=noop, must NOT emit auto_approved"
    )


# ===========================================================================
# (6) `add_ready` / `add_frozen` never invoke the gate.
# ===========================================================================


def test_drain_add_ready_skips_auto_approve_gate(cfg: Config, monkeypatch):
    """`add_ready` ops never carry review blockers in any current code
    path (Ready means already-approved by definition). The TB-293
    mirror is scoped to `add_backlog` only — assert `add_ready` skips
    the gate even when the master switch is on."""
    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")
    monkeypatch.delenv("AP2_AUTO_APPROVE_DRY_RUN", raising=False)

    tb_id = _queue_and_drain(cfg, {
        "op": "add_ready",
        "title": "queue-drained ready add",
        # Operator-side `add_ready` doesn't carry review by convention;
        # test the absence of gate evaluation by setting NO blocker.
        "tags": ["#autopilot"],
    })

    board = Board.load(cfg.tasks_file)
    assert board.find(tb_id) is not None
    evts = events.tail(cfg.events_file, 50)
    assert [e for e in evts if e.get("type") == "auto_approved"] == [], (
        "`add_ready` must NOT trigger the auto-approve gate"
    )
    assert [e for e in evts if e.get("type") == "would_auto_approve"] == [], (
        "`add_ready` must NOT emit `would_auto_approve`"
    )


def test_drain_add_frozen_skips_auto_approve_gate(cfg: Config, monkeypatch):
    """`add_frozen` ops never carry review blockers in any current code
    path (Frozen is for retry-exhausted tasks, not new proposals).
    Same scope-pin as `add_ready` above."""
    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")

    tb_id = _queue_and_drain(cfg, {
        "op": "add_frozen",
        "title": "queue-drained frozen add",
        "tags": ["#autopilot"],
    })

    board = Board.load(cfg.tasks_file)
    assert board.find(tb_id) is not None
    evts = events.tail(cfg.events_file, 50)
    assert [e for e in evts if e.get("type") == "auto_approved"] == [], (
        "`add_frozen` must NOT trigger the auto-approve gate"
    )
    assert [e for e in evts if e.get("type") == "would_auto_approve"] == [], (
        "`add_frozen` must NOT emit `would_auto_approve`"
    )


# ===========================================================================
# (7) Per-proposal record seeding (TB-188) at the queue-drain surface.
# ===========================================================================


def test_drain_add_backlog_with_review_seeds_proposal_record(
    cfg: Config, monkeypatch,
):
    """Queue-drain `add_backlog` with a `review` blocker seeds the
    per-proposal record (TB-188) at
    `.cc-autopilot/ideation_proposals/<TB-N>.json`, mirroring the
    direct-path call at `board_edits.do_board_edit:278-288`. Without
    this seeding, ideation-proposed-but-queue-routed tasks would lack
    the structured signal feeding ideation's later track-record
    block."""
    monkeypatch.delenv("AP2_AUTO_APPROVE", raising=False)

    tb_id = _queue_and_drain(cfg, {
        "title": "queue-drained ideation proposal",
        "blocked_on": "review",
        "tags": ["#autopilot"],
    })

    record_path = tools.proposal_record_path(cfg, tb_id)
    assert record_path.exists(), (
        f"per-proposal record expected at {record_path} for queue-"
        f"drained `add_backlog` carrying review blocker"
    )
    record = json.loads(record_path.read_text())
    assert record["tb_id"] == tb_id
    assert record["blocked_on"] == "review"
    assert record.get("briefing_path"), (
        "proposal record must carry briefing_path so reconciliation "
        "can locate the briefing on later events"
    )


def test_drain_add_backlog_no_review_skips_proposal_record(
    cfg: Config, monkeypatch,
):
    """Symmetric to the seeding test: queue-drain `add_backlog`
    without a review token does NOT create a per-proposal record.
    Pins the operator-vs-ideation discrimination."""
    monkeypatch.delenv("AP2_AUTO_APPROVE", raising=False)

    tb_id = _queue_and_drain(cfg, {
        "title": "queue-drained operator-style add",
        "tags": ["#autopilot"],
    })

    record_path = tools.proposal_record_path(cfg, tb_id)
    assert not record_path.exists(), (
        f"non-review-bearing `add_backlog` must NOT seed a per-proposal "
        f"record; found one at {record_path}"
    )

"""TB-383: decouple auto-approve from `board_edit` into a loop pass (axis 3).

Pins the inverted control flow:

  - `board_edit`'s `add_backlog` path is POLICY-FREE — a proposal carrying
    `blocked_on="review"` lands with `@blocked:review` intact REGARDLESS of
    `AP2_AUTO_APPROVE`, and NO `auto_approved` / `would_auto_approve` event
    fires at mutation time.
  - The `auto_approve` component's loop pass (`run_auto_approve_pass`,
    registered as the PRE_DISPATCH `_tick_hook`) is what strips
    `@blocked:review` from gate-clearing Backlog tasks BETWEEN agent runs,
    reusing the existing `evaluate_auto_approve_decision` gate chain. It
    strips only when the master knob + tags + freeze/violation gates pass,
    and emits the `auto_approved` / `would_auto_approve` events with the
    same payloads the pre-TB-383 proposal-time site produced.

Why the inversion: the pre-TB-383 strip ran inside `board_edit`'s mutation
(mid-agent-run, where a task-agent snapshot could capture a half-applied
board), and the `should_auto_approve` tags policy squatted in `ideation.py`
— the cross-boundary knot that blocked the ideation extraction (goal.md
axis 4). Moving the decision to a discrete daemon loop pass + relocating the
tags policy into the component untangles it.

A refactor that re-introduces the inline strip in `board_edit`, drops the
loop pass, or moves the tags policy back into core trips a focused subset of
these tests with a diff-shaped error.
"""
from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path

import pytest

from ap2 import daemon, events, tools
from ap2.board import Board
from ap2.components.auto_approve import run_auto_approve_pass
from ap2.config import Config
from ap2.init import init_project
from ap2.registry import Phase, Registry


# Minimal goal.md whose `## Current focus` heading + `## Done when` bullets
# expose anchors the briefing validator can match against. Mirrors the
# fixture in `test_tb223_auto_approve.py`.
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


_BRIEFING = (
    "# A TB-383 loop-pass test briefing\n\n"
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

_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    init_project(tmp_path)
    (tmp_path / "goal.md").write_text(_GOAL_MD)
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()
    return cfg


def _unwrap(res: dict) -> dict:
    assert not res.get("isError"), res
    return json.loads(res["content"][0]["text"])


def _add_review_proposal(cfg: Config, *, title: str, tags=None) -> str:
    res = tools.do_board_edit(
        cfg,
        {
            "action": "add_backlog",
            "title": title,
            "blocked_on": "review",
            "briefing": _BRIEFING,
            "tags": tags or ["#autopilot"],
        },
    )
    return _unwrap(res)["task_id"]


def _line(cfg: Config, tb_id: str) -> str:
    board = Board.load(cfg.tasks_file)
    loc = board.find(tb_id)
    assert loc is not None, f"{tb_id} not on board"
    return board.sections[loc[0]][loc[1]]


# ===========================================================================
# (1) board_edit is policy-free: proposal born @blocked:review regardless
#     of AP2_AUTO_APPROVE, with no audit event at mutation time.
# ===========================================================================


@pytest.mark.parametrize("knob", [None, "1", "true"])
def test_board_edit_lands_blocked_review_regardless_of_knob(
    cfg: Config, monkeypatch, knob,
):
    """A `board_edit add_backlog` carrying `blocked_on="review"` lands with
    `@blocked:review` intact whether `AP2_AUTO_APPROVE` is unset OR set to a
    truthy value, and NO `auto_approved` / `would_auto_approve` event fires
    at mutation time. TB-383: the strip is no longer a mutation-time
    decision; the proposal is uniformly born blocked."""
    if knob is None:
        monkeypatch.delenv("AP2_AUTO_APPROVE", raising=False)
    else:
        monkeypatch.setenv("AP2_AUTO_APPROVE", knob)
    monkeypatch.delenv("AP2_AUTO_APPROVE_DRY_RUN", raising=False)

    tb_id = _add_review_proposal(cfg, title="born blocked")

    line = _line(cfg, tb_id)
    assert "@blocked:review" in line, (
        f"TB-383: `board_edit` is policy-free — the proposal must be born "
        f"`@blocked:review` even with AP2_AUTO_APPROVE={knob!r}; got: {line!r}"
    )

    evts = events.tail(cfg.events_file, 50)
    assert [e for e in evts if e.get("type") == "auto_approved"] == [], (
        "policy-free `board_edit` must NOT emit `auto_approved` at "
        "mutation time"
    )
    assert [e for e in evts if e.get("type") == "would_auto_approve"] == [], (
        "policy-free `board_edit` must NOT emit `would_auto_approve` at "
        "mutation time"
    )


# ===========================================================================
# (2) The loop pass strips only when the master knob + gates pass.
# ===========================================================================


def test_loop_pass_noop_when_knob_unset(cfg: Config, monkeypatch):
    """With `AP2_AUTO_APPROVE` unset, the loop pass is a no-op: the
    `@blocked:review` codespan survives and no `auto_approved` fires."""
    monkeypatch.delenv("AP2_AUTO_APPROVE", raising=False)

    tb_id = _add_review_proposal(cfg, title="knob off")
    run_auto_approve_pass(cfg)

    assert "@blocked:review" in _line(cfg, tb_id), (
        "loop pass must NOT strip review when AP2_AUTO_APPROVE is unset"
    )
    evts = events.tail(cfg.events_file, 50)
    assert [e for e in evts if e.get("type") == "auto_approved"] == []


def test_loop_pass_strips_when_knob_set_and_gates_pass(
    cfg: Config, monkeypatch,
):
    """With `AP2_AUTO_APPROVE=1` and a non-gate-tagged proposal, the loop
    pass strips `@blocked:review` and emits `auto_approved` (`task=`,
    `knob=`) — the exact payload the pre-TB-383 proposal-time site
    produced, just from the between-runs site."""
    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")
    monkeypatch.delenv("AP2_AUTO_APPROVE_DRY_RUN", raising=False)
    monkeypatch.delenv("AP2_AUTO_APPROVE_GATE_TAGS", raising=False)

    tb_id = _add_review_proposal(cfg, title="strip me")
    # Born blocked.
    assert "@blocked:review" in _line(cfg, tb_id)

    run_auto_approve_pass(cfg)

    line = _line(cfg, tb_id)
    assert "@blocked:review" not in line, line
    assert "@blocked:" not in line, (
        f"review-only blocker stripped → no leftover empty codespan; "
        f"got: {line!r}"
    )
    auto = [
        e for e in events.tail(cfg.events_file, 50)
        if e.get("type") == "auto_approved"
    ]
    assert len(auto) == 1, auto
    assert auto[0]["task"] == tb_id
    assert auto[0]["knob"] == "1"


def test_loop_pass_retains_review_for_gate_tagged_proposal(
    cfg: Config, monkeypatch,
):
    """A proposal carrying a default gate-tag (`#breaking-change`) keeps
    `@blocked:review` even after the loop pass runs with the master knob
    on — the tags policy (relocated into the component) short-circuits the
    gate chain to "noop"."""
    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")
    monkeypatch.delenv("AP2_AUTO_APPROVE_DRY_RUN", raising=False)
    monkeypatch.delenv("AP2_AUTO_APPROVE_GATE_TAGS", raising=False)

    tb_id = _add_review_proposal(
        cfg, title="risky", tags=["#autopilot", "#breaking-change"],
    )
    run_auto_approve_pass(cfg)

    assert "@blocked:review" in _line(cfg, tb_id), (
        "gate-tag must preserve review through the loop pass"
    )
    assert [
        e for e in events.tail(cfg.events_file, 50)
        if e.get("type") == "auto_approved"
    ] == []


def test_loop_pass_is_idempotent_after_strip(cfg: Config, monkeypatch):
    """Running the loop pass twice strips once and emits exactly one
    `auto_approved` — the second pass finds no `review` token to act on."""
    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")
    monkeypatch.delenv("AP2_AUTO_APPROVE_DRY_RUN", raising=False)

    tb_id = _add_review_proposal(cfg, title="once only")
    run_auto_approve_pass(cfg)
    run_auto_approve_pass(cfg)

    auto = [
        e for e in events.tail(cfg.events_file, 50)
        if e.get("type") == "auto_approved" and e.get("task") == tb_id
    ]
    assert len(auto) == 1, auto


# ===========================================================================
# (3) Dry-run: the loop pass emits a one-shot would_auto_approve and keeps
#     the codespan.
# ===========================================================================


def test_loop_pass_dry_run_emits_once_and_preserves_codespan(
    cfg: Config, monkeypatch,
):
    """In dry-run mode the loop pass emits `would_auto_approve dry_run=True`
    and leaves `@blocked:review` intact. Because the codespan is preserved,
    the task stays in the pass's candidate set — the dedup gate ensures the
    event fires exactly ONCE across repeated passes (preserving the
    pre-TB-383 emit-once semantics + the 24h dry-run counter)."""
    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")
    monkeypatch.setenv("AP2_AUTO_APPROVE_DRY_RUN", "1")
    monkeypatch.delenv("AP2_AUTO_APPROVE_GATE_TAGS", raising=False)

    tb_id = _add_review_proposal(cfg, title="dry run")
    run_auto_approve_pass(cfg)
    run_auto_approve_pass(cfg)  # second pass must NOT re-emit

    line = _line(cfg, tb_id)
    assert "@blocked:review" in line, (
        f"dry-run must preserve the codespan; got: {line!r}"
    )
    would = [
        e for e in events.tail(cfg.events_file, 50)
        if e.get("type") == "would_auto_approve"
    ]
    assert len(would) == 1, would
    assert would[0]["task"] == tb_id
    assert would[0]["knob"] == "1"
    assert would[0]["dry_run"] is True
    assert [
        e for e in events.tail(cfg.events_file, 50)
        if e.get("type") == "auto_approved"
    ] == [], "dry-run must NOT emit auto_approved"


# ===========================================================================
# (4) Structural pins: board_edit policy-free + loop pass on PRE_DISPATCH.
# ===========================================================================


def test_board_edit_source_has_no_inline_gate_call():
    """`ap2/board_edits.py` no longer references
    `evaluate_auto_approve_decision` — the briefing's grep-shape
    Verification bullet pinned to source."""
    src = (_REPO_ROOT / "ap2/board_edits.py").read_text(encoding="utf-8")
    assert "evaluate_auto_approve_decision" not in src, (
        "TB-383: `board_edit` must not evaluate the auto-approve gate "
        "inline; the add_backlog branch is policy-free."
    )


def test_loop_pass_registered_on_pre_dispatch():
    """The auto_approve manifest registers its `_tick_hook` on
    `Phase.PRE_DISPATCH` (so the daemon runs the pass before the dispatch
    stage promotes Ready tasks) and the hook delegates to
    `run_auto_approve_pass` — no longer the pre-TB-383 POST_DISPATCH
    no-op."""
    registry = Registry.discover()
    manifest = registry.get("auto_approve")
    phases = [phase for phase, _ in manifest.tick_hooks]
    assert phases == [Phase.PRE_DISPATCH], (
        f"TB-383: auto_approve tick hook must be on PRE_DISPATCH; got "
        f"{[p.name for p in phases]}"
    )
    pre_hooks = registry.tick_hooks(Phase.PRE_DISPATCH)
    assert manifest.hook_points["tick_hook"] in pre_hooks
    # The hook body delegates to the real loop pass (not a no-op return).
    hook_src = inspect.getsource(manifest.hook_points["tick_hook"])
    assert "run_auto_approve_pass" in hook_src, (
        "TB-383: the tick hook must delegate to `run_auto_approve_pass`, "
        "not be a no-op placeholder."
    )


# ===========================================================================
# (5) End-to-end through `_tick`: born-blocked proposal is auto-approved by
#     the loop pass and promoted in the same tick.
# ===========================================================================


def _stub_tick_quiet(monkeypatch) -> None:
    """Neutralize every `_tick` stage except the PRE_DISPATCH walk (the
    loop pass) and the auto-promote/dispatch step. Mirrors the helper in
    `test_tb223_auto_approve.py`."""
    monkeypatch.setattr(
        tools, "drain_operator_queue",
        lambda cfg: {"applied": 0, "touched_paths": [], "force_ideate": False},
    )

    async def _noop_sweep(cfg, sdk):  # noqa: ARG001
        return None

    monkeypatch.setattr(daemon, "_sweep_pipeline_pending", _noop_sweep)
    monkeypatch.setattr(daemon, "_maybe_auto_diagnose", lambda cfg: None)

    async def _noop_async(*a, **kw):  # noqa: ARG001
        return None

    from ap2 import ideation as _ideation
    monkeypatch.setattr(_ideation, "_maybe_ideate", _noop_async)
    monkeypatch.setattr(_ideation, "force_ideate", _noop_async)
    monkeypatch.setattr("ap2.components.cron.impl.load_jobs", lambda path: [])

    captured: dict = {}

    async def _capture_run_task(cfg, sdk, mcp_server, task):  # noqa: ARG001
        captured["task"] = task

    monkeypatch.setattr(daemon, "run_task", _capture_run_task)
    return captured


class _NoopSDK:
    class ClaudeAgentOptions:
        def __init__(self, **kw):
            self.kw = kw

    def query(self, *, prompt, options):  # noqa: ARG002
        async def _gen():
            if False:
                yield None

        return _gen()


def test_tick_auto_approves_then_dispatches_in_same_tick(
    cfg: Config, monkeypatch,
):
    """A proposal born `@blocked:review` is auto-approved by the loop pass
    (PRE_DISPATCH) and promoted + dispatched by the auto-promote step — all
    within a single `_tick`, exactly as the pre-TB-383 proposal-time strip
    produced. Pins the load-bearing sequencing (pass runs before
    dispatch)."""
    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")
    monkeypatch.delenv("AP2_AUTO_APPROVE_DRY_RUN", raising=False)
    monkeypatch.delenv("AP2_AUTO_APPROVE_GATE_TAGS", raising=False)

    tb_id = _add_review_proposal(cfg, title="approve and dispatch")
    assert "@blocked:review" in _line(cfg, tb_id)

    captured = _stub_tick_quiet(monkeypatch)
    asyncio.run(daemon._tick(cfg, _NoopSDK(), mcp_server=None))

    # The loop pass stripped review + emitted auto_approved.
    auto = [
        e for e in events.tail(cfg.events_file, 200)
        if e.get("type") == "auto_approved" and e.get("task") == tb_id
    ]
    assert len(auto) == 1, auto
    # The auto-promote step then promoted it and dispatched it this tick.
    promoted = [
        e for e in events.tail(cfg.events_file, 200)
        if e.get("type") == "backlog_auto_promoted" and e.get("task") == tb_id
    ]
    assert len(promoted) == 1, promoted
    assert captured.get("task") is not None
    assert captured["task"].id == tb_id

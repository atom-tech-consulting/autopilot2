"""TB-224: behavioral pinning for the cost + blast-radius guards
layered on TB-223's `AP2_AUTO_APPROVE` gate.

TB-223 ships the auto-approve switch + tag-opt-out + cumulative-
regression freeze (N consecutive `verification_failed` → Frozen) but
explicitly excludes "Token-cost ceilings / per-window budgets"
(TB-223 brief L77) and does NOT distinguish infrastructure failures
(`task_error`) from work-quality failures (`verification_failed`).
TB-224 layers three halt conditions on the same auto-approve gate,
all sharing the ack verb `ap2 ack auto_approve_window_resume`:

  - `AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP` (default unset = no cap):
    integer max combined input+output tokens per auto-approved task.
    Catches the single-runaway pattern (one task burning $50 of
    tokens in an infinite tool-call loop).
  - `AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP` (default unset = no cap):
    integer max cumulative tokens across all auto-approved tasks
    in a rolling 24h window. Catches the drift pattern (50 small
    tasks each within the per-task cap but cumulatively unbounded).
  - `task_error` single-event halt: one occurrence of a `task_error`
    event for an auto-approved task halts auto-promote (no N
    threshold like TB-223 — infrastructure failures aren't
    statistical noise).

Six behavioral pinning cases (briefing's `## Verification` prose):

  (a) unset per-task cap → no skip event, halt path is dormant.
  (b) per-task cap exceeded → `auto_approve_halted reason=per_task_cap`
      fires once, `auto_approve_skipped` fires per preempted promotion,
      and the auto-approved Backlog task remains in Backlog.
  (c) unset window cap → no halt event.
  (d) window cap exceeded → `auto_approve_halted reason=window_cap`
      fires; manual `ap2 approve` (`ideation_approved`) still
      dispatches even while the auto-promote path is halted.
  (e) `task_error` on an auto-approved task halts with a SINGLE
      occurrence (no N threshold) and writes a `## Decisions needed
      from operator` bullet to `.cc-autopilot/ideation_state.md`.
  (f) `ap2 ack auto_approve_window_resume` resets the halt state for
      both window-cap and task-error reasons (one shared ack).

Test shape mirrors `test_tb223_auto_approve.py` (the predecessor of
this layer) and `test_tb211_event_types.py` (real `_tick` end-to-end
with stubbed internals). A future refactor that flips the cap parse
default, drops the auto-id filter on `task_run_usage` events,
softens the single-event `task_error` rule, or breaks the shared-ack
contract trips a focused subset of these tests with a precise
diff-shaped error.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ap2 import daemon, events, tools
from ap2.board import Board
from ap2.config import Config
from ap2.init import init_project


# Mirrors `_GOAL_MD` in `test_tb223_auto_approve.py`: minimal goal.md
# whose `## Current focus` heading + `## Done when` bullets expose the
# anchors the briefing structural validator (`_validate_briefing_structure`
# step 4 / 5) requires. Without these the `do_board_edit` shape below
# would fail at briefing-shape validation rather than reaching the
# auto-approve gate we're trying to pin.
_GOAL_MD = (
    "# Project Goals\n\n"
    "## Mission\n\n"
    "Drive the project toward end-to-end automation.\n\n"
    "## Done when\n\n"
    "- An operator can point ap2 at a fresh project and walk away "
    "without intervention.\n\n"
    "## Current focus: end-to-end automation\n\n"
    "Close the manual-approval bottleneck plus the cost ceilings.\n\n"
    "## Non-goals\n\n"
    "- something out of scope.\n"
)


# A briefing whose `## Goal` body cites the `## Current focus`
# heading verbatim AND carries a TB-164-shaped `Why now:`
# rationale, mirroring `_BRIEFING` in `test_tb223_auto_approve.py`
# so the structural gate passes cleanly on every `add_backlog`.
_BRIEFING = (
    "# A token-cap test briefing\n\n"
    "## Goal\n\n"
    "Layers cost ceilings onto auto-approve so the end-to-end "
    "automation focus (`## Current focus: end-to-end automation`) "
    "can flip safely.\n\n"
    "Why now: closes the unbounded-blast-radius gap — without "
    "this the auto-approve mode trades manual review for "
    "uncapped token spend, and the operator's walk-away promise "
    "breaks under a runaway loop.\n\n"
    "## Scope\n\n- daemon.py\n\n"
    "## Design\n\nDirect edit.\n\n"
    "## Verification\n- `uv run pytest -q` — gates pass\n\n"
    "## Out of scope\n\n- nothing\n"
)


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    """Project root with the standard ap2 init layout + a real
    `goal.md` so the briefing-structural gate has anchors to match.
    Same fixture shape as `test_tb223_auto_approve.py::cfg`."""
    init_project(tmp_path)
    (tmp_path / "goal.md").write_text(_GOAL_MD)
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()
    return cfg


def _unwrap(res: dict) -> dict:
    assert not res.get("isError"), res
    return json.loads(res["content"][0]["text"])


def _stub_tick_quiet(monkeypatch) -> None:
    """Stub every `_tick` internal except the auto-promote step so
    we can exercise the halt path end-to-end without dispatching a
    real task agent. Mirrors `_stub_tick_quiet` in
    `test_tb223_auto_approve.py` (same shape, same external deps)."""
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
    # TB-381: the cron stage is now the `Phase.CRON_DISPATCH` walk into the
    # cron scheduler component; neutralize it by stubbing the component's
    # `load_jobs` (string target avoids importing the impl module here).
    monkeypatch.setattr("ap2.components.cron.impl.load_jobs", lambda path: [])

    async def _noop_run_task(cfg, sdk, mcp_server, task):  # noqa: ARG001
        return None

    monkeypatch.setattr(daemon, "run_task", _noop_run_task)


class _NoopSDK:
    """SDK stub matching `test_tb223_auto_approve._NoopSDK`. The
    auto-promote step doesn't actually call `query`; the stub exists
    so the `_tick` argument signature stays well-formed."""

    def __init__(self) -> None:
        self.called = False

    class ClaudeAgentOptions:
        def __init__(self, **kw):
            self.kw = kw

    def query(self, *, prompt, options):  # noqa: ARG002
        self.called = True

        async def _gen():
            if False:
                yield None

        return _gen()


def _seed_auto_approved_task(cfg: Config, *, title: str, tags=None) -> str:
    """Add an auto-approved Backlog task (the TB-223 `do_board_edit`
    `add_backlog` path with `AP2_AUTO_APPROVE=1`). Returns the TB-N
    assigned by the daemon. Caller is responsible for setting
    `AP2_AUTO_APPROVE=1` in the env before calling."""
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


def _seed_task_run_usage(
    cfg: Config, *, task: str, input_tokens: int, output_tokens: int,
    status: str = "verification_failed",
) -> None:
    """Append a `task_run_usage` event with the given token counts.
    Mirrors the TB-165 shape `_emit_task_run_usage` produces in
    production so the helper's tail-scan reads real-shaped data."""
    events.append(
        cfg.events_file,
        "task_run_usage",
        task=task,
        run_id=f"r-{task}-1",
        status=status,
        duration_s=10.0,
        usage={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
        model_usage={},
        total_cost_usd=0.5,
        num_turns=10,
        model="opus",
    )


# ===========================================================================
# Direct knob-parser unit pins. Surface the env-parse shape so a refactor
# that flips defaults or accepts a non-int sentinel surfaces cleanly.
# ===========================================================================


def test_per_task_cap_unset_defaults_to_zero(cfg: Config, monkeypatch):
    """`AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP` unset / empty / non-int
    / non-positive → `_per_task_token_cap(cfg)` returns 0 (cap
    disabled). Pin against a refactor that bakes in a default cap.

    TB-326 (axis-5): the helper now takes a `cfg` argument and routes
    the env lookup through `Config.get_component_value`'s reverse-
    `FLAT_TO_SECTIONED` fallback. The flat env name still wins via the
    back-compat shim, so this parser pin still exercises the env
    parser shape end-to-end (default-on-empty/garbage/negative,
    positive-int passthrough).
    """
    monkeypatch.delenv("AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP", raising=False)
    assert daemon._per_task_token_cap(cfg) == 0

    monkeypatch.setenv("AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP", "")
    assert daemon._per_task_token_cap(cfg) == 0

    monkeypatch.setenv("AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP", "not-a-number")
    assert daemon._per_task_token_cap(cfg) == 0

    monkeypatch.setenv("AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP", "-5")
    assert daemon._per_task_token_cap(cfg) == 0

    monkeypatch.setenv("AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP", "50000")
    assert daemon._per_task_token_cap(cfg) == 50000


def test_window_cap_unset_defaults_to_zero(tmp_path: Path, monkeypatch):
    """`AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP` follows the same parse
    shape as the per-task cap. Distinct test rather than parameterized
    so a single regression names the failing knob explicitly.

    TB-326 (axis-5): same `cfg`-argument migration as the sibling
    per-task-cap parser test; the flat env name still wins via the
    `Config.get_component_value` back-compat reverse-lookup. Unlike the
    sibling test, this one builds its own cfg AFTER stripping every
    `AP2_*` env var so the cfg snapshot doesn't carry a stale
    `window_token_cap` value from a parent process whose
    `.cc-autopilot/env` happens to export the knob (this project's own
    env exports it at 100_000_000 — the cfg fixture would otherwise
    populate `cfg.components_config["auto_approve"]["window_token_cap"]`
    at load time and the call-time-evaluated `unset → 0` assertion
    would test the wrong precedence layer).
    """
    import os
    for name in list(os.environ):
        if name.startswith("AP2_"):
            monkeypatch.delenv(name, raising=False)
    init_project(tmp_path)
    cfg = Config.load(tmp_path)

    monkeypatch.delenv("AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP", raising=False)
    assert daemon._window_token_cap(cfg) == 0

    monkeypatch.setenv("AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP", "")
    assert daemon._window_token_cap(cfg) == 0

    monkeypatch.setenv("AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP", "garbage")
    assert daemon._window_token_cap(cfg) == 0

    monkeypatch.setenv("AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP", "0")
    assert daemon._window_token_cap(cfg) == 0

    monkeypatch.setenv("AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP", "1000000")
    assert daemon._window_token_cap(cfg) == 1000000


# ===========================================================================
# (a) Unset per-task cap → no halt, no skip event.
# ===========================================================================


def test_unset_per_task_cap_does_not_halt(cfg: Config, monkeypatch):
    """With `AP2_AUTO_APPROVE=1` and an auto-approved task in Backlog,
    BUT `AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP` unset, even an event
    with massive token counts produces no halt. Pins the
    no-cap-default behavior."""
    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")
    monkeypatch.delenv("AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP", raising=False)
    monkeypatch.delenv("AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP", raising=False)
    monkeypatch.setenv("AP2_AUTO_APPROVE_FREEZE_THRESHOLD", "0")

    tb = _seed_auto_approved_task(cfg, title="tb224 no-cap")
    # Massive usage but no cap → daemon must not halt.
    _seed_task_run_usage(
        cfg, task=tb, input_tokens=10_000_000, output_tokens=10_000_000,
    )

    assert daemon._auto_approve_check_violations(cfg) is None

    _stub_tick_quiet(monkeypatch)
    asyncio.run(daemon._tick(cfg, _NoopSDK(), mcp_server=None))

    evts = events.tail(cfg.events_file, 200)
    halted = [e for e in evts if e.get("type") == "auto_approve_halted"]
    skipped = [e for e in evts if e.get("type") == "auto_approve_skipped"]
    assert halted == [], (
        "no halt event must fire when per-task cap is unset; got: "
        f"{halted}"
    )
    assert skipped == [], (
        "no skip event must fire when no cap is in effect; got: "
        f"{skipped}"
    )
    # Task should have been auto-promoted (the auto-approve gate is on
    # and no cap blocked it).
    board = Board.load(cfg.tasks_file)
    loc = board.find(tb)
    assert loc is not None and loc[0] == "Ready", (
        f"unset-cap path must auto-promote the task to Ready; "
        f"got section={loc[0] if loc else 'missing'}"
    )


# ===========================================================================
# (b) Per-task cap exceeded → auto_approve_halted reason=per_task_cap.
# ===========================================================================


def test_per_task_cap_exceeded_halts_with_dedup(cfg: Config, monkeypatch):
    """A `task_run_usage` event whose combined tokens exceed the
    per-task cap trips an `auto_approve_halted reason=per_task_cap`
    event (once) and an `auto_approve_skipped` event (per preempted
    promotion). The Backlog task is NOT moved to Ready.

    A SECOND `_tick` run with the same state must NOT re-emit
    `auto_approve_halted` (it's deduped against the same trigger
    episode) but SHOULD re-emit `auto_approve_skipped` (each preempt
    is its own observability event)."""
    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")
    monkeypatch.setenv("AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP", "5000")
    monkeypatch.delenv("AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP", raising=False)
    monkeypatch.setenv("AP2_AUTO_APPROVE_FREEZE_THRESHOLD", "0")

    # Seed an auto-approved task that already ran and exceeded the
    # cap. In production this is the retry-after-failure path: the
    # task ran once, emitted task_run_usage with huge tokens,
    # bounced back to Backlog. Next `_tick` should refuse to
    # re-dispatch it.
    tb = _seed_auto_approved_task(cfg, title="tb224 per-task runaway")
    _seed_task_run_usage(cfg, task=tb, input_tokens=4000, output_tokens=2000)

    # Direct unit pin: the violation helper detects the over-cap event.
    violation = daemon._auto_approve_check_violations(cfg)
    assert violation is not None
    reason, used, cap, trigger, _detail = violation
    assert reason == "per_task_cap"
    assert used == 6000
    assert cap == 5000
    assert trigger == tb

    _stub_tick_quiet(monkeypatch)
    asyncio.run(daemon._tick(cfg, _NoopSDK(), mcp_server=None))

    evts = events.tail(cfg.events_file, 200)
    halted = [e for e in evts if e.get("type") == "auto_approve_halted"]
    skipped = [e for e in evts if e.get("type") == "auto_approve_skipped"]
    assert len(halted) == 1, (
        f"per_task_cap should emit exactly one auto_approve_halted; got {halted!r}"
    )
    assert halted[0]["reason"] == "per_task_cap"
    assert halted[0]["used"] == 6000
    assert halted[0]["cap"] == 5000
    assert halted[0]["task"] == tb
    assert len(skipped) == 1, skipped
    assert skipped[0]["reason"] == "per_task_cap"
    assert skipped[0]["task"] == tb

    # Task stays in Backlog.
    board = Board.load(cfg.tasks_file)
    loc = board.find(tb)
    assert loc is not None and loc[0] == "Backlog", (
        f"per_task_cap halt must keep the task in Backlog; "
        f"got section={loc[0] if loc else 'missing'}"
    )

    # Second tick: halt event must NOT re-fire (dedupe against the
    # same triggering episode); skip event must re-fire.
    asyncio.run(daemon._tick(cfg, _NoopSDK(), mcp_server=None))
    evts2 = events.tail(cfg.events_file, 400)
    halted2 = [e for e in evts2 if e.get("type") == "auto_approve_halted"]
    skipped2 = [e for e in evts2 if e.get("type") == "auto_approve_skipped"]
    assert len(halted2) == 1, (
        f"auto_approve_halted must dedupe across ticks for one episode; got {halted2!r}"
    )
    assert len(skipped2) == 2, (
        f"auto_approve_skipped must fire once per preempted promotion; got {skipped2!r}"
    )


# ===========================================================================
# (c) Unset window cap → no halt.
# ===========================================================================


def test_unset_window_cap_does_not_halt(cfg: Config, monkeypatch):
    """With `AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP` unset, cumulative
    `task_run_usage` token sums regardless of size produce no halt.
    Pins the window-cap default-disabled behavior."""
    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")
    monkeypatch.delenv("AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP", raising=False)
    monkeypatch.delenv("AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP", raising=False)
    monkeypatch.setenv("AP2_AUTO_APPROVE_FREEZE_THRESHOLD", "0")

    tb_a = _seed_auto_approved_task(cfg, title="tb224 window-A")
    tb_b = _seed_auto_approved_task(cfg, title="tb224 window-B")
    # Cumulative 6M tokens; no window cap → no halt.
    _seed_task_run_usage(
        cfg, task=tb_a, input_tokens=2_000_000, output_tokens=1_000_000,
    )
    _seed_task_run_usage(
        cfg, task=tb_b, input_tokens=2_000_000, output_tokens=1_000_000,
    )

    assert daemon._auto_approve_check_violations(cfg) is None


# ===========================================================================
# (d) Window cap exceeded → auto_approve_halted reason=window_cap;
#     manual ap2 approve still dispatches.
# ===========================================================================


def test_window_cap_exceeded_halts_auto_promote(
    cfg: Config, monkeypatch,
):
    """Cumulative `task_run_usage` token sum across auto-approved
    tasks in the 24h window exceeds the cap → daemon emits
    `auto_approve_halted reason=window_cap window_used=<N> cap=<M>`
    and refuses to auto-promote the auto-approved Backlog task.
    The `auto_approve_skipped` event names the would-have-promoted
    TB-N. Pins the briefing's window-cap halt rule from Scope (2)."""
    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")
    monkeypatch.setenv("AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP", "10000")
    monkeypatch.delenv("AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP", raising=False)
    monkeypatch.setenv("AP2_AUTO_APPROVE_FREEZE_THRESHOLD", "0")

    # Prior auto-approved tasks that already ran and burned the
    # budget. Emit the audit events without adding board rows so the
    # test focuses on the window-cap halt independent of board-order
    # plumbing (which has its own coverage elsewhere — TB-223's
    # `test_tick_halts_auto_promote_when_freeze_active`).
    events.append(cfg.events_file, "auto_approved", task="TB-900", knob="1")
    events.append(cfg.events_file, "auto_approved", task="TB-901", knob="1")
    _seed_task_run_usage(
        cfg, task="TB-900", input_tokens=4000, output_tokens=2000,
    )
    _seed_task_run_usage(
        cfg, task="TB-901", input_tokens=4000, output_tokens=2000,
    )

    # tb_d is the NEW auto-approved task that the daemon will try to
    # promote on this tick.
    tb_d = _seed_auto_approved_task(cfg, title="tb224 should be halted")

    # The violation check sees 12_000 cumulative tokens > 10_000 cap.
    violation = daemon._auto_approve_check_violations(cfg)
    assert violation is not None
    reason, used, cap, _trigger, _detail = violation
    assert reason == "window_cap"
    assert used == 12_000
    assert cap == 10_000

    _stub_tick_quiet(monkeypatch)
    asyncio.run(daemon._tick(cfg, _NoopSDK(), mcp_server=None))

    evts = events.tail(cfg.events_file, 400)
    halted = [
        e for e in evts
        if e.get("type") == "auto_approve_halted"
        and e.get("reason") == "window_cap"
    ]
    assert len(halted) == 1, (
        f"window_cap halt must emit exactly one auto_approve_halted; "
        f"got: {halted!r}"
    )
    assert halted[0]["window_used"] == 12_000
    assert halted[0]["cap"] == 10_000

    # tb_d stays in Backlog.
    board = Board.load(cfg.tasks_file)
    loc_d = board.find(tb_d)
    assert loc_d is not None and loc_d[0] == "Backlog", (
        f"auto-approved tb_d must remain in Backlog under window-cap halt; "
        f"got section={loc_d[0] if loc_d else 'missing'}"
    )

    skipped = [e for e in evts if e.get("type") == "auto_approve_skipped"]
    assert any(
        e.get("task") == tb_d and e.get("reason") == "window_cap"
        for e in skipped
    ), f"auto_approve_skipped must name tb_d as the preempted task; got: {skipped!r}"


def test_window_cap_halt_lets_operator_approved_dispatch(
    cfg: Config, monkeypatch,
):
    """The window-cap halt targets only the auto-approved bucket;
    an operator-approved Backlog task (no auto_approved event)
    continues to auto-promote on the same tick. Pins the briefing's
    "manual ap2 approve still dispatches" rule from Scope (2).

    Test shape mirrors TB-223's
    `test_operator_approved_task_dispatches_even_when_freeze_active`
    — single Backlog task that isn't auto-approved, halt active
    on the auto layer, expect promotion to Ready."""
    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")
    monkeypatch.setenv("AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP", "10000")
    monkeypatch.setenv("AP2_AUTO_APPROVE_FREEZE_THRESHOLD", "0")
    # Prior auto-approved tasks burned the window budget.
    events.append(cfg.events_file, "auto_approved", task="TB-910", knob="1")
    _seed_task_run_usage(
        cfg, task="TB-910", input_tokens=8000, output_tokens=4000,
    )

    # Land a task that is NOT auto-approved (carries a gate-tag so
    # `@blocked:review` stays on the row), then operator-approve it.
    monkeypatch.delenv("AP2_AUTO_APPROVE_GATE_TAGS", raising=False)
    res = tools.do_board_edit(
        cfg,
        {
            "action": "add_backlog",
            "title": "operator-approved while window halted",
            "blocked_on": "review",
            "briefing": _BRIEFING,
            "tags": ["#autopilot", "#breaking-change"],
        },
    )
    tb_op = _unwrap(res)["task_id"]
    tools.do_board_edit(cfg, {"action": "approve", "task_id": tb_op})
    assert daemon._was_auto_approved(cfg, tb_op) is False, (
        "operator-approved task must NOT be in the auto-approved bucket"
    )
    # Window cap is still violated.
    assert daemon._auto_approve_check_violations(cfg) is not None

    _stub_tick_quiet(monkeypatch)
    asyncio.run(daemon._tick(cfg, _NoopSDK(), mcp_server=None))

    board = Board.load(cfg.tasks_file)
    loc = board.find(tb_op)
    assert loc is not None and loc[0] in ("Ready", "Active"), (
        f"operator-approved task must dispatch even while window cap "
        f"halts the auto layer; got section={loc[0]}"
    )


# ===========================================================================
# (e) task_error on an auto-approved task halts with a SINGLE event.
# ===========================================================================


def test_task_error_single_event_halts_auto_promote(cfg: Config, monkeypatch):
    """ONE `task_error` event for an auto-approved task is enough to
    halt auto-promote — no N-consecutive threshold like TB-223. The
    daemon also appends a `## Decisions needed from operator` bullet
    to `.cc-autopilot/ideation_state.md` naming the failing TB-N +
    the error excerpt, so `ap2 status` surfaces it without waiting
    for the next ideation cron."""
    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")
    monkeypatch.setenv("AP2_AUTO_APPROVE_FREEZE_THRESHOLD", "0")
    monkeypatch.delenv("AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP", raising=False)
    monkeypatch.delenv("AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP", raising=False)

    # tb_a is the prior auto-approved task that hit a `task_error`
    # (SDK timeout). tb_b is the new auto-approved task in Backlog
    # that should NOT promote while the halt is active.
    tb_a = _seed_auto_approved_task(cfg, title="tb224 infra-failed")
    events.append(
        cfg.events_file, "task_error",
        task=tb_a,
        error="TimeoutError: SDK subprocess hung for 60s past deadline",
        stderr_tail="(empty)",
    )
    tb_b = _seed_auto_approved_task(cfg, title="tb224 paused by infra")

    # Direct pin: the violation helper sees the task_error event.
    violation = daemon._auto_approve_check_violations(cfg)
    assert violation is not None
    reason, _used, _cap, trigger, detail = violation
    assert reason == "task_error"
    assert trigger == tb_a
    assert "TimeoutError" in detail

    _stub_tick_quiet(monkeypatch)
    asyncio.run(daemon._tick(cfg, _NoopSDK(), mcp_server=None))

    evts = events.tail(cfg.events_file, 400)
    # The task_error path is gated by tb_a's auto_approved + the
    # tb_b auto-promote attempt this tick. Halt event fires once.
    halted = [
        e for e in evts
        if e.get("type") == "auto_approve_halted"
        and e.get("reason") == "task_error"
    ]
    assert len(halted) == 1, (
        f"task_error must halt with a single event (no N threshold); "
        f"got halted events: {halted!r}"
    )
    assert halted[0]["task"] == tb_a
    assert "TimeoutError" in halted[0]["error_excerpt"]

    # Bullet appended to ideation_state.md's `## Decisions needed
    # from operator` section.
    ideation_state = cfg.project_root / ".cc-autopilot" / "ideation_state.md"
    text = ideation_state.read_text()
    assert "## Decisions needed from operator" in text, (
        "task_error halt must materialize the section if absent"
    )
    assert tb_a in text, (
        f"`Decisions needed` bullet must name the failing TB-N {tb_a}; "
        f"got:\n{text}"
    )
    assert "task_error" in text or "TimeoutError" in text, (
        "`Decisions needed` bullet must carry the error excerpt; "
        f"got:\n{text}"
    )

    # tb_b stays in Backlog.
    board = Board.load(cfg.tasks_file)
    loc_b = board.find(tb_b)
    assert loc_b is not None and loc_b[0] == "Backlog", (
        f"auto-approved tb_b must remain in Backlog under task_error halt; "
        f"got section={loc_b[0] if loc_b else 'missing'}"
    )


# ===========================================================================
# (f) ap2 ack auto_approve_window_resume resumes auto-promote.
# ===========================================================================


def test_ack_window_resume_clears_window_cap_halt(cfg: Config, monkeypatch):
    """After a `window_cap` halt, an `operator_ack` carrying the
    `auto_approve_window_resume` token resets the halt state. The
    next `_tick` auto-promotes the previously-paused auto-approved
    Backlog task."""
    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")
    monkeypatch.setenv("AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP", "5000")
    monkeypatch.delenv("AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP", raising=False)
    monkeypatch.setenv("AP2_AUTO_APPROVE_FREEZE_THRESHOLD", "0")

    tb_old = _seed_auto_approved_task(cfg, title="tb224 burned the budget")
    _seed_task_run_usage(
        cfg, task=tb_old, input_tokens=4000, output_tokens=2000,
    )
    tb_new = _seed_auto_approved_task(cfg, title="tb224 to be released")

    # Halt active.
    assert daemon._auto_approve_check_violations(cfg) is not None

    # Operator emits the resume ack. Uses the same drain-side
    # helper as the TB-223 unfreeze ack so the test exercises the
    # real write + emit path.
    tools._apply_operator_ack(
        cfg,
        {"note": "auto_approve_window_resume: budget reset, root cause analyzed"},
    )
    assert daemon._auto_approve_check_violations(cfg) is None, (
        "operator_ack with auto_approve_window_resume token must reset the "
        "window-cap halt state"
    )

    _stub_tick_quiet(monkeypatch)
    asyncio.run(daemon._tick(cfg, _NoopSDK(), mcp_server=None))

    board = Board.load(cfg.tasks_file)
    # tb_old was last added; the next dispatchable could be either,
    # but after ack the auto-promote must succeed on the first
    # available auto-approved task.
    promoted = [
        e for e in events.tail(cfg.events_file, 400)
        if e.get("type") == "backlog_auto_promoted"
    ]
    assert promoted, (
        "after auto_approve_window_resume ack, auto-promote must resume; "
        "got no backlog_auto_promoted events"
    )
    # At least one of tb_old/tb_new advanced to Ready.
    loc_new = board.find(tb_new)
    loc_old = board.find(tb_old)
    advanced = (
        (loc_new is not None and loc_new[0] in ("Ready", "Active"))
        or (loc_old is not None and loc_old[0] in ("Ready", "Active"))
    )
    assert advanced, (
        f"auto-promote must move one of the released tasks to Ready; "
        f"got tb_new={loc_new[0] if loc_new else 'missing'}, "
        f"tb_old={loc_old[0] if loc_old else 'missing'}"
    )


def test_ack_window_resume_clears_task_error_halt(cfg: Config, monkeypatch):
    """The same shared ack `auto_approve_window_resume` resets a
    `task_error` halt — one ack covers both reasons since they share
    the same auto-promote-paused state. Mirrors TB-223's
    `auto_approve_unfreeze` shape but on a distinct token."""
    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")
    monkeypatch.setenv("AP2_AUTO_APPROVE_FREEZE_THRESHOLD", "0")

    tb_failed = _seed_auto_approved_task(cfg, title="tb224 infra-failed (ack)")
    events.append(
        cfg.events_file, "task_error",
        task=tb_failed,
        error="OOMKilled: SDK subprocess exceeded memory limit",
    )
    tb_after = _seed_auto_approved_task(cfg, title="tb224 after ack")

    assert daemon._auto_approve_check_violations(cfg) is not None

    tools._apply_operator_ack(
        cfg,
        {"note": "auto_approve_window_resume: infra fixed, cgroup limits raised"},
    )
    assert daemon._auto_approve_check_violations(cfg) is None, (
        "shared ack must clear the task_error halt too — one ack covers both"
    )

    _stub_tick_quiet(monkeypatch)
    asyncio.run(daemon._tick(cfg, _NoopSDK(), mcp_server=None))

    board = Board.load(cfg.tasks_file)
    # Either tb_failed or tb_after should now be advancing — the
    # halt is cleared.
    promoted = [
        e for e in events.tail(cfg.events_file, 400)
        if e.get("type") == "backlog_auto_promoted"
    ]
    assert promoted, (
        "after auto_approve_window_resume ack on task_error halt, "
        "auto-promote must resume; got no backlog_auto_promoted events"
    )


# ===========================================================================
# Additional pins: precedence + ideation_state.md helper behavior.
# ===========================================================================


def test_task_error_takes_precedence_over_caps(cfg: Config, monkeypatch):
    """When a `task_error` AND a per-task-cap violation BOTH exist
    in the post-ack window, the halt reason is `task_error` (highest
    precedence — infrastructure failures deserve immediate operator
    attention even if a cost cap also tripped). Pins the priority
    order in `_auto_approve_check_violations`."""
    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")
    monkeypatch.setenv("AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP", "1000")
    monkeypatch.setenv("AP2_AUTO_APPROVE_FREEZE_THRESHOLD", "0")

    tb = _seed_auto_approved_task(cfg, title="tb224 both")
    _seed_task_run_usage(cfg, task=tb, input_tokens=5000, output_tokens=5000)
    events.append(
        cfg.events_file, "task_error",
        task=tb,
        error="ValueError: malformed briefing",
    )

    violation = daemon._auto_approve_check_violations(cfg)
    assert violation is not None
    reason, _used, _cap, _trigger, _detail = violation
    assert reason == "task_error", (
        f"task_error must outrank per_task_cap; got reason={reason}"
    )


def test_window_cap_only_counts_auto_approved_tasks(cfg: Config, monkeypatch):
    """`task_run_usage` events for non-auto-approved tasks
    (operator-approved or unmarked) do NOT contribute to the window
    sum. Pins the briefing's filter: "tasks identified as auto-
    approved via TB-223's `auto_approved` audit event"."""
    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")
    monkeypatch.setenv("AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP", "10000")
    monkeypatch.setenv("AP2_AUTO_APPROVE_FREEZE_THRESHOLD", "0")
    monkeypatch.setenv("AP2_AUTO_APPROVE_GATE_TAGS", "#__never__")

    # tb_manual: operator-approved (not in auto bucket). Big tokens
    # but shouldn't count.
    tb_manual = _seed_auto_approved_task(
        cfg, title="tb224 manual-counted", tags=["#autopilot"],
    )
    tools.do_board_edit(cfg, {"action": "approve", "task_id": tb_manual})
    _seed_task_run_usage(
        cfg, task=tb_manual, input_tokens=50_000, output_tokens=50_000,
    )

    # tb_auto: auto-approved. Small tokens.
    tb_auto = _seed_auto_approved_task(cfg, title="tb224 auto-counted")
    _seed_task_run_usage(cfg, task=tb_auto, input_tokens=100, output_tokens=100)

    # 100M tokens on tb_manual must not count; only 200 from tb_auto
    # counts, well under 10000 cap → no violation.
    violation = daemon._auto_approve_check_violations(cfg)
    assert violation is None, (
        f"window-cap must only count auto-approved tasks; got: {violation!r}"
    )


def test_decisions_needed_helper_appends_to_existing_section(
    cfg: Config,
):
    """`_append_decisions_needed_bullet` adds a bullet to the
    existing section without clobbering sibling content. Pins the
    section-preserving write shape."""
    ideation_state = cfg.project_root / ".cc-autopilot" / "ideation_state.md"
    ideation_state.write_text(
        "# Ideation State\n\n"
        "## Mission alignment\n\n"
        "Some mission notes.\n\n"
        "## Decisions needed from operator\n\n"
        "- An existing decision item.\n\n"
        "## Cycle observations\n\n"
        "Some observations.\n"
    )
    daemon._append_decisions_needed_bullet(
        cfg, "A new TB-224 task_error halt entry.",
    )
    text = ideation_state.read_text()
    assert "An existing decision item." in text
    assert "A new TB-224 task_error halt entry." in text
    assert "Some mission notes." in text
    assert "Some observations." in text
    # The new bullet sits inside the Decisions section, before
    # the Cycle observations section.
    decisions_idx = text.index("## Decisions needed from operator")
    cycle_idx = text.index("## Cycle observations")
    new_bullet_idx = text.index("A new TB-224 task_error halt entry.")
    assert decisions_idx < new_bullet_idx < cycle_idx, (
        "new bullet must land inside the decisions section, not after "
        "the next sibling section"
    )


def test_decisions_needed_helper_creates_section_when_absent(
    cfg: Config,
):
    """When `ideation_state.md` has no `## Decisions needed from
    operator` section yet, the helper appends a fresh section at
    end-of-file. Pins the create-if-absent shape."""
    ideation_state = cfg.project_root / ".cc-autopilot" / "ideation_state.md"
    ideation_state.write_text(
        "# Ideation State\n\n"
        "## Mission alignment\n\nMission notes.\n"
    )
    daemon._append_decisions_needed_bullet(
        cfg, "Fresh-section entry.",
    )
    text = ideation_state.read_text()
    assert "## Decisions needed from operator" in text
    assert "Fresh-section entry." in text
    # Existing section preserved.
    assert "## Mission alignment" in text
    assert "Mission notes." in text

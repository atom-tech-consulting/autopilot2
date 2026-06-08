"""TB-272: behavioral pinning for the `validator_judge_noisy`
discriminator on the auto-approve `pause_reason` + dispatch gate.

Axis-1+3 cross-cut safety-floor closure for the load-bearing
dep-coherence validator-judge (TB-235) fail-open hazard goal.md
L82-88 names. TB-243 surfaced the rolling-24h fail/timeout counts on
`ap2 status` text/JSON + web home automation card but did NOT gate
the auto-approve dispatch path on them; an operator with
`AP2_AUTO_APPROVE=1` would see "0 fail, 11 timeout [noisy]" yet the
daemon would keep dispatching against a silently fail-open'ing
upstream judge.

TB-272 promotes the noisy state to a load-bearing pause:
`_pause_reason` returns the new `"validator_judge_noisy"` token (with
priority over the existing `consecutive_freezes` / cost halts), the
daemon dispatch gate refuses to auto-promote `auto_approved` Backlog
tasks, and per-preempt emits `auto_approve_skipped
reason=validator_judge_noisy fail_count_24h=<N>
timeout_count_24h=<M> threshold=<T>`. The opt-out knob
`AP2_AUTO_APPROVE_NOISY_PAUSE_DISABLED` restores the pre-TB-272
cosmetic-only behavior. The pause reuses the existing
`ap2 ack auto_approve_unfreeze` resume verb (no new ack token; same
operator muscle-memory as `consecutive_freezes`).

Six behavioral pinning cases mirror the scope bullets:

  (1) `_pause_reason` returns `"validator_judge_noisy"` when the
      rolling-24h timeout count alone crosses the default-5
      threshold.
  (2) Mixed fail + timeout events whose combined count crosses the
      threshold trip the same token.
  (3) Noisy state takes priority over `consecutive_freezes` when
      both fire (safety-floor failure dominates the post-hoc
      regression halt).
  (4) `AP2_AUTO_APPROVE_NOISY_PAUSE_DISABLED=1` opts out — the
      noisy state contributes None to `pause_reason` so the
      pre-TB-272 cosmetic-only TB-243 surface stands.
  (5) End-to-end `_tick` dispatch: a noisy state preserves the
      `@blocked:review` codespan and emits `auto_approve_skipped
      reason=validator_judge_noisy` with the structured count
      payload.
  (6) `_PAUSE_REASON_ACK_VERB["validator_judge_noisy"]` maps to
      `"auto_approve_unfreeze"` (no new ack token).

Plus the env_reload allowlist pin for the new opt-out knob.

Fixtures mirror `test_tb223_auto_approve.py` /
`test_tb224_token_caps.py` — `init_project` + a goal.md whose
`## Current focus` heading the briefing-structural validator can
match, plus `_stub_tick_quiet` so the `_tick` end-to-end pin
doesn't dispatch a real task agent. No SDK / network / freezegun
dependence.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ap2 import (
    automation_status,
    daemon,
    env_reload,
    events,
    tools,
)
from ap2.components import auto_approve
from ap2.board import Board
from ap2.config import Config
from ap2.init import init_project


# ===========================================================================
# Fixtures + helpers (mirror TB-223 / TB-224 — same shape so a regression
# that breaks the briefing-structural gate trips the existing tests too).
# ===========================================================================


_GOAL_MD = (
    "# Project Goals\n\n"
    "## Mission\n\n"
    "Drive the project toward end-to-end automation.\n\n"
    "## Done when\n\n"
    "- An operator can point ap2 at a fresh project and walk away "
    "without intervention.\n\n"
    "## Current focus: end-to-end automation\n\n"
    "Close the manual-approval bottleneck plus the safety floors.\n\n"
    "## Non-goals\n\n"
    "- something out of scope.\n"
)


_BRIEFING = (
    "# A TB-272 noisy-pause test briefing\n\n"
    "## Goal\n\n"
    "Closes the validator-judge fail-open hazard against the "
    "end-to-end automation focus "
    "(`## Current focus: end-to-end automation`).\n\n"
    "Why now: without the noisy-pause the auto-approve dispatch "
    "fires against a silently-degraded upstream judge, breaking "
    "goal.md L82-85's safety claim.\n\n"
    "## Scope\n\n- automation_status.py\n\n"
    "## Design\n\nDirect edit.\n\n"
    "## Verification\n- `uv run pytest -q` — gates pass\n\n"
    "## Out of scope\n\n- nothing\n"
)


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    init_project(tmp_path)
    (tmp_path / "goal.md").write_text(_GOAL_MD)
    c = Config.load(tmp_path)
    c.ensure_dirs()
    return c


def _unwrap(res: dict) -> dict:
    assert not res.get("isError"), res
    return json.loads(res["content"][0]["text"])


def _seed_validator_judge_events(
    cfg: Config, *, fail: int = 0, timeout: int = 0,
) -> None:
    """Append N `validator_judge_fail` + M `validator_judge_timeout`
    events to the project's events.jsonl, each stamped with the
    current `now()` so the rolling-24h window picks them up. Mirrors
    the test seeds in `test_tb243_validator_judge_surface.py` so a
    refactor that changes the event-emission shape trips both."""
    for _ in range(fail):
        events.append(
            cfg.events_file, "validator_judge_fail",
            error="non-dict judge response",
        )
    for _ in range(timeout):
        events.append(
            cfg.events_file, "validator_judge_timeout",
            timeout_s=60, error="TimeoutError()",
        )


def _seed_auto_approved_task(cfg: Config, *, title: str) -> str:
    res = tools.do_board_edit(
        cfg,
        {
            "action": "add_backlog",
            "title": title,
            "blocked_on": "review",
            "briefing": _BRIEFING,
            "tags": ["#autopilot"],
        },
    )
    return _unwrap(res)["task_id"]


def _stub_tick_quiet(monkeypatch) -> None:
    """Stub every `_tick` internal except the auto-promote step so the
    noisy-pause path is reachable end-to-end without dispatching a
    real task agent. Mirrors `_stub_tick_quiet` in
    `test_tb223_auto_approve.py` / `test_tb224_token_caps.py`."""
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
    """SDK stub matching the shape used by TB-223 / TB-224 end-to-end
    pins. The auto-promote step never calls `query`; this exists so
    the `_tick` argument signature stays well-formed."""

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


# ===========================================================================
# (1) `_pause_reason` returns `validator_judge_noisy` above threshold
#     (timeout-only path; default threshold 5).
# ===========================================================================


def test_pause_reason_returns_validator_judge_noisy_above_threshold(
    cfg: Config, monkeypatch,
):
    """5 `validator_judge_timeout` events in the trailing 24h →
    `collect_auto_approve_state(cfg)["pause_reason"] ==
    "validator_judge_noisy"`. Pins the count-derived discriminator at
    the default threshold (TB-243 calibration: 5).
    """
    monkeypatch.delenv("AP2_AUTO_APPROVE_NOISY_PAUSE_DISABLED", raising=False)
    monkeypatch.delenv("AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD", raising=False)

    _seed_validator_judge_events(cfg, timeout=5)

    state = automation_status.collect_auto_approve_state(cfg)
    assert state["pause_reason"] == "validator_judge_noisy", state
    assert state["auto_approve_paused"] is True


# ===========================================================================
# (2) Mixed fail + timeout — combined count drives the threshold.
# ===========================================================================


def test_pause_reason_returns_validator_judge_noisy_with_mixed_fail_and_timeout(
    cfg: Config, monkeypatch,
):
    """3 fails + 3 timeouts in the trailing 24h (combined 6 >=
    default 5) → `pause_reason == "validator_judge_noisy"`. Pins the
    combined-count semantics so a refactor that filters on only one
    of the two event types regresses cleanly.
    """
    monkeypatch.delenv("AP2_AUTO_APPROVE_NOISY_PAUSE_DISABLED", raising=False)
    monkeypatch.delenv("AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD", raising=False)

    _seed_validator_judge_events(cfg, fail=3, timeout=3)

    state = automation_status.collect_auto_approve_state(cfg)
    assert state["pause_reason"] == "validator_judge_noisy", state
    assert state["validator_judge_fail_count_24h"] == 3
    assert state["validator_judge_timeout_count_24h"] == 3


# ===========================================================================
# (3) Priority — noisy state dominates consecutive_freezes.
# ===========================================================================


def test_pause_reason_priority_over_consecutive_freezes(
    cfg: Config, monkeypatch,
):
    """When BOTH the validator-judge noisy state AND the cumulative-
    regression circuit-breaker fire, `pause_reason` is
    `"validator_judge_noisy"` (safety-floor failure dominates the
    post-hoc regression halt — the briefing's signal-clarity choice).

    Seeds the validator-judge noisy state PLUS a TB-223 freeze
    (3 consecutive `task_complete` failures ending in
    `retry_exhausted`, which triggers an `auto_approve_paused`
    event). Asserts the noisy token wins.
    """
    monkeypatch.delenv("AP2_AUTO_APPROVE_NOISY_PAUSE_DISABLED", raising=False)
    monkeypatch.delenv("AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD", raising=False)

    # Validator-judge noisy state.
    _seed_validator_judge_events(cfg, fail=5)

    # Seed an `auto_approve_paused` event (the surface signal the
    # `_pause_reason` consecutive_freezes branch picks up). Direct
    # event seed mirrors the shape `_tick`'s TB-223 branch emits in
    # production — no need to drive the full freeze chain through
    # `_tick` to pin the priority ordering at the collector layer.
    events.append(
        cfg.events_file, "auto_approve_paused",
        task="TB-901", threshold=3, reason="seeded for priority test",
    )

    state = automation_status.collect_auto_approve_state(cfg)
    assert state["pause_reason"] == "validator_judge_noisy", state


# ===========================================================================
# (4) Opt-out knob `AP2_AUTO_APPROVE_NOISY_PAUSE_DISABLED=1`.
# ===========================================================================


def test_noisy_pause_disabled_knob_opts_out(cfg: Config, monkeypatch):
    """`AP2_AUTO_APPROVE_NOISY_PAUSE_DISABLED=1` → the noisy state
    no longer drives `pause_reason`. Operator's explicit escape
    hatch for the pre-TB-272 cosmetic-only TB-243 behavior.

    Seeds 10 fails (well above the default 5 threshold); without the
    opt-out the helper returns `"validator_judge_noisy"`; with it,
    `pause_reason` is None (no other halt-class signal is active) or
    one of the cost/freeze tokens, but NOT `"validator_judge_noisy"`.
    """
    monkeypatch.delenv("AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD", raising=False)
    _seed_validator_judge_events(cfg, fail=10)

    # Sanity: without the opt-out the noisy token fires.
    monkeypatch.delenv("AP2_AUTO_APPROVE_NOISY_PAUSE_DISABLED", raising=False)
    state = automation_status.collect_auto_approve_state(cfg)
    assert state["pause_reason"] == "validator_judge_noisy", state

    # With the opt-out, the noisy state contributes None.
    monkeypatch.setenv("AP2_AUTO_APPROVE_NOISY_PAUSE_DISABLED", "1")
    state = automation_status.collect_auto_approve_state(cfg)
    assert state["pause_reason"] != "validator_judge_noisy", state


# ===========================================================================
# (5) End-to-end `_tick` — noisy state preserves `@blocked:review` AND
#     emits `auto_approve_skipped reason=validator_judge_noisy` with the
#     structured count payload.
# ===========================================================================


def test_auto_approve_skipped_when_validator_judge_noisy(
    cfg: Config, monkeypatch,
):
    """End-to-end pin: with `AP2_AUTO_APPROVE=1`, an auto-approved
    Backlog task waiting, AND 5+ validator-judge fail-open events in
    the rolling 24h, `_tick`:
      (a) does NOT promote the task to Ready (codespan
          `@blocked:review` was stripped at proposal time per
          TB-223, but the BOARD ROW must remain in Backlog),
      (b) emits `auto_approve_skipped reason=validator_judge_noisy
          fail_count_24h=<N> timeout_count_24h=<M> threshold=<T>`
          for the preempted promotion attempt.

    Mirrors TB-224's per-task-cap end-to-end pin shape.
    """
    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")
    monkeypatch.delenv("AP2_AUTO_APPROVE_NOISY_PAUSE_DISABLED", raising=False)
    monkeypatch.delenv("AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD", raising=False)
    # Suppress the other axis-1/2/3 pauses so the test focuses on
    # the TB-272 branch: zero the freeze threshold + leave cost caps
    # unset so neither branch can also fire.
    monkeypatch.setenv("AP2_AUTO_APPROVE_FREEZE_THRESHOLD", "0")
    monkeypatch.delenv("AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP", raising=False)
    monkeypatch.delenv("AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP", raising=False)

    tb = _seed_auto_approved_task(cfg, title="tb272 should be paused")
    _seed_validator_judge_events(cfg, fail=3, timeout=2)

    # Direct unit pin on the gate helper before the end-to-end run.
    noisy = auto_approve._validator_judge_noisy_paused(cfg)
    assert noisy is not None
    fail_count, timeout_count, threshold = noisy
    assert fail_count == 3
    assert timeout_count == 2
    assert threshold == 5

    _stub_tick_quiet(monkeypatch)
    asyncio.run(daemon._tick(cfg, _NoopSDK(), mcp_server=None))

    evts = events.tail(cfg.events_file, 400)
    skipped = [
        e for e in evts
        if e.get("type") == "auto_approve_skipped"
        and e.get("reason") == "validator_judge_noisy"
    ]
    assert len(skipped) == 1, (
        f"noisy state must emit exactly one auto_approve_skipped per "
        f"preempted promotion; got {skipped!r}"
    )
    payload = skipped[0]
    assert payload["task"] == tb
    assert payload["fail_count_24h"] == 3
    assert payload["timeout_count_24h"] == 2
    assert payload["threshold"] == 5

    # Task stays in Backlog (the row is auto-approved — codespan
    # already stripped at proposal time — but the dispatch gate
    # refused to promote it to Ready).
    board = Board.load(cfg.tasks_file)
    loc = board.find(tb)
    assert loc is not None and loc[0] == "Backlog", (
        f"noisy-pause must keep the task in Backlog; "
        f"got section={loc[0] if loc else 'missing'}"
    )

    # And `backlog_auto_promoted` did NOT fire for it.
    promoted = [
        e for e in evts
        if e.get("type") == "backlog_auto_promoted" and e.get("task") == tb
    ]
    assert promoted == [], (
        "the auto-approved task must not be promoted while noisy pause active"
    )


# ===========================================================================
# (6) Ack-verb mapping pin.
# ===========================================================================


def test_ack_verb_mapping_includes_validator_judge_noisy():
    """`_PAUSE_REASON_ACK_VERB["validator_judge_noisy"] ==
    "auto_approve_unfreeze"` — reuses the existing
    `consecutive_freezes` verb (no new ack token, no new CLI verb).
    """
    assert (
        automation_status._PAUSE_REASON_ACK_VERB["validator_judge_noisy"]
        == "auto_approve_unfreeze"
    )


# ===========================================================================
# (7) Hot-reload allowlist pin for the new opt-out knob.
# ===========================================================================


def test_hot_reloadable_knob_set_includes_noisy_pause_disabled():
    """`AP2_AUTO_APPROVE_NOISY_PAUSE_DISABLED` must be in
    `env_reload.HOT_RELOADABLE_KNOBS` so the operator can flip it
    without a daemon restart (TB-271 path). Catches a refactor that
    adds the knob to the source but forgets the reload allowlist —
    silent restart-required regression."""
    assert (
        "AP2_AUTO_APPROVE_NOISY_PAUSE_DISABLED"
        in env_reload.HOT_RELOADABLE_KNOBS
    )

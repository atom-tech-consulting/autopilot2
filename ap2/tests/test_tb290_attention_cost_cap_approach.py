"""TB-290: regression-pin for the proactive `cost_cap_approach`
attention detector (TB-282 follow-up closing the pre-trip path of the
"cost anomalies" leg of goal.md Current focus #3 Progress signal #3).

Pre-TB-290 a project approaching its
`AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP` budget had ZERO pre-trip surface:
the post-trip `auto_approve_paused:window_token_cap_exceeded` bullet
fires only AFTER the cap trips, when dispatch is already halted and
the operator must `ap2 ack auto_approve_window_resume` to resume.
This detector is the pre-trip companion — fires at a configurable
percentage threshold below the cap (default 75%) so the walk-away
operator gets a budget-spending nudge hours BEFORE the halt, on the
same Attention surface as the sibling detectors.

This module pins the briefing's seven named arcs (briefing scope item
2):

  (1) No fire when `AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP` is unset / 0
      (cap disabled → no approach state — operator opt-in design).
  (2) No fire when sum < `pct * cap` (below the approach floor).
  (3) ONE condition when `pct * cap <= sum < cap` (approach window).
  (4) No fire when `sum >= cap` (the post-trip `auto_approve_paused`
      detector owns this — explicit hand-off to avoid double-bullet
      noise).
  (5) Per-(type, key) debounce respected within
      `AP2_ATTENTION_DEBOUNCE_S`.
  (6) `AP2_AUTO_APPROVE_COST_APPROACH_PCT=50` env override fires at
      50% sum (pin the hot-reloadable knob path end-to-end).
  (7) Recent `operator_ack` with `auto_approve_window_resume` token
      resets the count (events before the ack don't count — mirrors
      the same semantics the post-trip surface uses).

Plus source-anchor pins mirroring the briefing's Verification greps
and a structural pin that the approach-check sum matches the
trip-check sum (the load-bearing no-drift property the briefing's
Design clause names).
"""
from __future__ import annotations

import datetime as _dt
import json as _json
from pathlib import Path

import pytest

from ap2 import events
from ap2.components import attention
from ap2.components.attention import (
    AttentionCondition,
    _cost_approach_pct,
    _detect_cost_cap_approach,
    detect_attention_conditions,
    find_last_attention_fire,
    should_suppress,
)
from ap2.components.auto_approve import _auto_approve_check_violations
from ap2.config import Config
from ap2.init import init_project


_DEFAULT_PCT = 75  # matches `DEFAULT_AUTO_APPROVE_COST_APPROACH_PCT`


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch) -> Config:
    """Clean project scaffold with the sibling attention-detector env
    knobs unset so the defaults are the contract under test.

    Mirrors TB-289's fixture shape — explicit unset for the
    auto-approve knobs the detector consults so each test can set
    them as needed.
    """
    monkeypatch.delenv("AP2_COMPONENTS_AUTO_APPROVE_WINDOW_TOKEN_CAP", raising=False)
    monkeypatch.delenv("AP2_COMPONENTS_AUTO_APPROVE_COST_APPROACH_PCT", raising=False)
    monkeypatch.delenv("AP2_COMPONENTS_AUTO_APPROVE_PER_TASK_TOKEN_CAP", raising=False)
    monkeypatch.delenv("AP2_COMPONENTS_AUTO_APPROVE_ENABLED", raising=False)
    monkeypatch.delenv("AP2_COMPONENTS_AUTO_APPROVE_FREEZE_THRESHOLD", raising=False)
    monkeypatch.delenv("AP2_COMPONENTS_VALIDATOR_JUDGE_NOISY_THRESHOLD", raising=False)
    monkeypatch.delenv("AP2_COMPONENTS_AUTO_APPROVE_NOISY_PAUSE_DISABLED", raising=False)
    monkeypatch.delenv("AP2_COMPONENTS_ATTENTION_TASK_STUCK_THRESHOLD_S", raising=False)
    monkeypatch.delenv("AP2_COMPONENTS_ATTENTION_TASK_FROZEN_RECENCY_S", raising=False)
    monkeypatch.delenv("AP2_COMPONENTS_ATTENTION_DEBOUNCE_S", raising=False)
    init_project(tmp_path)
    c = Config.load(tmp_path)
    c.ensure_dirs()
    return c


def _ts_seconds_ago(now: _dt.datetime, *, seconds_ago: float) -> str:
    """Format an ISO-8601 timestamp `seconds_ago` before `now`."""
    when = now - _dt.timedelta(seconds=seconds_ago)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _rewrite_last_event_ts(cfg: Config, ts: str) -> None:
    """Replace the `ts` field on the most recent events.jsonl line.

    Mirrors TB-288 / TB-289's tests — `events.append` always stamps
    `now()`; tests that need an event "at a specific time" rewrite
    the line afterward.
    """
    lines = cfg.events_file.read_text().splitlines()
    if not lines:
        return
    last = _json.loads(lines[-1])
    last["ts"] = ts
    lines[-1] = _json.dumps(last)
    cfg.events_file.write_text("\n".join(lines) + "\n")


def _seed_auto_approved_task(cfg: Config, task_id: str) -> None:
    """Emit an `auto_approved` event for `task_id` so it lands in
    `_auto_approved_task_ids`. Mirrors the daemon's emission shape at
    `do_board_edit`'s `add_backlog` branch.
    """
    events.append(
        cfg.events_file, "auto_approved",
        task=task_id, knob="1",
    )


def _seed_task_run_usage(
    cfg: Config,
    *,
    task_id: str,
    input_tokens: int,
    output_tokens: int,
    seconds_ago: float,
    now: _dt.datetime,
) -> None:
    """Emit a `task_run_usage` event for `task_id` at `seconds_ago`
    before `now`. The detector sums `input_tokens + output_tokens`
    via `_event_combined_tokens` — same shape the TB-224 trip-check
    walker uses.
    """
    events.append(
        cfg.events_file, "task_run_usage",
        task=task_id,
        run_id=f"{task_id}-run",
        status="complete",
        duration_s=10.0,
        usage={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        },
    )
    ts = _ts_seconds_ago(now, seconds_ago=seconds_ago)
    _rewrite_last_event_ts(cfg, ts)


def _seed_approach_sum(
    cfg: Config,
    *,
    total_tokens: int,
    now: _dt.datetime,
    chunks: int = 3,
) -> None:
    """Seed `chunks` auto-approved tasks whose `task_run_usage` events
    sum to `total_tokens`. Spreads the seed across several tasks so
    the auto-approved-task-id filter has multiple ids to walk (a
    one-task seed would let a refactor that scoped the walk to "first
    auto-approved task only" pass silently).
    """
    per_chunk = total_tokens // chunks
    remainder = total_tokens - per_chunk * chunks
    for i in range(chunks):
        tid = f"TB-{800 + i}"
        _seed_auto_approved_task(cfg, tid)
        tokens = per_chunk + (remainder if i == chunks - 1 else 0)
        _seed_task_run_usage(
            cfg,
            task_id=tid,
            input_tokens=tokens // 2,
            output_tokens=tokens - (tokens // 2),
            seconds_ago=3600 + i * 60,
            now=now,
        )


# ===========================================================================
# Arc 1: no fire when the cap is unset / 0.
# ===========================================================================


def test_detector_misses_when_cap_unset(cfg: Config):
    """`AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP` unset → cap disabled →
    detector returns `[]` regardless of auto-approved token spend.
    Pin the operator-opt-in design: an operator who hasn't budgeted
    their project must NOT see a phantom approach bullet.
    """
    now = _dt.datetime(2026, 5, 26, 12, 0, 0, tzinfo=_dt.timezone.utc)
    # Big spend with no cap set — should be silent.
    _seed_approach_sum(cfg, total_tokens=10_000_000, now=now)

    conditions = detect_attention_conditions(cfg, now=now)
    approach_conds = [c for c in conditions if c.type == "cost_cap_approach"]
    assert approach_conds == [], conditions


def test_detector_misses_when_cap_is_zero(cfg: Config, monkeypatch):
    """`AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP=0` → cap disabled (TB-224
    parser semantics) → detector returns `[]`. Pin the
    cap-disabled-with-explicit-zero shape since operators set `0` to
    disable rather than unsetting.
    """
    monkeypatch.setenv("AP2_COMPONENTS_AUTO_APPROVE_WINDOW_TOKEN_CAP", "0")
    now = _dt.datetime(2026, 5, 26, 12, 0, 0, tzinfo=_dt.timezone.utc)
    _seed_approach_sum(cfg, total_tokens=10_000_000, now=now)

    conditions = detect_attention_conditions(cfg, now=now)
    approach_conds = [c for c in conditions if c.type == "cost_cap_approach"]
    assert approach_conds == [], conditions


# ===========================================================================
# Arc 2: no fire when sum < pct * cap.
# ===========================================================================


def test_detector_misses_below_threshold(cfg: Config, monkeypatch):
    """`cap=1000, pct=75 → threshold=750`. Sum=600 (60% of cap) →
    no fire. Pin the strict-inequality seam below the threshold so
    every transient spend doesn't surface the Attention bullet.
    """
    monkeypatch.setenv("AP2_COMPONENTS_AUTO_APPROVE_WINDOW_TOKEN_CAP", "1000")
    now = _dt.datetime(2026, 5, 26, 12, 0, 0, tzinfo=_dt.timezone.utc)
    _seed_approach_sum(cfg, total_tokens=600, now=now)

    conditions = detect_attention_conditions(cfg, now=now)
    approach_conds = [c for c in conditions if c.type == "cost_cap_approach"]
    assert approach_conds == [], conditions


# ===========================================================================
# Arc 3: ONE condition when sum >= pct*cap AND sum < cap.
# ===========================================================================


def test_detector_fires_inside_approach_window(cfg: Config, monkeypatch):
    """`cap=1000, pct=75 → threshold=750`. Sum=800 (80% of cap, in
    the [750, 1000) approach window) → exactly ONE condition keyed
    `cost_cap_approach:window` (singleton, NOT per-task). Pin the
    happy-path approach fire including summary contents + extras
    blob the briefing's Design clause names.
    """
    monkeypatch.setenv("AP2_COMPONENTS_AUTO_APPROVE_WINDOW_TOKEN_CAP", "1000")
    now = _dt.datetime(2026, 5, 26, 12, 0, 0, tzinfo=_dt.timezone.utc)
    _seed_approach_sum(cfg, total_tokens=800, now=now)

    conditions = detect_attention_conditions(cfg, now=now)
    approach_conds = [c for c in conditions if c.type == "cost_cap_approach"]
    assert len(approach_conds) == 1, conditions
    cond = approach_conds[0]
    assert cond.type == "cost_cap_approach"
    assert cond.key == "cost_cap_approach:window"  # singleton key
    # Summary names the bullet topic + the resume-via verb.
    assert "auto-approve cost cap approach" in cond.summary
    assert "800 tokens used in last 24h" in cond.summary
    assert "80% of window cap 1000" in cond.summary
    assert f"threshold {_DEFAULT_PCT}%" in cond.summary
    assert "ap2 ack auto_approve_window_resume" in cond.summary
    # Extras blob is the briefing's load-bearing diagnostic contract.
    assert cond.extras["total_tokens_24h"] == 800
    assert cond.extras["window_cap"] == 1000
    assert cond.extras["approach_pct"] == _DEFAULT_PCT
    assert abs(cond.extras["pct_used"] - 80.0) < 0.01
    assert cond.extras["window_s"] == 86400


def test_detector_boundary_fires_at_exact_threshold(
    cfg: Config, monkeypatch,
):
    """`cap=1000, pct=75 → threshold=750`. Sum=750 (exactly at the
    floor) → ONE condition. Inclusive lower boundary (`>=`, not
    `>`) is the load-bearing arithmetic — pin so a refactor to
    strict-greater-than doesn't push the bullet off the wrong side
    of the boundary.
    """
    monkeypatch.setenv("AP2_COMPONENTS_AUTO_APPROVE_WINDOW_TOKEN_CAP", "1000")
    now = _dt.datetime(2026, 5, 26, 12, 0, 0, tzinfo=_dt.timezone.utc)
    _seed_approach_sum(cfg, total_tokens=750, now=now)

    conditions = detect_attention_conditions(cfg, now=now)
    approach_conds = [c for c in conditions if c.type == "cost_cap_approach"]
    assert len(approach_conds) == 1, conditions


# ===========================================================================
# Arc 4: no fire when sum >= cap (post-trip surface owns this).
# ===========================================================================


def test_detector_misses_at_or_above_cap(cfg: Config, monkeypatch):
    """`cap=1000`. Sum=1000 (at the cap) and sum=1500 (above) → no
    approach fire. The post-trip `auto_approve_paused` surface owns
    the at/above-cap state; a second "approach" bullet alongside
    the post-trip pause bullet would be double-noise. Pin the
    explicit hand-off so a refactor that loosens the upper bound
    doesn't regress the briefing's "two simultaneous bullets would
    be noise" design clause.
    """
    monkeypatch.setenv("AP2_COMPONENTS_AUTO_APPROVE_WINDOW_TOKEN_CAP", "1000")
    now = _dt.datetime(2026, 5, 26, 12, 0, 0, tzinfo=_dt.timezone.utc)
    _seed_approach_sum(cfg, total_tokens=1000, now=now)

    conditions = detect_attention_conditions(cfg, now=now)
    approach_conds = [c for c in conditions if c.type == "cost_cap_approach"]
    assert approach_conds == [], "sum == cap should hand off to post-trip"

    # Reset and try above-cap.
    cfg.events_file.unlink()
    _seed_approach_sum(cfg, total_tokens=1500, now=now)
    conditions = detect_attention_conditions(cfg, now=now)
    approach_conds = [c for c in conditions if c.type == "cost_cap_approach"]
    assert approach_conds == [], "sum > cap should hand off to post-trip"


# ===========================================================================
# Arc 5: per-(type, key) debounce respected across consecutive ticks.
# ===========================================================================


def test_detector_debounce_suppresses_within_window(
    cfg: Config, monkeypatch,
):
    """A prior `attention_raised attention_type=cost_cap_approach
    key=cost_cap_approach:window` event 1h ago (well inside the 6h
    default debounce) → `should_suppress` returns True for the
    still-approaching condition. Pin the per-(type, key) debounce
    contract so a sustained approach window doesn't re-fire every
    tick.
    """
    monkeypatch.setenv("AP2_COMPONENTS_AUTO_APPROVE_WINDOW_TOKEN_CAP", "1000")
    now = _dt.datetime(2026, 5, 26, 12, 0, 0, tzinfo=_dt.timezone.utc)
    _seed_approach_sum(cfg, total_tokens=800, now=now)
    # Emit a prior fire 1h ago — well inside the 6h default debounce.
    events.append(
        cfg.events_file, "attention_raised",
        attention_type="cost_cap_approach",
        key="cost_cap_approach:window",
        summary="prior approach fire",
        total_tokens_24h=800, window_cap=1000,
        approach_pct=75, pct_used=80.0, window_s=86400,
    )
    prior_ts = _ts_seconds_ago(now, seconds_ago=3600)
    _rewrite_last_event_ts(cfg, prior_ts)

    conditions = detect_attention_conditions(cfg, now=now)
    approach_conds = [c for c in conditions if c.type == "cost_cap_approach"]
    # Detector still produces the candidate (state IS approaching)...
    assert len(approach_conds) == 1
    # ...but the daemon's debounce check suppresses re-emission.
    tail = events.tail(cfg.events_file, 200)
    assert should_suppress(approach_conds[0], tail=tail, now=now, cfg=cfg) is True


def test_detector_debounce_releases_after_window(
    cfg: Config, monkeypatch,
):
    """A prior fire 7h ago (past the 6h default debounce) →
    `should_suppress` returns False, so a still-approaching state
    surfaces again on this tick. Pin the cadence — operators get
    re-notified once per debounce window for sustained issues
    (mirrors TB-287 / TB-288 / TB-289's debounce-release contract).
    """
    monkeypatch.setenv("AP2_COMPONENTS_AUTO_APPROVE_WINDOW_TOKEN_CAP", "1000")
    now = _dt.datetime(2026, 5, 26, 12, 0, 0, tzinfo=_dt.timezone.utc)
    _seed_approach_sum(cfg, total_tokens=800, now=now)
    events.append(
        cfg.events_file, "attention_raised",
        attention_type="cost_cap_approach",
        key="cost_cap_approach:window",
        summary="prior approach fire",
        total_tokens_24h=800, window_cap=1000,
        approach_pct=75, pct_used=80.0, window_s=86400,
    )
    prior_ts = _ts_seconds_ago(now, seconds_ago=7 * 3600)
    _rewrite_last_event_ts(cfg, prior_ts)

    conditions = detect_attention_conditions(cfg, now=now)
    approach_conds = [c for c in conditions if c.type == "cost_cap_approach"]
    assert len(approach_conds) == 1
    tail = events.tail(cfg.events_file, 200)
    assert should_suppress(approach_conds[0], tail=tail, now=now, cfg=cfg) is False


# ===========================================================================
# Arc 6: env-knob override (AP2_AUTO_APPROVE_COST_APPROACH_PCT).
# ===========================================================================


def test_pct_env_override_fires_at_50_percent(
    cfg: Config, monkeypatch,
):
    """`AP2_AUTO_APPROVE_COST_APPROACH_PCT=50` → detector fires at
    50% sum. Pin the hot-reloadable knob path end-to-end so an
    operator tightening the threshold via `.cc-autopilot/env`
    takes effect on the next detector tick without code changes.
    """
    monkeypatch.setenv("AP2_COMPONENTS_AUTO_APPROVE_WINDOW_TOKEN_CAP", "1000")
    monkeypatch.setenv("AP2_COMPONENTS_AUTO_APPROVE_COST_APPROACH_PCT", "50")
    assert _cost_approach_pct(cfg) == 50

    now = _dt.datetime(2026, 5, 26, 12, 0, 0, tzinfo=_dt.timezone.utc)
    _seed_approach_sum(cfg, total_tokens=500, now=now)

    conditions = detect_attention_conditions(cfg, now=now)
    approach_conds = [c for c in conditions if c.type == "cost_cap_approach"]
    assert len(approach_conds) == 1, conditions
    cond = approach_conds[0]
    assert cond.extras["approach_pct"] == 50
    assert cond.extras["total_tokens_24h"] == 500
    assert "threshold 50%" in cond.summary


def test_pct_env_override_at_50_pct_misses_at_499(
    cfg: Config, monkeypatch,
):
    """`AP2_AUTO_APPROVE_COST_APPROACH_PCT=50, cap=1000 → floor=500`.
    Sum=499 → no fire (boundary exclusion below the floor). Pin the
    integer-arithmetic threshold-check shape (`total*100 >=
    pct*cap`) so a refactor that drops the multiplication form
    can't tip a sum just below the floor over the boundary via
    floating-point rounding.
    """
    monkeypatch.setenv("AP2_COMPONENTS_AUTO_APPROVE_WINDOW_TOKEN_CAP", "1000")
    monkeypatch.setenv("AP2_COMPONENTS_AUTO_APPROVE_COST_APPROACH_PCT", "50")
    now = _dt.datetime(2026, 5, 26, 12, 0, 0, tzinfo=_dt.timezone.utc)
    _seed_approach_sum(cfg, total_tokens=499, now=now)

    conditions = detect_attention_conditions(cfg, now=now)
    approach_conds = [c for c in conditions if c.type == "cost_cap_approach"]
    assert approach_conds == [], conditions


# ===========================================================================
# Arc 7: operator_ack with auto_approve_window_resume resets the count.
# ===========================================================================


def test_detector_respects_operator_ack_reset(
    cfg: Config, monkeypatch,
):
    """Events BEFORE the most recent `operator_ack` whose note carries
    the `auto_approve_window_resume` token don't count toward the
    approach sum — mirrors the same reset semantics
    `_auto_approve_check_violations` uses for its window-cap branch
    (load-bearing for the no-drift property the briefing's Design
    clause pins).

    Seed a big spend, then emit the resume ack, then no new spend →
    detector returns `[]` because the post-ack window is empty.
    """
    monkeypatch.setenv("AP2_COMPONENTS_AUTO_APPROVE_WINDOW_TOKEN_CAP", "1000")
    now = _dt.datetime(2026, 5, 26, 12, 0, 0, tzinfo=_dt.timezone.utc)
    # Seed a sum that WOULD trip the approach detector...
    _seed_approach_sum(cfg, total_tokens=900, now=now)
    pre_ack = detect_attention_conditions(cfg, now=now)
    pre_ack_approach = [c for c in pre_ack if c.type == "cost_cap_approach"]
    assert len(pre_ack_approach) == 1, "seed should trip approach pre-ack"

    # ...now emit the operator_ack resume signal.
    events.append(
        cfg.events_file, "operator_ack",
        verb="auto_approve_window_resume",
        note="operator acked auto_approve_window_resume",
    )
    ack_ts = _ts_seconds_ago(now, seconds_ago=30)
    _rewrite_last_event_ts(cfg, ack_ts)

    # Post-ack: no new task_run_usage events → approach sum is 0.
    conditions = detect_attention_conditions(cfg, now=now)
    approach_conds = [c for c in conditions if c.type == "cost_cap_approach"]
    assert approach_conds == [], (
        "events before the resume-ack must not count toward the "
        "post-ack approach sum"
    )


# ===========================================================================
# Cross-detector independence.
# ===========================================================================


def test_debounce_independent_from_other_detectors(
    cfg: Config, monkeypatch,
):
    """A recent `attention_raised attention_type=task_stuck` event
    must NOT suppress a fresh `cost_cap_approach` fire — debounce is
    per-(attention_type, key), not per-detector-module. Pin the
    cross-detector independence so a different detector firing
    1 minute ago doesn't gate the approach bullet.
    """
    monkeypatch.setenv("AP2_COMPONENTS_AUTO_APPROVE_WINDOW_TOKEN_CAP", "1000")
    now = _dt.datetime(2026, 5, 26, 12, 0, 0, tzinfo=_dt.timezone.utc)
    _seed_approach_sum(cfg, total_tokens=800, now=now)
    # Recent task_stuck fire — different attention_type, different key.
    events.append(
        cfg.events_file, "attention_raised",
        attention_type="task_stuck", key="task_stuck:TB-999",
        summary="unrelated stuck task",
    )
    prior_ts = _ts_seconds_ago(now, seconds_ago=60)
    _rewrite_last_event_ts(cfg, prior_ts)

    conditions = detect_attention_conditions(cfg, now=now)
    approach_conds = [c for c in conditions if c.type == "cost_cap_approach"]
    assert len(approach_conds) == 1
    tail = events.tail(cfg.events_file, 200)
    # The approach condition itself has no prior matching fire.
    assert should_suppress(approach_conds[0], tail=tail, now=now, cfg=cfg) is False
    # Cross-check the find helper agrees.
    assert find_last_attention_fire(
        tail,
        type_="cost_cap_approach", key="cost_cap_approach:window",
    ) is None


# ===========================================================================
# 24h window boundary.
# ===========================================================================


def test_detector_ignores_events_outside_24h_window(
    cfg: Config, monkeypatch,
):
    """Auto-approved spend 25h ago (just past the rolling 24h window)
    → no fire. Pin the window boundary so a refactor that broadens
    the walk doesn't accidentally surface old budget noise.
    """
    monkeypatch.setenv("AP2_COMPONENTS_AUTO_APPROVE_WINDOW_TOKEN_CAP", "1000")
    now = _dt.datetime(2026, 5, 26, 12, 0, 0, tzinfo=_dt.timezone.utc)
    # All spend 25h ago — should not count toward the rolling sum.
    tid = "TB-800"
    _seed_auto_approved_task(cfg, tid)
    _seed_task_run_usage(
        cfg, task_id=tid,
        input_tokens=500, output_tokens=400,
        seconds_ago=25 * 3600,
        now=now,
    )

    conditions = detect_attention_conditions(cfg, now=now)
    approach_conds = [c for c in conditions if c.type == "cost_cap_approach"]
    assert approach_conds == [], conditions


def test_detector_ignores_non_auto_approved_task_spend(
    cfg: Config, monkeypatch,
):
    """`task_run_usage` events for tasks that were never
    `auto_approved` (operator-approved via `ap2 approve`, or
    operator-added with `ap2 add`) MUST NOT count toward the
    approach sum — the budget guard is scoped to the auto-approved
    dispatch axis, not all task spend. Pin the same `_auto_approved_
    task_ids` filter the trip-check uses so the approach surface
    can't surface on operator-driven spend.
    """
    monkeypatch.setenv("AP2_COMPONENTS_AUTO_APPROVE_WINDOW_TOKEN_CAP", "1000")
    now = _dt.datetime(2026, 5, 26, 12, 0, 0, tzinfo=_dt.timezone.utc)
    # Big spend on a task without an `auto_approved` event preceding.
    _seed_task_run_usage(
        cfg, task_id="TB-999",
        input_tokens=500, output_tokens=400,
        seconds_ago=3600,
        now=now,
    )

    conditions = detect_attention_conditions(cfg, now=now)
    approach_conds = [c for c in conditions if c.type == "cost_cap_approach"]
    assert approach_conds == [], conditions


# ===========================================================================
# Structural: approach-sum matches trip-sum (no-drift contract).
# ===========================================================================


def test_approach_sum_matches_trip_check_sum(
    cfg: Config, monkeypatch,
):
    """The briefing's Design clause pins the load-bearing property
    that the approach-detector sum matches the trip-detector sum —
    drift between the two would mean an Attention bullet that
    doesn't predict the eventual pause. Verify structurally: seed
    a sum just above the cap, confirm the trip-check fires, then
    drop below the cap and confirm the approach detector fires
    with the same sum the trip-check would have reported.
    """
    monkeypatch.setenv("AP2_COMPONENTS_AUTO_APPROVE_WINDOW_TOKEN_CAP", "1000")
    now = _dt.datetime(2026, 5, 26, 12, 0, 0, tzinfo=_dt.timezone.utc)
    # Sum just below the cap → approach should fire.
    _seed_approach_sum(cfg, total_tokens=900, now=now)
    conditions = detect_attention_conditions(cfg, now=now)
    approach_conds = [c for c in conditions if c.type == "cost_cap_approach"]
    assert len(approach_conds) == 1
    approach_sum = approach_conds[0].extras["total_tokens_24h"]
    assert approach_sum == 900

    # The trip-check on this same fixture must NOT fire (sum < cap)...
    violation = _auto_approve_check_violations(cfg, now=now)
    assert violation is None, (
        f"trip-check fired unexpectedly at sum={approach_sum} < cap=1000: "
        f"{violation}"
    )

    # ...and adding one more token pushes us to sum=1100 > cap=1000;
    # the trip-check fires with the same arithmetic shape.
    _seed_auto_approved_task(cfg, "TB-900")
    _seed_task_run_usage(
        cfg, task_id="TB-900",
        input_tokens=100, output_tokens=100,
        seconds_ago=300, now=now,
    )
    violation = _auto_approve_check_violations(cfg, now=now)
    assert violation is not None
    reason, total_used, cap, _trigger, _detail = violation
    assert reason == "window_cap"
    assert cap == 1000
    # The trip-check sums the same events the approach-check did,
    # so total_used == 900 + 200 = 1100.
    assert total_used == 1100


# ===========================================================================
# Integration with the union dispatcher.
# ===========================================================================


def test_detect_attention_conditions_includes_cost_cap_approach(
    cfg: Config, monkeypatch,
):
    """`detect_attention_conditions` runs every sibling detector AND
    `_detect_cost_cap_approach` and unions the results. Pin the
    wire-up so a refactor that forgets one detector surfaces.
    """
    monkeypatch.setenv("AP2_COMPONENTS_AUTO_APPROVE_WINDOW_TOKEN_CAP", "1000")
    now = _dt.datetime(2026, 5, 26, 12, 0, 0, tzinfo=_dt.timezone.utc)
    _seed_approach_sum(cfg, total_tokens=800, now=now)

    tail = events.tail(cfg.events_file, 500)
    direct = _detect_cost_cap_approach(cfg, tail=tail, now=now)
    union = detect_attention_conditions(cfg, tail=tail, now=now)
    union_approach = [c for c in union if c.type == "cost_cap_approach"]
    assert [c.key for c in direct] == [c.key for c in union_approach]
    assert union_approach[0].key == "cost_cap_approach:window"


def test_render_attention_section_includes_cost_cap_approach(
    cfg: Config, monkeypatch,
):
    """End-to-end render check: the status-report's
    `render_attention_section` consumes the new detector's condition
    via its generic fallback path (`- ⚠ {cond.summary}`) — no per-
    detector branch needed in the renderer. Pin the render surface
    so a refactor that adds detector-specific branches without
    handling `cost_cap_approach` doesn't silently drop the bullet.
    """
    from ap2.status_report import render_attention_section

    monkeypatch.setenv("AP2_COMPONENTS_AUTO_APPROVE_WINDOW_TOKEN_CAP", "1000")
    now = _dt.datetime(2026, 5, 26, 12, 0, 0, tzinfo=_dt.timezone.utc)
    _seed_approach_sum(cfg, total_tokens=800, now=now)

    rendered = render_attention_section(cfg, since_event_idx=0, now=now)
    assert "## Attention needed" in rendered
    assert "auto-approve cost cap approach" in rendered
    assert "ap2 ack auto_approve_window_resume" in rendered


# ===========================================================================
# Resolver semantics: env-knob fallback + clamp.
# ===========================================================================


def test_cost_approach_pct_default_when_unset(cfg: Config, monkeypatch):
    """`AP2_AUTO_APPROVE_COST_APPROACH_PCT` unset → resolver returns
    the documented default (75). Pin the call-time-env-first
    contract (TB-336: cfg helper) so env-reload propagates without
    re-threading state.
    """
    monkeypatch.delenv("AP2_COMPONENTS_AUTO_APPROVE_COST_APPROACH_PCT", raising=False)
    assert _cost_approach_pct(cfg) == 75


def test_cost_approach_pct_invalid_falls_back(cfg: Config, monkeypatch):
    """Non-int / empty / negative env value → resolver falls back to
    the default. Pin the parse-defensive shape (mirrors
    `_task_stuck_threshold_s` / `_task_frozen_recency_s`).
    """
    monkeypatch.setenv("AP2_COMPONENTS_AUTO_APPROVE_COST_APPROACH_PCT", "not-a-number")
    assert _cost_approach_pct(cfg) == 75
    monkeypatch.setenv("AP2_COMPONENTS_AUTO_APPROVE_COST_APPROACH_PCT", "-5")
    assert _cost_approach_pct(cfg) == 75


def test_cost_approach_pct_clamps_above_99(cfg: Config, monkeypatch):
    """Values >= 100 are clamped to 99. A 100%-of-cap approach
    coincides with the trip line (which the post-trip surface
    owns), so the briefing's "trip-not-approach" semantics for >=
    100 means the resolver caps just below the trip rather than
    raising.
    """
    monkeypatch.setenv("AP2_COMPONENTS_AUTO_APPROVE_COST_APPROACH_PCT", "100")
    assert _cost_approach_pct(cfg) == 99
    monkeypatch.setenv("AP2_COMPONENTS_AUTO_APPROVE_COST_APPROACH_PCT", "150")
    assert _cost_approach_pct(cfg) == 99


# ===========================================================================
# Source-anchor pins mirroring the briefing's Verification greps.
# ===========================================================================


def test_briefing_verification_greps_match():
    """Mirror the briefing's `## Verification` greps in test form so
    a refactor that violates the structural pins surfaces here as a
    clean test failure (parallel to TB-282 / TB-287 / TB-288 / TB-289
    pins).

    Mirrors:
      - `grep -q "_detect_cost_cap_approach" ap2/attention.py`
      - `grep -q "AP2_AUTO_APPROVE_COST_APPROACH_PCT" ap2/config.py`
      - `grep -q "DEFAULT_AUTO_APPROVE_COST_APPROACH_PCT" ap2/config.py`
      - `grep -q "AP2_AUTO_APPROVE_COST_APPROACH_PCT" ap2/env_reload.py`
      - `grep -q "cost_cap_approach" skills/ap2-config/SKILL.md`
      - `grep -q "cost_cap_approach" ap2/architecture.md`
      - `grep -rq "_detect_cost_cap_approach" ap2/tests/`
        (this test module's own filename + `import` line satisfies the
        recursive grep — pinning it here documents the contract.)
      - `! grep -q "_detect_cost_cap_approach" ap2/auto_approve.py`
        (the detector lives in `attention.py`, NOT `auto_approve.py` —
        the gate stays the trip check, attention stays the surface.)
    """
    repo_root = Path(__file__).resolve().parent.parent
    # TB-343: the attention body moved to the sibling impl.py.
    attention_src = (repo_root / "components" / "attention" / "impl.py").read_text()
    config_src = (repo_root / "config.py").read_text()
    env_reload_src = (repo_root / "env_reload.py").read_text()
    # TB-398 carved the attention-knob documentation into
    # `skills/ap2-config/SKILL.md`'s `## Configuration knobs` section, so
    # the operator-facing detector mention now lives in the config skill.
    config_skill_src = (
        repo_root.parent / "skills" / "ap2-config" / "SKILL.md"
    ).read_text()
    architecture_src = (repo_root / "architecture.md").read_text()
    # TB-318 (axis 5): `ap2/auto_approve.py` was relocated to
    # `ap2/components/auto_approve/__init__.py`. The absence-check below
    # pins the same invariant (the detector must NOT live in the
    # auto_approve subpackage body) against the new canonical path.
    auto_approve_src = (
        # TB-343: the auto_approve body moved to the sibling impl.py.
        repo_root / "components" / "auto_approve" / "impl.py"
    ).read_text()
    this_test_src = Path(__file__).read_text()

    assert "_detect_cost_cap_approach" in attention_src
    assert "AP2_AUTO_APPROVE_COST_APPROACH_PCT" in config_src
    assert "DEFAULT_AUTO_APPROVE_COST_APPROACH_PCT" in config_src
    assert "AP2_AUTO_APPROVE_COST_APPROACH_PCT" in env_reload_src
    assert "cost_cap_approach" in config_skill_src
    assert "cost_cap_approach" in architecture_src
    assert "_detect_cost_cap_approach" in this_test_src
    # Absence-check: the detector must NOT live in the auto_approve
    # subpackage body (post-TB-318 path).
    assert "_detect_cost_cap_approach" not in auto_approve_src, (
        "detector function should live in "
        "ap2/components/attention/__init__.py, not "
        "ap2/components/auto_approve/__init__.py — the gate stays the "
        "trip check, attention stays the surface"
    )

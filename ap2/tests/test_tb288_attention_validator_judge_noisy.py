"""TB-288: regression-pin for the proactive `validator_judge_noisy`
attention detector (TB-282 follow-up closing Progress signal #3
"validator-judge anomalies" leg).

Pre-TB-288 the noisy state surfaced only as pull-surfaces or
bottom-of-digest sub-blocks:

  (a) `[noisy]` suffix on `ap2 status`'s validator-judge sub-line
      (TB-243).
  (b) TB-245 status-report sub-block near the bottom of the digest.
  (c) Warn-tinted "Validator judge (24h)" row on the web automation
      card (TB-243).

None of these are the daemon-rendered `## Attention needed` block
that TB-282 places ABOVE the routine progress bullets — exactly the
visual hierarchy the goal.md Current focus #3 Progress signal #3
asks for ("Attention-needing conditions ... surfaced proactively in
operator-legible terms, distinct from routine progress updates").

This module pins the briefing's five named arcs (briefing scope item
2):

  (1) Count below threshold → no fire.
  (2) Count exactly at threshold → one condition (boundary inclusive).
  (3) Count above threshold → still ONE condition (singleton, not
      one bullet per event).
  (4) Debounce respected across consecutive ticks within
      `AP2_ATTENTION_DEBOUNCE_S`.
  (5) Threshold-override via env (`AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD
      =1` fires on a single event).

Plus source-anchor pins mirroring the briefing's Verification greps.
"""
from __future__ import annotations

import datetime as _dt
import json as _json
from pathlib import Path

import pytest

from ap2 import attention, events
from ap2.attention import (
    AttentionCondition,
    _detect_validator_judge_noisy,
    detect_attention_conditions,
    find_last_attention_fire,
    should_suppress,
)
from ap2.automation_status import validator_judge_noisy_threshold
from ap2.config import Config
from ap2.init import init_project


_DEFAULT_THRESHOLD = 5  # matches `validator_judge_noisy_threshold()` default


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch) -> Config:
    """Clean project scaffold with the TB-288 + sibling attention env
    knobs unset so the defaults are the contract under test.

    Also unsets the TB-282 / TB-287 detector knobs so the
    `task_stuck` / `task_frozen` siblings don't false-fire from
    test-seeded events that may co-occur in some arcs.
    """
    monkeypatch.delenv("AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD", raising=False)
    monkeypatch.delenv("AP2_TASK_STUCK_THRESHOLD_S", raising=False)
    monkeypatch.delenv("AP2_TASK_FROZEN_RECENCY_S", raising=False)
    monkeypatch.delenv("AP2_ATTENTION_DEBOUNCE_S", raising=False)
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

    Mirrors TB-282 / TB-287's tests — `events.append` always stamps
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


def _emit_judge_event(
    cfg: Config,
    *,
    seconds_ago: float,
    now: _dt.datetime,
    event_type: str = "validator_judge_fail",
) -> str:
    """Emit a `validator_judge_fail` (or `_timeout`) event and rewrite
    its `ts` to `seconds_ago` before `now`. Returns the rewritten
    timestamp.
    """
    events.append(
        cfg.events_file, event_type,
        timeout_s=60.0, briefing_bytes=4000, max_turns=2,
    )
    ts = _ts_seconds_ago(now, seconds_ago=seconds_ago)
    _rewrite_last_event_ts(cfg, ts)
    return ts


def _seed_judge_events(
    cfg: Config,
    *,
    fail_count: int,
    timeout_count: int,
    now: _dt.datetime,
    seconds_ago_base: float = 3600,
) -> None:
    """Seed `fail_count` `validator_judge_fail` + `timeout_count`
    `validator_judge_timeout` events within the 24h window. Each event
    is spaced 60s apart starting from `seconds_ago_base` so they read
    as a temporally-realistic burst rather than a single-second
    cluster.
    """
    offset = 0.0
    for _ in range(fail_count):
        _emit_judge_event(
            cfg, seconds_ago=seconds_ago_base + offset, now=now,
            event_type="validator_judge_fail",
        )
        offset += 60.0
    for _ in range(timeout_count):
        _emit_judge_event(
            cfg, seconds_ago=seconds_ago_base + offset, now=now,
            event_type="validator_judge_timeout",
        )
        offset += 60.0


# ===========================================================================
# Arc 1: count below threshold — detector stays quiet.
# ===========================================================================


def test_detector_misses_when_count_below_threshold(cfg: Config):
    """`fail+timeout = 4 < default 5` → detector returns `[]`. Pin
    the strict-inequality seam: a 4-event window must NOT fire, or
    operators would see the Attention bullet on every transient
    cluster.
    """
    now = _dt.datetime(2026, 5, 26, 12, 0, 0, tzinfo=_dt.timezone.utc)
    _seed_judge_events(cfg, fail_count=2, timeout_count=2, now=now)

    conditions = detect_attention_conditions(cfg, now=now)
    noisy_conds = [c for c in conditions if c.type == "validator_judge_noisy"]
    assert noisy_conds == [], conditions


def test_detector_ignores_events_outside_24h_window(cfg: Config):
    """Five events 25h ago (just past the rolling 24h window) → no
    fire. Pin the window boundary so a refactor that broadens the
    walk doesn't accidentally surface old burst noise.
    """
    now = _dt.datetime(2026, 5, 26, 12, 0, 0, tzinfo=_dt.timezone.utc)
    _seed_judge_events(
        cfg,
        fail_count=5, timeout_count=0,
        now=now,
        seconds_ago_base=86400 + 3600,  # 25h ago
    )

    conditions = detect_attention_conditions(cfg, now=now)
    noisy_conds = [c for c in conditions if c.type == "validator_judge_noisy"]
    assert noisy_conds == [], conditions


# ===========================================================================
# Arc 2: count exactly at threshold — boundary fires.
# ===========================================================================


def test_detector_fires_when_count_at_threshold(cfg: Config):
    """`fail+timeout = 5 == default 5` → one condition. Inclusive
    boundary is the load-bearing contract; mirrors TB-272's
    `_pause_reason` arithmetic (`>=`, not `>`).
    """
    now = _dt.datetime(2026, 5, 26, 12, 0, 0, tzinfo=_dt.timezone.utc)
    _seed_judge_events(cfg, fail_count=3, timeout_count=2, now=now)

    conditions = detect_attention_conditions(cfg, now=now)
    noisy_conds = [c for c in conditions if c.type == "validator_judge_noisy"]
    assert len(noisy_conds) == 1, conditions
    cond = noisy_conds[0]
    assert cond.type == "validator_judge_noisy"
    assert cond.key == "validator_judge_noisy"  # singleton key, NOT per-task
    assert "validator-judge noisy" in cond.summary
    assert "3+2=5" in cond.summary
    assert f"threshold {_DEFAULT_THRESHOLD}" in cond.summary
    assert cond.extras["fail_count_24h"] == 3
    assert cond.extras["timeout_count_24h"] == 2
    assert cond.extras["threshold"] == _DEFAULT_THRESHOLD
    assert cond.extras["window_s"] == 86400


# ===========================================================================
# Arc 3: count above threshold — still ONE condition (singleton).
# ===========================================================================


def test_detector_returns_singleton_above_threshold(cfg: Config):
    """`fail+timeout = 20`, well above threshold → exactly ONE
    condition, NOT one per event. The Attention surface is a
    project-wide noisy-window indicator, not a per-event log dump.
    Pin the singleton contract so a refactor that surfaces per-event
    bullets doesn't drown the operator.
    """
    now = _dt.datetime(2026, 5, 26, 12, 0, 0, tzinfo=_dt.timezone.utc)
    _seed_judge_events(cfg, fail_count=10, timeout_count=10, now=now)

    conditions = detect_attention_conditions(cfg, now=now)
    noisy_conds = [c for c in conditions if c.type == "validator_judge_noisy"]
    assert len(noisy_conds) == 1, conditions
    cond = noisy_conds[0]
    assert cond.extras["fail_count_24h"] == 10
    assert cond.extras["timeout_count_24h"] == 10
    # Singleton key — same regardless of count. Pin the dedup key so
    # the daemon's debounce treats consecutive ticks as the same
    # condition.
    assert cond.key == "validator_judge_noisy"


# ===========================================================================
# Arc 4: debounce respected across consecutive ticks.
# ===========================================================================


def test_detector_debounce_suppresses_within_window(cfg: Config):
    """A prior `attention_raised attention_type=validator_judge_noisy`
    event within `AP2_ATTENTION_DEBOUNCE_S` (default 6h) → the
    `should_suppress` helper returns True for the still-noisy
    condition. Pin the per-(type, key) debounce contract so a
    sustained noisy window doesn't re-fire every tick.
    """
    now = _dt.datetime(2026, 5, 26, 12, 0, 0, tzinfo=_dt.timezone.utc)
    _seed_judge_events(cfg, fail_count=5, timeout_count=0, now=now)
    # Emit a prior fire 1h ago — well inside the 6h default debounce.
    events.append(
        cfg.events_file, "attention_raised",
        attention_type="validator_judge_noisy",
        key="validator_judge_noisy",
        summary="prior fire",
        fail_count_24h=5, timeout_count_24h=0,
        threshold=_DEFAULT_THRESHOLD, window_s=86400,
    )
    prior_ts = _ts_seconds_ago(now, seconds_ago=3600)  # 1h ago
    _rewrite_last_event_ts(cfg, prior_ts)

    conditions = detect_attention_conditions(cfg, now=now)
    noisy_conds = [c for c in conditions if c.type == "validator_judge_noisy"]
    # Detector still produces the candidate (state IS noisy)...
    assert len(noisy_conds) == 1
    # ...but the daemon's debounce check suppresses re-emission.
    tail = events.tail(cfg.events_file, 100)
    assert should_suppress(noisy_conds[0], tail=tail, now=now) is True


def test_detector_debounce_releases_after_window(cfg: Config):
    """A prior fire 7h ago (past the 6h default debounce) →
    `should_suppress` returns False, so a still-noisy state surfaces
    again on this tick. Pin the cadence — operators get re-notified
    once per debounce window for sustained issues.
    """
    now = _dt.datetime(2026, 5, 26, 12, 0, 0, tzinfo=_dt.timezone.utc)
    _seed_judge_events(cfg, fail_count=5, timeout_count=0, now=now)
    events.append(
        cfg.events_file, "attention_raised",
        attention_type="validator_judge_noisy",
        key="validator_judge_noisy",
        summary="prior fire",
        fail_count_24h=5, timeout_count_24h=0,
        threshold=_DEFAULT_THRESHOLD, window_s=86400,
    )
    prior_ts = _ts_seconds_ago(now, seconds_ago=7 * 3600)  # 7h ago
    _rewrite_last_event_ts(cfg, prior_ts)

    conditions = detect_attention_conditions(cfg, now=now)
    noisy_conds = [c for c in conditions if c.type == "validator_judge_noisy"]
    assert len(noisy_conds) == 1
    tail = events.tail(cfg.events_file, 100)
    assert should_suppress(noisy_conds[0], tail=tail, now=now) is False


def test_debounce_independent_from_other_detectors(cfg: Config):
    """A recent `attention_raised attention_type=task_stuck` event
    must NOT suppress a fresh `validator_judge_noisy` fire — debounce
    is per-(attention_type, key), not per-detector-module. Pin the
    cross-detector independence so a different detector firing
    1 minute ago doesn't gate the noisy bullet.
    """
    now = _dt.datetime(2026, 5, 26, 12, 0, 0, tzinfo=_dt.timezone.utc)
    _seed_judge_events(cfg, fail_count=5, timeout_count=0, now=now)
    # Recent task_stuck fire — different attention_type, different key.
    events.append(
        cfg.events_file, "attention_raised",
        attention_type="task_stuck", key="task_stuck:TB-999",
        summary="unrelated stuck task",
    )
    prior_ts = _ts_seconds_ago(now, seconds_ago=60)  # 1min ago
    _rewrite_last_event_ts(cfg, prior_ts)

    conditions = detect_attention_conditions(cfg, now=now)
    noisy_conds = [c for c in conditions if c.type == "validator_judge_noisy"]
    assert len(noisy_conds) == 1
    tail = events.tail(cfg.events_file, 100)
    # The noisy condition itself has no prior matching fire.
    assert should_suppress(noisy_conds[0], tail=tail, now=now) is False
    # Cross-check the find helper agrees.
    assert find_last_attention_fire(
        tail, type_="validator_judge_noisy", key="validator_judge_noisy",
    ) is None


# ===========================================================================
# Arc 5: env-knob override + singleton-with-tightened-threshold.
# ===========================================================================


def test_threshold_override_fires_on_single_event(
    cfg: Config, monkeypatch,
):
    """`AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD=1` → a single
    `validator_judge_fail` event fires the Attention bullet. Pin
    the env-knob path end-to-end — operator tuning the threshold
    via `.cc-autopilot/env` takes effect on the next detector tick
    without code changes.
    """
    monkeypatch.setenv("AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD", "1")
    assert validator_judge_noisy_threshold() == 1

    now = _dt.datetime(2026, 5, 26, 12, 0, 0, tzinfo=_dt.timezone.utc)
    _seed_judge_events(cfg, fail_count=1, timeout_count=0, now=now)

    conditions = detect_attention_conditions(cfg, now=now)
    noisy_conds = [c for c in conditions if c.type == "validator_judge_noisy"]
    assert len(noisy_conds) == 1, conditions
    cond = noisy_conds[0]
    assert cond.extras["threshold"] == 1
    assert cond.extras["fail_count_24h"] == 1
    assert "threshold 1" in cond.summary


def test_threshold_override_zero_falls_back_to_default(
    cfg: Config, monkeypatch,
):
    """`AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD=0` → the resolver
    normalizes to the default (5), so a 4-event window stays
    quiet. Pin the safe-default rule from the resolver — operator
    typo of `0` MUST NOT silently disable the safety surface.
    """
    monkeypatch.setenv("AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD", "0")
    assert validator_judge_noisy_threshold() == 5

    now = _dt.datetime(2026, 5, 26, 12, 0, 0, tzinfo=_dt.timezone.utc)
    _seed_judge_events(cfg, fail_count=4, timeout_count=0, now=now)

    conditions = detect_attention_conditions(cfg, now=now)
    noisy_conds = [c for c in conditions if c.type == "validator_judge_noisy"]
    assert noisy_conds == [], conditions


def test_validator_judge_noisy_env_knob_is_hot_reloadable():
    """`AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD` lands in
    `HOT_RELOADABLE_KNOBS` (verified pre-TB-288 by TB-243; pinned
    here so a refactor that drops it surfaces in THIS detector's
    test module too). Mirrors TB-287's
    `test_task_frozen_env_knob_is_hot_reloadable`.
    """
    from ap2.env_reload import HOT_RELOADABLE_KNOBS

    assert "AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD" in HOT_RELOADABLE_KNOBS


# ===========================================================================
# Integration with the union dispatcher + render-verbatim check.
# ===========================================================================


def test_detect_attention_conditions_includes_validator_judge_noisy(
    cfg: Config,
):
    """`detect_attention_conditions` runs `_detect_task_stuck`,
    `_detect_task_frozen`, AND `_detect_validator_judge_noisy` and
    unions the results. Pin the wire-up so a refactor that forgets
    one detector surfaces.
    """
    now = _dt.datetime(2026, 5, 26, 12, 0, 0, tzinfo=_dt.timezone.utc)
    _seed_judge_events(cfg, fail_count=3, timeout_count=2, now=now)

    tail = events.tail(cfg.events_file, 100)
    direct = _detect_validator_judge_noisy(cfg, tail=tail, now=now)
    union = detect_attention_conditions(cfg, tail=tail, now=now)
    union_noisy = [c for c in union if c.type == "validator_judge_noisy"]
    assert [c.key for c in direct] == [c.key for c in union_noisy]
    assert union_noisy[0].key == "validator_judge_noisy"


def test_render_attention_section_includes_validator_judge_noisy(cfg: Config):
    """End-to-end render check: the status-report's
    `render_attention_section` consumes the new detector's condition
    via its generic fallback path (`- ⚠ {cond.summary}`) — no per-
    detector branch needed in the renderer. Pin the render surface
    so a refactor that adds detector-specific branches without
    handling `validator_judge_noisy` doesn't silently drop the
    bullet.
    """
    from ap2.status_report import render_attention_section

    now = _dt.datetime(2026, 5, 26, 12, 0, 0, tzinfo=_dt.timezone.utc)
    _seed_judge_events(cfg, fail_count=5, timeout_count=0, now=now)

    rendered = render_attention_section(cfg, since_event_idx=0)
    assert "## Attention needed" in rendered
    assert "validator-judge noisy" in rendered
    assert "5+0=5" in rendered


# ===========================================================================
# Source-anchor pins mirroring the briefing's Verification greps.
# ===========================================================================


def test_briefing_verification_greps_match():
    """Mirror the briefing's `## Verification` greps in test form so
    a refactor that violates the structural pins surfaces here as a
    clean test failure (parallel to TB-282 / TB-287 pins).

    Mirrors:
      - `grep -q "_detect_validator_judge_noisy" ap2/attention.py`
      - `grep -q "validator_judge_noisy" ap2/howto.md`
      - `grep -q "validator_judge_noisy" ap2/architecture.md`
      - `grep -rq "_detect_validator_judge_noisy" ap2/tests/`
        (the test module's own filename + `import` line satisfies the
        recursive grep — pinning it here documents the contract.)
    """
    repo_root = Path(__file__).resolve().parent.parent
    attention_src = (repo_root / "attention.py").read_text()
    howto_src = (repo_root / "howto.md").read_text()
    architecture_src = (repo_root / "architecture.md").read_text()
    this_test_src = Path(__file__).read_text()

    assert "_detect_validator_judge_noisy" in attention_src
    assert "validator_judge_noisy" in howto_src
    assert "validator_judge_noisy" in architecture_src
    assert "_detect_validator_judge_noisy" in this_test_src

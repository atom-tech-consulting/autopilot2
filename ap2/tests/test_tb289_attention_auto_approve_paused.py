"""TB-289: regression-pin for the proactive `auto_approve_paused`
attention detector (TB-282 follow-up closing Progress signal #3
"pending decision" leg).

Pre-TB-289 an active auto-approve pause surfaced ONLY as:

  (a) A TB-228 automation-digest sub-block line near the BOTTOM of the
      cron status-report (`auto-approve: disabled (paused: <reason>)`).
  (b) A line in `ap2 status` text / `--json` output.
  (c) A warn-tinted row on the web automation card.

None of these are the daemon-rendered `## Attention needed` block at
the TOP of the cron status-report that TB-282 promotes above the
routine progress bullets — exactly the visual hierarchy Current
focus #3 Progress signal #3 names ("Attention-needing conditions ...
surfaced proactively in operator-legible terms, distinct from routine
progress updates"). The operator's `ack` is the only path back to
dispatch, so a paused auto-approve IS a pending decision and belongs
in the attention surface.

This module pins six arcs (briefing scope item 2):

  (1) No-fire when `pause_reason is None` (auto-approve actively
      running, no halt).
  (2) Fires-on-consecutive-freezes (with the consecutive-freeze
      count surfaced in the extras and the `auto_approve_unfreeze`
      ack verb named in the summary).
  (3) Fires-on-validator-judge-noisy (with the fail+timeout 24h
      counts surfaced in the extras).
  (4) Per-reason dedup key (`auto_approve_paused:<reason>` so a
      sequential reason transition surfaces both bullets — distinct
      conditions, distinct keys).
  (5) No-fire when auto-approve is disabled via `AP2_AUTO_APPROVE=0`
      AND no halt-class signal is in-effect (disabled is not paused
      — distinct states).
  (6) Debounce respected across consecutive ticks within
      `AP2_ATTENTION_DEBOUNCE_S`.

Plus source-anchor pins mirroring the briefing's Verification greps.
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
    _detect_auto_approve_paused,
    detect_attention_conditions,
    find_last_attention_fire,
    should_suppress,
)
from ap2.automation_status import (
    _PAUSE_REASON_ACK_VERB,
    collect_auto_approve_state,
)
from ap2.config import Config
from ap2.init import init_project


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch) -> Config:
    """Clean project scaffold with the sibling attention-detector env
    knobs unset so the defaults are the contract under test.

    Unsets the TB-282 / TB-287 / TB-288 detector knobs so those
    siblings don't false-fire from test-seeded events that may
    co-occur in some arcs; explicitly leaves the auto-approve knob
    unset by default so each test sets it as needed.
    """
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

    Mirrors TB-282 / TB-287 / TB-288's tests — `events.append` always
    stamps `now()`; tests that need an event "at a specific time"
    rewrite the line afterward.
    """
    lines = cfg.events_file.read_text().splitlines()
    if not lines:
        return
    last = _json.loads(lines[-1])
    last["ts"] = ts
    lines[-1] = _json.dumps(last)
    cfg.events_file.write_text("\n".join(lines) + "\n")


def _seed_consecutive_freezes(
    cfg: Config, *, count: int, now: _dt.datetime,
) -> None:
    """Seed `count` consecutive `task_complete status=verification_failed`
    events followed by an `auto_approve_paused` halt event so
    `collect_auto_approve_state` returns `pause_reason="consecutive_
    freezes"`.

    The `auto_approve_paused` event is the load-bearing discriminator
    for `_pause_reason`'s consecutive-freezes branch — without it the
    streak count rises but `pause_reason` stays None.
    """
    for i in range(count):
        events.append(
            cfg.events_file, "task_complete",
            task=f"TB-90{i}", status="verification_failed",
            commit="", summary="seeded freeze",
        )
        ts = _ts_seconds_ago(now, seconds_ago=3600 - i * 60)
        _rewrite_last_event_ts(cfg, ts)
    # The daemon emits `auto_approve_paused` when the streak hits the
    # threshold; mirror that here so `_pause_reason` finds the halt
    # event.
    events.append(
        cfg.events_file, "auto_approve_paused",
        consecutive_freezes=count,
    )
    paused_ts = _ts_seconds_ago(now, seconds_ago=60)
    _rewrite_last_event_ts(cfg, paused_ts)


def _seed_judge_events(
    cfg: Config,
    *,
    fail_count: int,
    timeout_count: int,
    now: _dt.datetime,
    seconds_ago_base: float = 3600,
) -> None:
    """Seed `fail_count` `validator_judge_fail` + `timeout_count`
    `validator_judge_timeout` events within the 24h window. Mirrors
    TB-288's helper of the same name."""
    offset = 0.0
    for _ in range(fail_count):
        events.append(
            cfg.events_file, "validator_judge_fail",
            timeout_s=60.0, briefing_bytes=4000, max_turns=2,
        )
        ts = _ts_seconds_ago(now, seconds_ago=seconds_ago_base + offset)
        _rewrite_last_event_ts(cfg, ts)
        offset += 60.0
    for _ in range(timeout_count):
        events.append(
            cfg.events_file, "validator_judge_timeout",
            timeout_s=60.0, briefing_bytes=4000, max_turns=2,
        )
        ts = _ts_seconds_ago(now, seconds_ago=seconds_ago_base + offset)
        _rewrite_last_event_ts(cfg, ts)
        offset += 60.0


# ===========================================================================
# Arc 1: no-fire when `pause_reason is None`.
# ===========================================================================


def test_detector_misses_when_pause_reason_is_none(cfg: Config):
    """Fresh project with no halt events → `collect_auto_approve_state`
    returns `pause_reason=None` → detector returns `[]`. Pin the
    happy-path silence: an actively-running (or never-paused) project
    must NOT see a phantom Attention bullet.
    """
    now = _dt.datetime(2026, 5, 26, 12, 0, 0, tzinfo=_dt.timezone.utc)
    state = collect_auto_approve_state(cfg, now=now)
    assert state["pause_reason"] is None, state

    conditions = detect_attention_conditions(cfg, now=now)
    pause_conds = [c for c in conditions if c.type == "auto_approve_paused"]
    assert pause_conds == [], conditions


def test_detector_misses_when_auto_approve_disabled(
    cfg: Config, monkeypatch,
):
    """`AP2_AUTO_APPROVE=0` (or unset) with no halt-class signal →
    pause_reason is None → no Attention bullet. Disabled is NOT
    paused — these are distinct operator-facing states. Pin the
    distinction so a refactor that conflates "disabled" with
    "paused" (e.g. by surfacing every `auto_approve_enabled=False`
    project) doesn't drown the operator's first-touch surface in
    permanently-quiet projects.
    """
    monkeypatch.setenv("AP2_COMPONENTS_AUTO_APPROVE_ENABLED", "0")
    now = _dt.datetime(2026, 5, 26, 12, 0, 0, tzinfo=_dt.timezone.utc)
    state = collect_auto_approve_state(cfg, now=now)
    assert state["auto_approve_enabled"] is False
    assert state["pause_reason"] is None

    conditions = detect_attention_conditions(cfg, now=now)
    pause_conds = [c for c in conditions if c.type == "auto_approve_paused"]
    assert pause_conds == [], conditions


# ===========================================================================
# Arc 2: fires on `consecutive_freezes`.
# ===========================================================================


def test_detector_fires_on_consecutive_freezes(cfg: Config, monkeypatch):
    """Three consecutive `task_complete status=verification_failed`
    events + a trailing `auto_approve_paused` halt → detector returns
    one `AttentionCondition` keyed `auto_approve_paused:consecutive_
    freezes`, naming the `auto_approve_unfreeze` ack verb in the
    summary. The `consecutive_freezes` count surfaces in extras so the
    event-stream reader has the diagnostic context inline (per the
    briefing's extras contract).
    """
    monkeypatch.setenv("AP2_COMPONENTS_AUTO_APPROVE_FREEZE_THRESHOLD", "3")
    now = _dt.datetime(2026, 5, 26, 12, 0, 0, tzinfo=_dt.timezone.utc)
    _seed_consecutive_freezes(cfg, count=3, now=now)

    state = collect_auto_approve_state(cfg, now=now)
    assert state["pause_reason"] == "consecutive_freezes"
    assert state["consecutive_freezes"] == 3

    conditions = detect_attention_conditions(cfg, now=now)
    pause_conds = [c for c in conditions if c.type == "auto_approve_paused"]
    assert len(pause_conds) == 1, conditions
    cond = pause_conds[0]
    assert cond.type == "auto_approve_paused"
    assert cond.key == "auto_approve_paused:consecutive_freezes"
    assert "auto-approve paused" in cond.summary
    assert "consecutive_freezes" in cond.summary
    assert "ap2 ack auto_approve_unfreeze" in cond.summary
    assert cond.extras["pause_reason"] == "consecutive_freezes"
    assert cond.extras["ack_verb"] == "auto_approve_unfreeze"
    # The briefing names `consecutive_freezes` as one of the count
    # fields surfaced in extras — pin its presence + value so a
    # refactor that drops the diagnostic context surfaces here.
    assert cond.extras["consecutive_freezes"] == 3


# ===========================================================================
# Arc 3: fires on `validator_judge_noisy`.
# ===========================================================================


def test_detector_fires_on_validator_judge_noisy(cfg: Config):
    """Five `validator_judge_fail` events in the rolling 24h window →
    `collect_auto_approve_state` returns
    `pause_reason="validator_judge_noisy"` (TB-272 safety-floor
    priority — overrides cost / consecutive-freeze halts).
    Detector returns one condition keyed
    `auto_approve_paused:validator_judge_noisy`, with the 24h
    fail+timeout counts surfaced in extras.
    """
    now = _dt.datetime(2026, 5, 26, 12, 0, 0, tzinfo=_dt.timezone.utc)
    _seed_judge_events(cfg, fail_count=3, timeout_count=2, now=now)

    state = collect_auto_approve_state(cfg, now=now)
    assert state["pause_reason"] == "validator_judge_noisy"
    assert state["validator_judge_fail_count_24h"] == 3
    assert state["validator_judge_timeout_count_24h"] == 2

    conditions = detect_attention_conditions(cfg, now=now)
    pause_conds = [c for c in conditions if c.type == "auto_approve_paused"]
    assert len(pause_conds) == 1, conditions
    cond = pause_conds[0]
    assert cond.type == "auto_approve_paused"
    assert cond.key == "auto_approve_paused:validator_judge_noisy"
    assert "validator_judge_noisy" in cond.summary
    # TB-272 maps validator_judge_noisy to the same `auto_approve_
    # unfreeze` verb as consecutive_freezes for operator muscle-memory.
    assert "ap2 ack auto_approve_unfreeze" in cond.summary
    assert cond.extras["pause_reason"] == "validator_judge_noisy"
    assert cond.extras["ack_verb"] == "auto_approve_unfreeze"
    assert cond.extras["validator_judge_fail_count_24h"] == 3
    assert cond.extras["validator_judge_timeout_count_24h"] == 2


# ===========================================================================
# Arc 4: per-reason dedup key — sequential reasons surface as distinct bullets.
# ===========================================================================


def test_detector_dedup_key_is_per_reason(cfg: Config, monkeypatch):
    """A prior `attention_raised attention_type=auto_approve_paused
    key=auto_approve_paused:consecutive_freezes` event 1 minute ago
    MUST NOT suppress a fresh `auto_approve_paused:validator_judge_
    noisy` condition — debounce is per-(type, key), and the briefing's
    "per-reason dedup so a sequential reason transition surfaces both"
    contract demands distinct keys for distinct reasons.

    Pin the contract end-to-end: seed a recent freezes-fire then a
    fresh noisy condition; the noisy bullet must NOT be suppressed.
    """
    now = _dt.datetime(2026, 5, 26, 12, 0, 0, tzinfo=_dt.timezone.utc)
    # Recent prior fire for a DIFFERENT reason — must not block the
    # current detector from emitting a fresh key.
    events.append(
        cfg.events_file, "attention_raised",
        attention_type="auto_approve_paused",
        key="auto_approve_paused:consecutive_freezes",
        summary="prior consecutive_freezes fire",
        pause_reason="consecutive_freezes",
        ack_verb="auto_approve_unfreeze",
    )
    prior_ts = _ts_seconds_ago(now, seconds_ago=60)
    _rewrite_last_event_ts(cfg, prior_ts)

    # Now seed a noisy state that resolves to a DIFFERENT pause_reason.
    _seed_judge_events(cfg, fail_count=5, timeout_count=0, now=now)
    state = collect_auto_approve_state(cfg, now=now)
    assert state["pause_reason"] == "validator_judge_noisy"

    conditions = detect_attention_conditions(cfg, now=now)
    pause_conds = [c for c in conditions if c.type == "auto_approve_paused"]
    assert len(pause_conds) == 1
    assert pause_conds[0].key == "auto_approve_paused:validator_judge_noisy"

    # The debounce check on the noisy condition must NOT find the
    # prior consecutive_freezes fire (different key family).
    tail = events.tail(cfg.events_file, 200)
    assert should_suppress(pause_conds[0], tail=tail, now=now, cfg=cfg) is False
    # Cross-check the find helper.
    assert find_last_attention_fire(
        tail,
        type_="auto_approve_paused",
        key="auto_approve_paused:validator_judge_noisy",
    ) is None
    # But the prior consecutive_freezes fire IS still findable on its
    # own key — pin the per-(type, key) lookup symmetry.
    found_prior = find_last_attention_fire(
        tail,
        type_="auto_approve_paused",
        key="auto_approve_paused:consecutive_freezes",
    )
    assert found_prior is not None


# ===========================================================================
# Arc 5: debounce respected across consecutive ticks within window.
# ===========================================================================


def test_detector_debounce_suppresses_within_window(cfg: Config):
    """A prior `attention_raised attention_type=auto_approve_paused
    key=auto_approve_paused:validator_judge_noisy` event 1h ago (well
    inside the 6h default debounce) → `should_suppress` returns True
    for the still-paused condition. Pin the per-(type, key) debounce
    contract so a sustained pause window doesn't re-fire every tick.
    """
    now = _dt.datetime(2026, 5, 26, 12, 0, 0, tzinfo=_dt.timezone.utc)
    _seed_judge_events(cfg, fail_count=5, timeout_count=0, now=now)
    # Emit a prior fire 1h ago — well inside the 6h default debounce.
    events.append(
        cfg.events_file, "attention_raised",
        attention_type="auto_approve_paused",
        key="auto_approve_paused:validator_judge_noisy",
        summary="prior fire",
        pause_reason="validator_judge_noisy",
        ack_verb="auto_approve_unfreeze",
    )
    prior_ts = _ts_seconds_ago(now, seconds_ago=3600)
    _rewrite_last_event_ts(cfg, prior_ts)

    conditions = detect_attention_conditions(cfg, now=now)
    pause_conds = [c for c in conditions if c.type == "auto_approve_paused"]
    # Detector still produces the candidate (state IS paused)...
    assert len(pause_conds) == 1
    # ...but the daemon's debounce check suppresses re-emission.
    tail = events.tail(cfg.events_file, 200)
    assert should_suppress(pause_conds[0], tail=tail, now=now, cfg=cfg) is True


def test_detector_debounce_releases_after_window(cfg: Config):
    """A prior fire 7h ago (past the 6h default debounce) →
    `should_suppress` returns False, so a still-paused state surfaces
    again on this tick. Pin the cadence — operators get re-notified
    once per debounce window for sustained issues (mirrors TB-287 /
    TB-288's debounce-release contract).
    """
    now = _dt.datetime(2026, 5, 26, 12, 0, 0, tzinfo=_dt.timezone.utc)
    _seed_judge_events(cfg, fail_count=5, timeout_count=0, now=now)
    events.append(
        cfg.events_file, "attention_raised",
        attention_type="auto_approve_paused",
        key="auto_approve_paused:validator_judge_noisy",
        summary="prior fire",
        pause_reason="validator_judge_noisy",
        ack_verb="auto_approve_unfreeze",
    )
    prior_ts = _ts_seconds_ago(now, seconds_ago=7 * 3600)
    _rewrite_last_event_ts(cfg, prior_ts)

    conditions = detect_attention_conditions(cfg, now=now)
    pause_conds = [c for c in conditions if c.type == "auto_approve_paused"]
    assert len(pause_conds) == 1
    tail = events.tail(cfg.events_file, 200)
    assert should_suppress(pause_conds[0], tail=tail, now=now, cfg=cfg) is False


# ===========================================================================
# Arc 6: env opt-out for the noisy pause leaves the consecutive-freezes path.
# ===========================================================================


def test_detector_respects_noisy_pause_opt_out(
    cfg: Config, monkeypatch,
):
    """`AP2_AUTO_APPROVE_NOISY_PAUSE_DISABLED=1` → `_pause_reason`
    returns None for a noisy-but-not-otherwise-halted project →
    detector stays quiet. Pin the operator's "trust the upstream
    judge degradation surface" escape hatch so a noisy window
    doesn't fire the Attention bullet when the operator has
    explicitly opted out of the pause behavior.
    """
    monkeypatch.setenv("AP2_COMPONENTS_AUTO_APPROVE_NOISY_PAUSE_DISABLED", "1")
    now = _dt.datetime(2026, 5, 26, 12, 0, 0, tzinfo=_dt.timezone.utc)
    _seed_judge_events(cfg, fail_count=5, timeout_count=0, now=now)

    state = collect_auto_approve_state(cfg, now=now)
    assert state["pause_reason"] is None

    conditions = detect_attention_conditions(cfg, now=now)
    pause_conds = [c for c in conditions if c.type == "auto_approve_paused"]
    assert pause_conds == [], conditions


# ===========================================================================
# Integration with the union dispatcher + render path.
# ===========================================================================


def test_detect_attention_conditions_includes_auto_approve_paused(
    cfg: Config, monkeypatch,
):
    """`detect_attention_conditions` runs every sibling detector AND
    `_detect_auto_approve_paused` and unions the results. Pin the
    wire-up so a refactor that forgets one detector surfaces.
    """
    monkeypatch.setenv("AP2_COMPONENTS_AUTO_APPROVE_FREEZE_THRESHOLD", "3")
    now = _dt.datetime(2026, 5, 26, 12, 0, 0, tzinfo=_dt.timezone.utc)
    _seed_consecutive_freezes(cfg, count=3, now=now)

    tail = events.tail(cfg.events_file, 200)
    direct = _detect_auto_approve_paused(cfg, tail=tail, now=now)
    union = detect_attention_conditions(cfg, tail=tail, now=now)
    union_paused = [c for c in union if c.type == "auto_approve_paused"]
    assert [c.key for c in direct] == [c.key for c in union_paused]
    assert union_paused[0].key == "auto_approve_paused:consecutive_freezes"


def test_render_attention_section_includes_auto_approve_paused(
    cfg: Config, monkeypatch,
):
    """End-to-end render check: the status-report's
    `render_attention_section` consumes the new detector's condition
    via its generic fallback path (`- ⚠ {cond.summary}`) — no per-
    detector branch needed in the renderer. Pin the render surface
    so a refactor that adds detector-specific branches without
    handling `auto_approve_paused` doesn't silently drop the bullet.
    """
    from ap2.status_report import render_attention_section

    monkeypatch.setenv("AP2_COMPONENTS_AUTO_APPROVE_FREEZE_THRESHOLD", "3")
    now = _dt.datetime(2026, 5, 26, 12, 0, 0, tzinfo=_dt.timezone.utc)
    _seed_consecutive_freezes(cfg, count=3, now=now)

    rendered = render_attention_section(cfg, since_event_idx=0, now=now)
    assert "## Attention needed" in rendered
    assert "auto-approve paused: consecutive_freezes" in rendered
    assert "ap2 ack auto_approve_unfreeze" in rendered


# ===========================================================================
# Source-anchor pins mirroring the briefing's Verification greps.
# ===========================================================================


def test_briefing_verification_greps_match():
    """Mirror the briefing's `## Verification` greps in test form so
    a refactor that violates the structural pins surfaces here as a
    clean test failure (parallel to TB-282 / TB-287 / TB-288 pins).

    Mirrors:
      - `grep -q "_detect_auto_approve_paused" ap2/attention.py`
      - `grep -q "auto_approve_paused" skills/ap2-config/SKILL.md`
      - `grep -q "auto_approve_paused" ap2/architecture.md`
      - `grep -rq "_detect_auto_approve_paused" ap2/tests/`
        (this test module's own filename + `import` line satisfies the
        recursive grep — pinning it here documents the contract.)
    """
    repo_root = Path(__file__).resolve().parent.parent
    # TB-343: the attention body moved to the sibling impl.py.
    attention_src = (repo_root / "components" / "attention" / "impl.py").read_text()
    # TB-398 carved the attention-knob documentation into
    # `skills/ap2-config/SKILL.md`'s `## Configuration knobs` section, so
    # the operator-facing detector mention now lives in the config skill.
    config_skill_src = (
        repo_root / "skills" / "ap2-config" / "SKILL.md"
    ).read_text()
    architecture_src = (repo_root / "architecture.md").read_text()
    this_test_src = Path(__file__).read_text()

    assert "_detect_auto_approve_paused" in attention_src
    assert "auto_approve_paused" in config_skill_src
    assert "auto_approve_paused" in architecture_src
    assert "_detect_auto_approve_paused" in this_test_src


def test_pause_reason_ack_verb_map_covers_all_today_reasons():
    """Pin the contract that every pause_reason
    `collect_auto_approve_state` can return today is mapped to an
    ack verb in `_PAUSE_REASON_ACK_VERB`. The detector's bullet shape
    depends on the verb mapping: a pause_reason without a registered
    verb would skip the bullet (defensive `if not ack_verb: return []`
    branch in `_detect_auto_approve_paused`), so a refactor that adds
    a new pause_reason without the verb mapping would silently drop
    the operator-facing nudge.

    Pinned here (rather than only in the detector's defensive guard)
    so the contract surfaces as a clear test failure with a named
    fix-shape rather than a quiet skipped bullet.
    """
    expected_today = {
        "consecutive_freezes",
        "per_task_token_cap_exceeded",
        "window_token_cap_exceeded",
        "task_error",
        "validator_judge_noisy",
    }
    assert expected_today.issubset(_PAUSE_REASON_ACK_VERB.keys()), (
        f"Missing ack-verb mapping for: "
        f"{expected_today - _PAUSE_REASON_ACK_VERB.keys()}"
    )

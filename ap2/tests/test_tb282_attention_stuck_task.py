"""TB-282: regression-pin for the proactive attention-raised push
surface + stuck-Active-task detector.

Closes goal.md focus-1's Done-when bullet on shallow monitoring
("Attention-needing conditions ... are surfaced proactively in
operator-legible terms, distinct from routine progress updates").

Pre-TB-282 there was no `ap2/attention.py` module, no
`attention_raised` event type in `events.py`'s registry, no
`## Attention needed` section in `STATUS_REPORT_PROMPT`, and the
periodic status-report Mattermost cron post was the ONLY push surface;
attention-needing conditions landed buried inside its routine
progress bullets, where a stuck Active task at minute 5 of a
cron-interval window waited up to the next tick to surface.

This module pins eight arcs (briefing scope item 6):

  (1) Detector fires at threshold+1s for an Active task whose most
      recent `task_start` is older than `AP2_TASK_STUCK_THRESHOLD_S`.
  (2) Detector misses a fresh task at threshold-1s — the seam between
      "long but healthy" and "stuck enough to surface".
  (3) Detector misses a task with an intervening terminal event
      (`task_complete` / `verification_failed` / etc.) — the closed-
      run guard.
  (4) Debounce: per-(attention_type, key) suppression of re-fires
      within `AP2_ATTENTION_DEBOUNCE_S`. A still-stuck task that
      surfaced 1h ago should NOT re-fire on the next tick at the
      default 6h debounce.
  (5) `attention_raised` event emitted with the documented payload
      shape (`attention_type`, `key`, `summary`, plus the detector's
      extras blob inlined).
  (6) `render_attention_section` produces the documented bullet shape
      on synthetic input, AND returns "" on the empty-conditions
      case (omit-on-empty parity with the sibling digest helpers).
  (7) Skip-gate treats `attention_raised` as interesting (parallel
      to TB-244 / TB-245's interesting-types tests).
  (8) Env-knob default + override + invalid-value fallback for both
      `AP2_TASK_STUCK_THRESHOLD_S` and `AP2_ATTENTION_DEBOUNCE_S`.
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
    DEFAULT_ATTENTION_DEBOUNCE_S,
    DEFAULT_TASK_STUCK_THRESHOLD_S,
    _attention_debounce_s,
    _task_stuck_threshold_s,
    detect_attention_conditions,
    find_last_attention_fire,
    should_suppress,
)
from ap2.board import Board
from ap2.config import Config
from ap2.init import init_project
from ap2.status_report import (
    _ATTENTION_NEEDED_HEADING,
    _STATUS_REPORT_AUTOMATION_INTERESTING_TYPES,
    _status_report_should_skip,
    render_attention_section,
    STATUS_REPORT_PROMPT,
)


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch) -> Config:
    """Clean project scaffold with both TB-282 env knobs unset so the
    defaults are the contract under test."""
    monkeypatch.delenv("AP2_TASK_STUCK_THRESHOLD_S", raising=False)
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

    Mirrors the helper TB-245's test module defined for the same need
    — `events.append` always stamps `now()`; tests that need an event
    "in the past" rewrite the line afterward.
    """
    lines = cfg.events_file.read_text().splitlines()
    if not lines:
        return
    last = _json.loads(lines[-1])
    last["ts"] = ts
    lines[-1] = _json.dumps(last)
    cfg.events_file.write_text("\n".join(lines) + "\n")


def _seed_active_task(cfg: Config, task_id: str, title: str) -> None:
    """Move a synthetic task into the Active section of TASKS.md.

    The detector reads the Active section as the source-of-truth for
    "currently dispatched" — a task that finished between ticks but
    hasn't been moved yet is NOT stuck. We use the Board API directly
    rather than running a real dispatch to keep the test hermetic.
    """
    board = Board.load(cfg.tasks_file)
    board.add("Active", task_id=task_id, title=title)
    board.save()


# ===========================================================================
# Arc 1+2: detector threshold seam (fires at threshold+1s, misses at
# threshold-1s).
# ===========================================================================


def test_detector_fires_for_active_task_past_threshold(cfg: Config):
    """Active task with `task_start` at threshold+1s in the past →
    detector returns one `AttentionCondition` of type `task_stuck`
    keyed `task_stuck:TB-300`. This is the load-bearing happy path:
    a 4h-stuck task surfaces on the next tick.
    """
    _seed_active_task(cfg, "TB-300", "Stuck task example")
    now = _dt.datetime(2026, 5, 23, 12, 0, 0, tzinfo=_dt.timezone.utc)
    start_ts = _ts_seconds_ago(
        now, seconds_ago=DEFAULT_TASK_STUCK_THRESHOLD_S + 1,
    )
    events.append(cfg.events_file, "task_start", task="TB-300", title="x")
    _rewrite_last_event_ts(cfg, start_ts)

    conditions = detect_attention_conditions(cfg, now=now)

    assert len(conditions) == 1, conditions
    cond = conditions[0]
    assert cond.type == "task_stuck"
    assert cond.key == "task_stuck:TB-300"
    assert "TB-300" in cond.summary
    assert cond.extras["task"] == "TB-300"
    assert cond.extras["title"] == "Stuck task example"
    assert cond.extras["age_s"] >= DEFAULT_TASK_STUCK_THRESHOLD_S
    assert cond.extras["start_ts"] == start_ts
    assert cond.extras["threshold_s"] == DEFAULT_TASK_STUCK_THRESHOLD_S


def test_detector_misses_active_task_below_threshold(cfg: Config):
    """Active task with `task_start` at threshold-1s → detector returns
    `[]`. Pins the boundary so a refactor that flips the comparator
    surfaces here (the cost is silent: a flipped comparator turns the
    detector into a permanent false-positive for every Active task).
    """
    _seed_active_task(cfg, "TB-301", "Fresh task")
    now = _dt.datetime(2026, 5, 23, 12, 0, 0, tzinfo=_dt.timezone.utc)
    start_ts = _ts_seconds_ago(
        now, seconds_ago=DEFAULT_TASK_STUCK_THRESHOLD_S - 1,
    )
    events.append(cfg.events_file, "task_start", task="TB-301", title="x")
    _rewrite_last_event_ts(cfg, start_ts)

    conditions = detect_attention_conditions(cfg, now=now)
    assert conditions == [], conditions


# ===========================================================================
# Arc 3: terminal-event closes the run — detector must miss even when
# `task_start` is well past threshold.
# ===========================================================================


def test_detector_misses_when_intervening_terminal_event(cfg: Config):
    """Even with `task_start` at 10h ago (well past 4h threshold), an
    intervening `task_complete` event closes the run — the detector
    must NOT fire. Pins the closed-run guard (board section drifts
    behind events for a fraction of a tick when a run terminates;
    surfacing during that window would be a false-positive).
    """
    _seed_active_task(cfg, "TB-302", "Recently-completed task")
    now = _dt.datetime(2026, 5, 23, 12, 0, 0, tzinfo=_dt.timezone.utc)
    start_ts = _ts_seconds_ago(now, seconds_ago=36000)  # 10h ago
    events.append(cfg.events_file, "task_start", task="TB-302", title="x")
    _rewrite_last_event_ts(cfg, start_ts)
    # Intervening terminal event after the task_start.
    complete_ts = _ts_seconds_ago(now, seconds_ago=60)
    events.append(
        cfg.events_file, "task_complete",
        task="TB-302", status="complete", commit="abc1234",
    )
    _rewrite_last_event_ts(cfg, complete_ts)

    conditions = detect_attention_conditions(cfg, now=now)
    assert conditions == [], conditions


def test_detector_misses_when_intervening_verification_failed(cfg: Config):
    """Symmetric to the `task_complete` guard above for the other
    terminal event types (`verification_failed`, `task_failed`,
    `retry_exhausted`). Pin them collectively here so a future
    refactor that drops one from the closed-run set surfaces."""
    _seed_active_task(cfg, "TB-303", "Verification-failed task")
    now = _dt.datetime(2026, 5, 23, 12, 0, 0, tzinfo=_dt.timezone.utc)
    start_ts = _ts_seconds_ago(now, seconds_ago=20000)
    events.append(cfg.events_file, "task_start", task="TB-303", title="x")
    _rewrite_last_event_ts(cfg, start_ts)
    vfail_ts = _ts_seconds_ago(now, seconds_ago=30)
    events.append(
        cfg.events_file, "verification_failed",
        task="TB-303", kind="per_task", overall="fail",
    )
    _rewrite_last_event_ts(cfg, vfail_ts)

    conditions = detect_attention_conditions(cfg, now=now)
    assert conditions == [], conditions


def test_detector_skips_task_not_in_active_section(cfg: Config):
    """A stale `task_start` event for a task that's now in Complete
    (the run finished and was moved) MUST NOT surface as stuck —
    the detector only considers tasks currently in the Active
    section. Pin the Active-only filter."""
    board = Board.load(cfg.tasks_file)
    board.add("Complete", task_id="TB-304", title="Done task")
    board.save()
    now = _dt.datetime(2026, 5, 23, 12, 0, 0, tzinfo=_dt.timezone.utc)
    start_ts = _ts_seconds_ago(now, seconds_ago=20000)
    events.append(cfg.events_file, "task_start", task="TB-304", title="x")
    _rewrite_last_event_ts(cfg, start_ts)

    conditions = detect_attention_conditions(cfg, now=now)
    assert conditions == [], conditions


def test_detector_handles_multiple_stuck_tasks(cfg: Config):
    """Two distinct stuck tasks → two `AttentionCondition` records
    with distinct `key`s. Per-(type, key) debounce is the briefing's
    load-bearing contract — pin the multi-candidate shape so the
    next layer's debounce check doesn't merge them."""
    _seed_active_task(cfg, "TB-310", "Stuck A")
    _seed_active_task(cfg, "TB-311", "Stuck B")
    now = _dt.datetime(2026, 5, 23, 12, 0, 0, tzinfo=_dt.timezone.utc)
    start_ts_a = _ts_seconds_ago(now, seconds_ago=20000)
    events.append(cfg.events_file, "task_start", task="TB-310", title="x")
    _rewrite_last_event_ts(cfg, start_ts_a)
    start_ts_b = _ts_seconds_ago(now, seconds_ago=25000)
    events.append(cfg.events_file, "task_start", task="TB-311", title="x")
    _rewrite_last_event_ts(cfg, start_ts_b)

    conditions = detect_attention_conditions(cfg, now=now)
    keys = sorted(c.key for c in conditions)
    assert keys == ["task_stuck:TB-310", "task_stuck:TB-311"], keys


# ===========================================================================
# Arc 4: debounce — per-(attention_type, key) suppression.
# ===========================================================================


def test_debounce_suppresses_refire_within_window(cfg: Config):
    """A still-stuck task that already surfaced 1h ago at the default
    6h debounce → `should_suppress` returns True. Pin the suppression
    so a re-fire every 30s tick doesn't drown the channel."""
    now = _dt.datetime(2026, 5, 23, 12, 0, 0, tzinfo=_dt.timezone.utc)
    # Stash a prior `attention_raised` event 1h ago.
    events.append(
        cfg.events_file, "attention_raised",
        attention_type="task_stuck", key="task_stuck:TB-400",
        summary="prior", task="TB-400",
    )
    prior_ts = _ts_seconds_ago(now, seconds_ago=3600)
    _rewrite_last_event_ts(cfg, prior_ts)

    cond = AttentionCondition(
        type="task_stuck", key="task_stuck:TB-400",
        summary="fresh", ts=prior_ts, extras={"task": "TB-400"},
    )
    tail = events.tail(cfg.events_file, 100)
    assert should_suppress(cond, tail=tail, now=now, cfg=cfg) is True


def test_debounce_allows_refire_past_window(cfg: Config):
    """A prior `attention_raised` event 7h ago (past the 6h default
    debounce) → `should_suppress` returns False. Pin the boundary so
    a still-stuck task re-surfaces once per operator workday."""
    now = _dt.datetime(2026, 5, 23, 12, 0, 0, tzinfo=_dt.timezone.utc)
    events.append(
        cfg.events_file, "attention_raised",
        attention_type="task_stuck", key="task_stuck:TB-401",
        summary="prior", task="TB-401",
    )
    prior_ts = _ts_seconds_ago(
        now, seconds_ago=DEFAULT_ATTENTION_DEBOUNCE_S + 3600,
    )
    _rewrite_last_event_ts(cfg, prior_ts)

    cond = AttentionCondition(
        type="task_stuck", key="task_stuck:TB-401",
        summary="fresh", ts=prior_ts, extras={"task": "TB-401"},
    )
    tail = events.tail(cfg.events_file, 100)
    assert should_suppress(cond, tail=tail, now=now, cfg=cfg) is False


def test_debounce_per_key_not_per_type(cfg: Config):
    """A prior `attention_raised` for TB-500 must NOT suppress a
    fresh condition for TB-501 — the debounce keys on (type, key)
    not just type. Briefing's load-bearing contract: "a second stuck
    task doesn't get suppressed because a first one fired recently".
    """
    now = _dt.datetime(2026, 5, 23, 12, 0, 0, tzinfo=_dt.timezone.utc)
    events.append(
        cfg.events_file, "attention_raised",
        attention_type="task_stuck", key="task_stuck:TB-500",
        summary="prior", task="TB-500",
    )
    prior_ts = _ts_seconds_ago(now, seconds_ago=60)
    _rewrite_last_event_ts(cfg, prior_ts)

    cond = AttentionCondition(
        type="task_stuck", key="task_stuck:TB-501",
        summary="fresh", ts=prior_ts, extras={"task": "TB-501"},
    )
    tail = events.tail(cfg.events_file, 100)
    assert should_suppress(cond, tail=tail, now=now, cfg=cfg) is False, (
        "different keys must NOT cross-suppress"
    )


def test_find_last_attention_fire_returns_none_for_no_match(cfg: Config):
    """`find_last_attention_fire` returns None when no matching event
    is in the tail. Pin the None-return path so callers' early-out
    semantics stay correct."""
    events.append(
        cfg.events_file, "attention_raised",
        attention_type="task_stuck", key="task_stuck:TB-600",
        summary="x", task="TB-600",
    )
    tail = events.tail(cfg.events_file, 100)
    assert find_last_attention_fire(
        tail, type_="task_stuck", key="task_stuck:TB-601",
    ) is None
    assert find_last_attention_fire(
        tail, type_="some_other_detector", key="task_stuck:TB-600",
    ) is None


# ===========================================================================
# Arc 5: end-to-end daemon wire-up emits `attention_raised` with the
# documented payload.
# ===========================================================================


def test_daemon_wire_up_emits_attention_raised_event(cfg: Config):
    """The daemon's `_maybe_emit_attention_events` helper emits one
    `attention_raised` event per fresh condition with the documented
    payload shape: `attention_type` + `key` + `summary` + the
    detector's extras blob inlined (`task`, `title`, `age_s`,
    `start_ts`, `threshold_s` for `task_stuck`).

    Pin the payload shape so downstream consumers (ideation events
    block, status-report skip-gate, `ap2 logs`, `/events` web view)
    can rely on the field names.
    """
    from ap2.daemon import _maybe_emit_attention_events

    _seed_active_task(cfg, "TB-700", "Stuck end-to-end")
    # Need to force a past `task_start`.
    events.append(cfg.events_file, "task_start", task="TB-700", title="x")
    past_ts = (
        _dt.datetime.now(_dt.timezone.utc)
        - _dt.timedelta(seconds=DEFAULT_TASK_STUCK_THRESHOLD_S + 60)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    _rewrite_last_event_ts(cfg, past_ts)

    _maybe_emit_attention_events(cfg)

    tail = events.tail(cfg.events_file, 100)
    fires = [e for e in tail if e.get("type") == "attention_raised"]
    assert len(fires) == 1, fires
    fire = fires[0]
    assert fire["attention_type"] == "task_stuck"
    assert fire["key"] == "task_stuck:TB-700"
    assert "TB-700" in fire["summary"]
    # Extras blob inlined into the payload.
    assert fire["task"] == "TB-700"
    assert fire["title"] == "Stuck end-to-end"
    assert "age_s" in fire
    assert fire["start_ts"] == past_ts
    assert fire["threshold_s"] == DEFAULT_TASK_STUCK_THRESHOLD_S


def test_daemon_wire_up_respects_debounce(cfg: Config):
    """The daemon wire-up does NOT emit a fresh `attention_raised`
    when a prior matching fire is inside the debounce window. Pin
    the debounce-vs-emit contract at the wire-up level (separate
    from the `should_suppress` unit test above)."""
    from ap2.daemon import _maybe_emit_attention_events

    _seed_active_task(cfg, "TB-701", "Re-surfaced task")
    events.append(cfg.events_file, "task_start", task="TB-701", title="x")
    past_ts = (
        _dt.datetime.now(_dt.timezone.utc)
        - _dt.timedelta(seconds=DEFAULT_TASK_STUCK_THRESHOLD_S + 60)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    _rewrite_last_event_ts(cfg, past_ts)
    # Prior fire 1h ago — well inside default 6h debounce.
    events.append(
        cfg.events_file, "attention_raised",
        attention_type="task_stuck", key="task_stuck:TB-701",
        summary="prior", task="TB-701",
    )
    prior_fire_ts = (
        _dt.datetime.now(_dt.timezone.utc)
        - _dt.timedelta(seconds=3600)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    _rewrite_last_event_ts(cfg, prior_fire_ts)

    _maybe_emit_attention_events(cfg)

    tail = events.tail(cfg.events_file, 100)
    fires = [e for e in tail if e.get("type") == "attention_raised"]
    # Still only the seeded prior fire — no new emission.
    assert len(fires) == 1, fires


# ===========================================================================
# Arc 6: renderer bullet shape + omit-on-empty.
# ===========================================================================


def test_render_attention_section_bullet_shape(cfg: Config):
    """The renderer emits one bullet per active condition under the
    `## Attention needed` heading, shaped
    `- ⚠ **TB-N** — <title> Active for <h>h since <ts>`. Pin the
    structural shape so operator muscle-memory holds across releases.
    """
    _seed_active_task(cfg, "TB-800", "Detector sample")
    events.append(cfg.events_file, "task_start", task="TB-800", title="x")
    past_ts = (
        _dt.datetime.now(_dt.timezone.utc)
        - _dt.timedelta(seconds=DEFAULT_TASK_STUCK_THRESHOLD_S + 1800)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    _rewrite_last_event_ts(cfg, past_ts)

    section = render_attention_section(cfg, since_event_idx=-1)
    assert section.startswith("## Attention needed"), section
    # Bullet shape pins.
    assert "⚠" in section
    assert "**TB-800**" in section
    assert "Detector sample" in section
    assert "Active for" in section
    assert past_ts in section


def test_render_attention_section_omits_when_empty(cfg: Config):
    """No conditions active → renderer returns "". Pins the
    omit-on-empty rule that lets the pre-TB-282 digest baseline stay
    byte-identical on healthy projects (parallel to TB-280's
    `test_section_absent_when_window_has_zero_terminal_task_events`).
    """
    # No Active tasks, no task_start events.
    section = render_attention_section(cfg, since_event_idx=-1)
    assert section == "", section


def test_render_attention_section_uses_summary_for_unknown_detector(
    cfg: Config, monkeypatch,
):
    """Future detectors with malformed extras → renderer falls back
    to the pre-rendered `cond.summary` verbatim. Pin the generic
    fallback so a new detector that doesn't ship structured extras
    still produces a usable bullet.
    """
    def _fake_detect(cfg, *, tail=None, now=None):  # noqa: ARG001
        return [AttentionCondition(
            type="future_detector",
            key="future_detector:something",
            summary="custom phrasing the renderer should forward",
            ts="2026-05-23T12:00:00Z",
            extras={},
        )]

    monkeypatch.setattr(attention, "detect_attention_conditions", _fake_detect)
    section = render_attention_section(cfg, since_event_idx=-1)
    assert "⚠ custom phrasing the renderer should forward" in section


# ===========================================================================
# Arc 7: skip-gate treats `attention_raised` as interesting.
# ===========================================================================


def test_attention_raised_in_interesting_types_set():
    """Source-level pin: TB-282's `attention_raised` lands in
    `_STATUS_REPORT_AUTOMATION_INTERESTING_TYPES` so a fresh fire
    un-skips the dedup/idle gate. Parallel to TB-244 / TB-245's
    interesting-types tests."""
    assert (
        "attention_raised"
        in _STATUS_REPORT_AUTOMATION_INTERESTING_TYPES
    ), _STATUS_REPORT_AUTOMATION_INTERESTING_TYPES
    # Regression-pin against an edit that overwrote the frozenset
    # instead of extending it: prior allowlist members survive.
    assert (
        "validator_judge_fail"
        in _STATUS_REPORT_AUTOMATION_INTERESTING_TYPES
    )
    # TB-342: `focus_advanced` retired with the multi-focus rotation
    # pointer walk; only `roadmap_complete` remains as the axis-4
    # interesting event.
    assert (
        "roadmap_complete"
        in _STATUS_REPORT_AUTOMATION_INTERESTING_TYPES
    )


def test_should_skip_false_when_attention_raised_in_window(cfg: Config):
    """A lone `attention_raised` event past the last
    `cron_complete name=status-report` must keep the cron from
    skipping. Operator's primary push channel must carry the
    attention condition on the next post — same contract TB-245
    pins for the validator-judge events.
    """
    events.append(cfg.events_file, "cron_complete", job="status-report")
    events.append(
        cfg.events_file, "attention_raised",
        attention_type="task_stuck", key="task_stuck:TB-900",
        summary="TB-900 Active for 5.0h since 2026-05-23T07:00:00Z",
        task="TB-900",
    )

    assert _status_report_should_skip(cfg) is False, (
        "attention_raised in the window must keep the report from "
        "skipping — operator must see the condition on the next post"
    )


def test_attention_raised_in_ideation_relevant_types():
    """Source-level pin: TB-282's `attention_raised` lands in
    `IDEATION_RELEVANT_EVENT_TYPES` so ideation sees fresh attention
    events in its prompt tail and can reason against them next
    cycle. Parallel to the validator-judge / auto-approve allowlist
    extensions documented in `ap2/ideation.py`'s
    `IDEATION_RELEVANT_EVENT_TYPES` block."""
    from ap2.ideation import IDEATION_RELEVANT_EVENT_TYPES

    assert "attention_raised" in IDEATION_RELEVANT_EVENT_TYPES


def test_status_report_prompt_references_attention_section():
    """The prompt body documents the `## Attention needed` section
    the daemon pre-renders + the verbatim-forwarding contract +
    the FIRST-position contract. Pin the literal so a paraphrase
    that drops any of the three trips here.
    """
    body = STATUS_REPORT_PROMPT
    assert "Attention needed" in body
    assert "TB-282" in body
    assert "VERBATIM" in body or "verbatim" in body.lower()
    # First-position-load-bearing contract.
    assert "BEFORE your body bullets" in body or "FIRST" in body, body


# ===========================================================================
# Arc 8: env-knob default + override + invalid-value fallback.
# ===========================================================================


def test_task_stuck_threshold_default(cfg: Config, monkeypatch):
    """No env knob set → `_task_stuck_threshold_s` returns
    `DEFAULT_TASK_STUCK_THRESHOLD_S` (14400 / 4h). Pin the default
    so a refactor that silently shifts the floor blows here.

    TB-328: the helper now takes a `cfg` argument; the resolved-config
    layer reads sectioned-env > flat-env > TOML > default at call time.
    """
    monkeypatch.delenv("AP2_TASK_STUCK_THRESHOLD_S", raising=False)
    assert _task_stuck_threshold_s(cfg) == DEFAULT_TASK_STUCK_THRESHOLD_S
    assert DEFAULT_TASK_STUCK_THRESHOLD_S == 14400


def test_task_stuck_threshold_env_override(cfg: Config, monkeypatch):
    """`AP2_TASK_STUCK_THRESHOLD_S=7200` → `_task_stuck_threshold_s`
    returns 7200 (operator tightens the floor to 2h). TB-328: the
    resolved-config layer's call-time env-first precedence preserves
    the pre-migration lazy-read pattern so an env-reload propagates
    without re-threading state."""
    monkeypatch.setenv("AP2_TASK_STUCK_THRESHOLD_S", "7200")
    assert _task_stuck_threshold_s(cfg) == 7200


def test_task_stuck_threshold_invalid_falls_back(cfg: Config, monkeypatch):
    """Garbage value (`AP2_TASK_STUCK_THRESHOLD_S=not-a-number`) →
    falls back to the default. Pin the safe-default rule so an
    operator typo doesn't disable the detector silently."""
    monkeypatch.setenv("AP2_TASK_STUCK_THRESHOLD_S", "not-a-number")
    assert _task_stuck_threshold_s(cfg) == DEFAULT_TASK_STUCK_THRESHOLD_S
    monkeypatch.setenv("AP2_TASK_STUCK_THRESHOLD_S", "0")
    assert _task_stuck_threshold_s(cfg) == DEFAULT_TASK_STUCK_THRESHOLD_S
    monkeypatch.setenv("AP2_TASK_STUCK_THRESHOLD_S", "-1")
    assert _task_stuck_threshold_s(cfg) == DEFAULT_TASK_STUCK_THRESHOLD_S


def test_attention_debounce_default(cfg: Config, monkeypatch):
    """No env knob set → `_attention_debounce_s` returns
    `DEFAULT_ATTENTION_DEBOUNCE_S` (21600 / 6h). Pin the default."""
    monkeypatch.delenv("AP2_ATTENTION_DEBOUNCE_S", raising=False)
    assert _attention_debounce_s(cfg) == DEFAULT_ATTENTION_DEBOUNCE_S
    assert DEFAULT_ATTENTION_DEBOUNCE_S == 21600


def test_attention_debounce_env_override(cfg: Config, monkeypatch):
    """`AP2_ATTENTION_DEBOUNCE_S=3600` → `_attention_debounce_s`
    returns 3600 (operator tightens to 1h)."""
    monkeypatch.setenv("AP2_ATTENTION_DEBOUNCE_S", "3600")
    assert _attention_debounce_s(cfg) == 3600


def test_attention_debounce_invalid_falls_back(cfg: Config, monkeypatch):
    """Garbage value → falls back to the default."""
    monkeypatch.setenv("AP2_ATTENTION_DEBOUNCE_S", "garbage")
    assert _attention_debounce_s(cfg) == DEFAULT_ATTENTION_DEBOUNCE_S
    monkeypatch.setenv("AP2_ATTENTION_DEBOUNCE_S", "0")
    assert _attention_debounce_s(cfg) == DEFAULT_ATTENTION_DEBOUNCE_S


def test_attention_env_knobs_are_hot_reloadable():
    """Both TB-282 env knobs land in `HOT_RELOADABLE_KNOBS` so a
    threshold/debounce change takes effect on the next tick without
    a daemon restart. Mirrors TB-280's
    `test_env_reload_lists_project_name_as_hot_reloadable`."""
    from ap2.env_reload import HOT_RELOADABLE_KNOBS

    assert "AP2_TASK_STUCK_THRESHOLD_S" in HOT_RELOADABLE_KNOBS
    assert "AP2_ATTENTION_DEBOUNCE_S" in HOT_RELOADABLE_KNOBS


# ===========================================================================
# Source-anchor pins mirroring the briefing's Verification greps.
# ===========================================================================


def test_briefing_verification_greps_match():
    """Mirror the briefing's `## Verification` greps in test form so
    a refactor that violates the structural pins surfaces here as a
    clean test failure (parallel to TB-280's pin)."""
    repo_root = Path(__file__).resolve().parent.parent
    # TB-343: the attention body moved to the sibling impl.py.
    attention_src = (repo_root / "components" / "attention" / "impl.py").read_text()
    events_src = (repo_root / "events.py").read_text()
    config_src = (repo_root / "config.py").read_text()
    status_report_src = (repo_root / "status_report.py").read_text()

    # `grep -Eq "detect_attention_conditions|attention_raised" ap2/attention.py`
    assert "detect_attention_conditions" in attention_src
    assert "attention_raised" in attention_src

    # `grep -q "attention_raised" ap2/events.py`
    assert "attention_raised" in events_src

    # `grep -Eq "AP2_TASK_STUCK_THRESHOLD_S|AP2_ATTENTION_DEBOUNCE_S" ap2/config.py`
    assert (
        "AP2_TASK_STUCK_THRESHOLD_S" in config_src
        or "AP2_ATTENTION_DEBOUNCE_S" in config_src
    ), config_src[:1000]

    # `grep -Eq "render_attention_section|Attention needed" ap2/status_report.py`
    assert (
        "render_attention_section" in status_report_src
        or "Attention needed" in status_report_src
    )

    # Both heading and renderer wired (positive both-checks).
    assert "render_attention_section" in status_report_src
    assert "Attention needed" in status_report_src
    assert _ATTENTION_NEEDED_HEADING == "## Attention needed"

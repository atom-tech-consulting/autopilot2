"""TB-287: regression-pin for the proactive `task_frozen` attention
detector (TB-282 follow-up closing Progress signal #3 "frozen tasks"
leg).

Pre-TB-287 the only attention-detector seeded in `ap2/attention.py`
was `task_stuck`; Frozen tasks surfaced only as the `3F` aggregate
count on `ap2 status` / status-report headline. A walk-away operator
returning after a day where a new task froze saw the count tick up
but got no proactive `ap2 unfreeze` nudge — exactly the "operator
must poll each project to find problems" failure mode goal.md
L210-213 names.

This module pins five arcs (briefing scope item 3):

  (1) Happy path: Frozen task within the recency window AND no
      intervening `task_unfrozen` / `task_deleted` → one
      `AttentionCondition` of type `task_frozen` keyed
      `task_frozen:TB-N` with the documented operator-legible summary.
  (2) Dormancy: Frozen task whose freeze-entry event is older than
      `AP2_TASK_FROZEN_RECENCY_S` → no fire (operator's seen it).
  (3) Intervening operator unfreeze: a `task_unfrozen` event LATER
      than the `retry_exhausted` event → no fire (operator acted;
      board section may not have drained yet).
  (4) Per-key dedup: two distinct Frozen tasks both surface — debounce
      is per `task_frozen:<task_id>`, not per detector kind. Pin the
      multi-candidate shape so the daemon's debounce sees independent
      keys.
  (5) Env-knob override: `AP2_TASK_FROZEN_RECENCY_S=3600` shortens the
      window — a 2h-old freeze stops surfacing when the operator
      tightens the floor.

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
    _detect_task_frozen,
    _task_frozen_recency_s,
    detect_attention_conditions,
)
from ap2.board import Board
from ap2.config import (
    Config,
    DEFAULT_TASK_FROZEN_RECENCY_S,
)
from ap2.init import init_project


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch) -> Config:
    """Clean project scaffold with the TB-287 env knob unset so the
    default is the contract under test. Also unsets the TB-282
    siblings so the `task_stuck` detector doesn't false-fire from
    test-seeded `task_start` events that may co-occur in some arcs.
    """
    monkeypatch.delenv("AP2_COMPONENTS_ATTENTION_TASK_FROZEN_RECENCY_S", raising=False)
    monkeypatch.delenv("AP2_COMPONENTS_ATTENTION_TASK_STUCK_THRESHOLD_S", raising=False)
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

    Mirrors the helper TB-282's test module defined for the same need
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


def _seed_frozen_task(cfg: Config, task_id: str, title: str) -> None:
    """Move a synthetic task into the Frozen section of TASKS.md.

    The detector reads the Frozen section as the source-of-truth for
    "currently parked"; a task that the operator just unfroze (queue
    ack pending drain) is NOT a candidate. We use the Board API
    directly rather than running a real dispatch-failure cycle to keep
    the test hermetic.
    """
    board = Board.load(cfg.tasks_file)
    board.add("Frozen", task_id=task_id, title=title)
    board.save()


def _emit_freeze_entry(
    cfg: Config,
    task_id: str,
    *,
    seconds_ago: float,
    now: _dt.datetime,
    event_type: str = "retry_exhausted",
) -> str:
    """Emit a freeze-entry event for `task_id` and rewrite its `ts` to
    `seconds_ago` before `now`. Returns the rewritten timestamp.
    """
    events.append(
        cfg.events_file, event_type,
        task=task_id, attempts=3, last_status="blocked",
    )
    ts = _ts_seconds_ago(now, seconds_ago=seconds_ago)
    _rewrite_last_event_ts(cfg, ts)
    return ts


# ===========================================================================
# Arc 1: happy path — Frozen task within the recency window fires.
# ===========================================================================


def test_detector_fires_for_frozen_task_within_recency(cfg: Config):
    """Frozen task with `retry_exhausted` 2h ago (well inside the
    default 24h recency window) → detector returns one
    `AttentionCondition` of type `task_frozen` keyed
    `task_frozen:TB-300`. This is the load-bearing happy path: a
    fresh freeze surfaces on the next tick.
    """
    _seed_frozen_task(cfg, "TB-300", "Freshly frozen task")
    now = _dt.datetime(2026, 5, 23, 12, 0, 0, tzinfo=_dt.timezone.utc)
    freeze_ts = _emit_freeze_entry(
        cfg, "TB-300", seconds_ago=7200, now=now,
    )

    conditions = detect_attention_conditions(cfg, now=now)

    # Filter to the task_frozen detector to keep this test focused
    # — the cfg fixture's clean env shouldn't surface any task_stuck
    # conditions, but the filter pins intent.
    frozen_conds = [c for c in conditions if c.type == "task_frozen"]
    assert len(frozen_conds) == 1, conditions
    cond = frozen_conds[0]
    assert cond.type == "task_frozen"
    assert cond.key == "task_frozen:TB-300"
    assert "TB-300" in cond.summary
    assert "ap2 unfreeze TB-300" in cond.summary
    assert "Frozen for" in cond.summary
    assert cond.extras["task"] == "TB-300"
    assert cond.extras["title"] == "Freshly frozen task"
    assert cond.extras["age_s"] >= 7200 - 5  # 2h ± clock noise
    assert cond.extras["freeze_ts"] == freeze_ts
    assert cond.extras["recency_s"] == DEFAULT_TASK_FROZEN_RECENCY_S


def test_detector_fires_for_task_failed_freeze_entry(cfg: Config):
    """Symmetric to the `retry_exhausted` arc above for the other
    freeze-entry event type — `task_failed`. Pin both event types
    collectively so a future refactor that drops one from
    `_FREEZE_ENTRY_EVENT_TYPES` surfaces.
    """
    _seed_frozen_task(cfg, "TB-301", "Manually failed task")
    now = _dt.datetime(2026, 5, 23, 12, 0, 0, tzinfo=_dt.timezone.utc)
    _emit_freeze_entry(
        cfg, "TB-301", seconds_ago=3600, now=now,
        event_type="task_failed",
    )

    conditions = detect_attention_conditions(cfg, now=now)
    frozen_conds = [c for c in conditions if c.type == "task_frozen"]
    assert len(frozen_conds) == 1, conditions
    assert frozen_conds[0].key == "task_frozen:TB-301"


# ===========================================================================
# Arc 2: dormancy — Frozen task older than the recency window stays quiet.
# ===========================================================================


def test_detector_misses_when_freeze_is_older_than_recency(cfg: Config):
    """Frozen task whose `retry_exhausted` is 25h ago (past the
    default 24h recency window) → detector returns `[]`. Pin the
    boundary so a refactor that flips the comparator surfaces here
    (the cost is silent: a flipped comparator would turn the
    detector into a permanent false-positive for every Frozen task).
    """
    _seed_frozen_task(cfg, "TB-310", "Long-stale frozen task")
    now = _dt.datetime(2026, 5, 23, 12, 0, 0, tzinfo=_dt.timezone.utc)
    _emit_freeze_entry(
        cfg, "TB-310",
        seconds_ago=DEFAULT_TASK_FROZEN_RECENCY_S + 3600,
        now=now,
    )

    conditions = detect_attention_conditions(cfg, now=now)
    frozen_conds = [c for c in conditions if c.type == "task_frozen"]
    assert frozen_conds == [], conditions


def test_detector_misses_when_no_freeze_entry_event_at_all(cfg: Config):
    """A Frozen-section task with NO `retry_exhausted` /
    `task_failed` event in the tail (test-only edge — a real Frozen
    row always has a freeze-entry event somewhere upstream) → no
    fire. Pin the "no candidate" guard so a missing event doesn't
    surface as a phantom freeze.
    """
    _seed_frozen_task(cfg, "TB-311", "Frozen with no freeze event")
    now = _dt.datetime(2026, 5, 23, 12, 0, 0, tzinfo=_dt.timezone.utc)
    # Emit an unrelated event so the tail isn't empty.
    events.append(cfg.events_file, "tick", note="unrelated")

    conditions = detect_attention_conditions(cfg, now=now)
    frozen_conds = [c for c in conditions if c.type == "task_frozen"]
    assert frozen_conds == [], conditions


# ===========================================================================
# Arc 3: intervening operator unfreeze / delete closes the window.
# ===========================================================================


def test_detector_misses_when_intervening_task_unfrozen(cfg: Config):
    """Even with a `retry_exhausted` 1h ago (well within the 24h
    recency window), an intervening `task_unfrozen` event closes the
    window — the operator already acted. Detector must NOT fire.
    Pin the operator-acted guard (board section drifts behind the
    operator-queue drain for a fraction of a tick; surfacing during
    that window would be a false-positive).
    """
    _seed_frozen_task(cfg, "TB-320", "Unfrozen-but-not-drained yet")
    now = _dt.datetime(2026, 5, 23, 12, 0, 0, tzinfo=_dt.timezone.utc)
    _emit_freeze_entry(cfg, "TB-320", seconds_ago=3600, now=now)
    # Intervening operator unfreeze after the freeze-entry event.
    events.append(cfg.events_file, "task_unfrozen", task="TB-320")
    unfrozen_ts = _ts_seconds_ago(now, seconds_ago=60)
    _rewrite_last_event_ts(cfg, unfrozen_ts)

    conditions = detect_attention_conditions(cfg, now=now)
    frozen_conds = [c for c in conditions if c.type == "task_frozen"]
    assert frozen_conds == [], conditions


def test_detector_misses_when_intervening_task_deleted(cfg: Config):
    """Symmetric to the `task_unfrozen` guard above for
    `task_deleted` (operator chose delete over unfreeze). Pin both
    operator-acted event types collectively so a refactor that drops
    one from `_FREEZE_RESOLVED_EVENT_TYPES` surfaces."""
    _seed_frozen_task(cfg, "TB-321", "Deleted-but-not-drained yet")
    now = _dt.datetime(2026, 5, 23, 12, 0, 0, tzinfo=_dt.timezone.utc)
    _emit_freeze_entry(cfg, "TB-321", seconds_ago=3600, now=now)
    events.append(cfg.events_file, "task_deleted", task="TB-321")
    deleted_ts = _ts_seconds_ago(now, seconds_ago=30)
    _rewrite_last_event_ts(cfg, deleted_ts)

    conditions = detect_attention_conditions(cfg, now=now)
    frozen_conds = [c for c in conditions if c.type == "task_frozen"]
    assert frozen_conds == [], conditions


def test_detector_skips_task_not_in_frozen_section(cfg: Config):
    """A `retry_exhausted` event for a task that's now in Backlog
    (the operator unfroze and the drain ran — board section is
    correct) MUST NOT surface as Frozen. The detector only
    considers tasks currently in the Frozen section. Pin the
    Frozen-only filter."""
    board = Board.load(cfg.tasks_file)
    board.add("Backlog", task_id="TB-322", title="Recovered task")
    board.save()
    now = _dt.datetime(2026, 5, 23, 12, 0, 0, tzinfo=_dt.timezone.utc)
    _emit_freeze_entry(cfg, "TB-322", seconds_ago=3600, now=now)

    conditions = detect_attention_conditions(cfg, now=now)
    frozen_conds = [c for c in conditions if c.type == "task_frozen"]
    assert frozen_conds == [], conditions


# ===========================================================================
# Arc 4: per-key dedup — two distinct frozen tasks both surface.
# ===========================================================================


def test_detector_handles_multiple_frozen_tasks(cfg: Config):
    """Two distinct Frozen tasks both within the recency window →
    two `AttentionCondition` records with distinct `key`s. Per-
    (type, key) debounce is the briefing's load-bearing contract —
    pin the multi-candidate shape so the daemon's debounce check
    doesn't merge them.
    """
    _seed_frozen_task(cfg, "TB-330", "Frozen A")
    _seed_frozen_task(cfg, "TB-331", "Frozen B")
    now = _dt.datetime(2026, 5, 23, 12, 0, 0, tzinfo=_dt.timezone.utc)
    _emit_freeze_entry(cfg, "TB-330", seconds_ago=3600, now=now)
    _emit_freeze_entry(cfg, "TB-331", seconds_ago=7200, now=now)

    conditions = detect_attention_conditions(cfg, now=now)
    frozen_keys = sorted(
        c.key for c in conditions if c.type == "task_frozen"
    )
    assert frozen_keys == [
        "task_frozen:TB-330", "task_frozen:TB-331",
    ], frozen_keys


# ===========================================================================
# Arc 5: env-knob override + invalid-value fallback + default contract.
# ===========================================================================


def test_task_frozen_recency_default(cfg: Config, monkeypatch):
    """No env knob set → `_task_frozen_recency_s` returns
    `DEFAULT_TASK_FROZEN_RECENCY_S` (86400 / 24h). Pin the default
    so a refactor that silently shifts the floor blows here.

    TB-328: the helper now takes a `cfg` argument; the resolved-config
    layer reads sectioned-env > flat-env > TOML > default at call time.
    """
    monkeypatch.delenv("AP2_COMPONENTS_ATTENTION_TASK_FROZEN_RECENCY_S", raising=False)
    assert _task_frozen_recency_s(cfg) == DEFAULT_TASK_FROZEN_RECENCY_S
    assert DEFAULT_TASK_FROZEN_RECENCY_S == 86400


def test_task_frozen_recency_env_override(cfg: Config, monkeypatch):
    """`AP2_TASK_FROZEN_RECENCY_S=3600` → recency floor tightens to
    1h. A 2h-old freeze that was a candidate under the default now
    drops out. Pin both the resolver result AND the end-to-end
    detector behavior under the override.
    """
    monkeypatch.setenv("AP2_COMPONENTS_ATTENTION_TASK_FROZEN_RECENCY_S", "3600")
    assert _task_frozen_recency_s(cfg) == 3600

    _seed_frozen_task(cfg, "TB-340", "Two-hour-stale freeze")
    now = _dt.datetime(2026, 5, 23, 12, 0, 0, tzinfo=_dt.timezone.utc)
    _emit_freeze_entry(cfg, "TB-340", seconds_ago=7200, now=now)

    conditions = detect_attention_conditions(cfg, now=now)
    frozen_conds = [c for c in conditions if c.type == "task_frozen"]
    assert frozen_conds == [], (
        "1h recency override should drop a 2h-old freeze candidate "
        f"(got {frozen_conds})"
    )


def test_task_frozen_recency_invalid_falls_back(cfg: Config, monkeypatch):
    """Garbage value → falls back to the default. Pin the safe-
    default rule so an operator typo doesn't disable the detector
    silently (parallel to TB-282's
    `test_task_stuck_threshold_invalid_falls_back`).
    """
    monkeypatch.setenv("AP2_COMPONENTS_ATTENTION_TASK_FROZEN_RECENCY_S", "not-a-number")
    assert _task_frozen_recency_s(cfg) == DEFAULT_TASK_FROZEN_RECENCY_S
    monkeypatch.setenv("AP2_COMPONENTS_ATTENTION_TASK_FROZEN_RECENCY_S", "0")
    assert _task_frozen_recency_s(cfg) == DEFAULT_TASK_FROZEN_RECENCY_S
    monkeypatch.setenv("AP2_COMPONENTS_ATTENTION_TASK_FROZEN_RECENCY_S", "-1")
    assert _task_frozen_recency_s(cfg) == DEFAULT_TASK_FROZEN_RECENCY_S


def test_task_frozen_env_knob_is_hot_reloadable():
    """`AP2_TASK_FROZEN_RECENCY_S` lands in `HOT_RELOADABLE_KNOBS`
    so a recency-floor change takes effect on the next tick without
    a daemon restart. Mirrors TB-282's
    `test_attention_env_knobs_are_hot_reloadable`."""
    from ap2.env_reload import HOT_RELOADABLE_KNOBS

    assert "AP2_TASK_FROZEN_RECENCY_S" in HOT_RELOADABLE_KNOBS


# ===========================================================================
# Source-anchor pins mirroring the briefing's Verification greps.
# ===========================================================================


def test_briefing_verification_greps_match():
    """Mirror the briefing's `## Verification` greps in test form so
    a refactor that violates the structural pins surfaces here as a
    clean test failure (parallel to TB-282's pin)."""
    repo_root = Path(__file__).resolve().parent.parent
    # TB-343: the attention body moved to the sibling impl.py.
    attention_src = (repo_root / "components" / "attention" / "impl.py").read_text()
    config_src = (repo_root / "config.py").read_text()
    # TB-398 carved the attention-knob documentation into
    # `skills/ap2-config/SKILL.md`'s `## Configuration knobs` section, so
    # the operator-facing detector mention now lives in the config skill.
    config_skill_src = (
        repo_root / "skills" / "ap2-config" / "SKILL.md"
    ).read_text()
    architecture_src = (repo_root / "architecture.md").read_text()

    # `grep -q "_detect_task_frozen" ap2/attention.py`
    assert "_detect_task_frozen" in attention_src

    # `grep -q "AP2_TASK_FROZEN_RECENCY_S" ap2/config.py`
    assert "AP2_TASK_FROZEN_RECENCY_S" in config_src

    # `grep -q "task_frozen" skills/ap2-config/SKILL.md`
    assert "task_frozen" in config_skill_src

    # `grep -q "task_frozen" ap2/architecture.md`
    assert "task_frozen" in architecture_src


# ===========================================================================
# Integration with the union dispatcher.
# ===========================================================================


def test_detect_attention_conditions_includes_task_frozen(cfg: Config):
    """`detect_attention_conditions` runs both `_detect_task_stuck`
    and `_detect_task_frozen` and unions the results. Pin the
    wire-up so a refactor that forgets one detector surfaces.
    """
    _seed_frozen_task(cfg, "TB-350", "Union test freeze")
    now = _dt.datetime(2026, 5, 23, 12, 0, 0, tzinfo=_dt.timezone.utc)
    _emit_freeze_entry(cfg, "TB-350", seconds_ago=1800, now=now)

    # Also seed via the direct detector to assert symmetry with the
    # union path — both should produce the same candidate.
    tail = events.tail(cfg.events_file, 100)
    direct = _detect_task_frozen(cfg, tail=tail, now=now)
    union = detect_attention_conditions(cfg, tail=tail, now=now)
    union_frozen = [c for c in union if c.type == "task_frozen"]
    assert [c.key for c in direct] == [c.key for c in union_frozen]
    assert union_frozen[0].key == "task_frozen:TB-350"

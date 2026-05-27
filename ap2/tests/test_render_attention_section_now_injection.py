"""TB-301: regression-pin for the `now=` injection seam on
`render_attention_section`.

The renderer historically took only `(cfg, *, since_event_idx, tail)`
and called `_attention.detect_attention_conditions(cfg, tail=tail)`
without threading `now`. The detector defaults to actual wall-clock
UTC. End-to-end render tests that seed events relative to a hardcoded
reference time (e.g. `now = datetime(2026, 5, 26, 12, 0, 0)` — the
shape TB-288 / TB-289 / TB-290 use) work on the day they were written
because the seeded events still fall inside the detector's 24h
recency window relative to actual UTC. They break the moment that
calendar day passes: the seeded events drift out of the window, the
renderer emits "", and the assertion `"## Attention needed" in
rendered` fails. The full pytest suite is then blocked on a single
unrelated test, time-bombing every downstream regression gate.

TB-301 threads an optional `now: _dt.datetime | None = None` kwarg
through `render_attention_section` → `detect_attention_conditions`.
Default-None preserves production behavior (cron status-report path
doesn't carry a `now` reference; the detector reads actual UTC).
Tests inject a deterministic reference so the seeded events line up
with the detector's window regardless of when the test runs.

This module pins two arcs:

  (1) Production path — `render_attention_section(cfg,
      since_event_idx=0)` (no `now`) still works against same-day-
      seeded events. Mirrors the cron status-report call shape.
  (2) Time-bombed-test path — `render_attention_section(cfg,
      since_event_idx=0, now=fixed_now)` uses the injected reference
      for the detector's window. Seed events relative to a hardcoded
      `now` from 2025 (well outside any rolling window relative to
      actual UTC) and assert the rendered output is non-empty. This
      is the structural pin that would have caught the TB-288 /
      TB-290 time-bomb at write-time.

Plus a signature-pin documenting the kwarg's existence and default.
"""
from __future__ import annotations

import datetime as _dt
import inspect
import json as _json
from pathlib import Path

import pytest

from ap2 import events
from ap2.config import Config
from ap2.init import init_project
from ap2.status_report import render_attention_section


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch) -> Config:
    """Clean project scaffold with the validator-judge / task-stuck /
    task-frozen / debounce env knobs unset so the defaults are the
    contract under test. Mirrors TB-288 / TB-290's `cfg` fixture so
    the seeded events fire the same detectors at the same thresholds.
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
    when = now - _dt.timedelta(seconds=seconds_ago)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _rewrite_last_event_ts(cfg: Config, ts: str) -> None:
    lines = cfg.events_file.read_text().splitlines()
    if not lines:
        return
    last = _json.loads(lines[-1])
    last["ts"] = ts
    lines[-1] = _json.dumps(last)
    cfg.events_file.write_text("\n".join(lines) + "\n")


def _seed_judge_fail_events(
    cfg: Config, *, count: int, now: _dt.datetime, seconds_ago_base: float = 3600,
) -> None:
    """Seed `count` `validator_judge_fail` events spaced 60s apart,
    each rewritten to land `seconds_ago_base + k*60s` before `now`.
    With default threshold 5, count=5 inside the 24h window fires the
    `validator_judge_noisy` detector — the same shape TB-288 uses.
    """
    offset = 0.0
    for _ in range(count):
        events.append(
            cfg.events_file, "validator_judge_fail",
            timeout_s=60.0, briefing_bytes=4000, max_turns=2,
        )
        _rewrite_last_event_ts(
            cfg, _ts_seconds_ago(now, seconds_ago=seconds_ago_base + offset),
        )
        offset += 60.0


# ===========================================================================
# Arc 0: signature pin — `now` kwarg exists with default None.
# ===========================================================================


def test_render_attention_section_signature_has_now_kwarg():
    """`render_attention_section` accepts an optional `now=` kwarg
    with default None. Pin the API surface so a refactor that drops
    the seam re-introduces the time-bomb shape.
    """
    sig = inspect.signature(render_attention_section)
    assert "now" in sig.parameters, sig
    param = sig.parameters["now"]
    assert param.default is None, param
    # Keyword-only — production callers pass by name.
    assert param.kind == inspect.Parameter.KEYWORD_ONLY, param


# ===========================================================================
# Arc 1: production path — no `now=` kwarg, events seeded near actual UTC.
# ===========================================================================


def test_render_attention_section_no_now_uses_actual_utc(cfg: Config):
    """`render_attention_section(cfg, since_event_idx=0)` (no `now`)
    still produces a non-empty section when events are seeded RELATIVE
    TO actual UTC. This mirrors the production cron status-report path
    — it doesn't carry a `now` reference and must use wall-clock time.
    """
    actual_now = _dt.datetime.now(_dt.timezone.utc)
    _seed_judge_fail_events(cfg, count=5, now=actual_now)

    rendered = render_attention_section(cfg, since_event_idx=0)
    assert "## Attention needed" in rendered, rendered
    assert "validator-judge noisy" in rendered, rendered


# ===========================================================================
# Arc 2: injected reference — events seeded from 2025 still surface.
# ===========================================================================


def test_render_attention_section_with_now_uses_injected_reference(cfg: Config):
    """`render_attention_section(cfg, since_event_idx=0, now=fixed_now)`
    threads `fixed_now` into `detect_attention_conditions`'s window.
    Seed events relative to a hardcoded 2025-03-15 reference (well
    outside any rolling 24h window relative to ACTUAL UTC at any
    plausible future test-run time), inject the same reference, and
    assert the rendered section is non-empty. Without the seam (or
    if a refactor stops threading `now=`), this test fails the same
    way TB-288's L415 did — empty `rendered`, missing heading.
    """
    fixed_now = _dt.datetime(2025, 3, 15, 9, 30, 0, tzinfo=_dt.timezone.utc)
    _seed_judge_fail_events(cfg, count=5, now=fixed_now)

    rendered = render_attention_section(
        cfg, since_event_idx=0, now=fixed_now,
    )
    assert "## Attention needed" in rendered, rendered
    assert "validator-judge noisy" in rendered, rendered


def test_render_attention_section_without_now_skips_old_events(cfg: Config):
    """Negative-side of Arc 2: when events are seeded relative to a
    far-past `now` and the renderer is called WITHOUT `now=`, the
    detector falls back to actual UTC; the seeded events fall outside
    the 24h window; the section is empty. This is exactly the
    time-bomb shape — pinning it documents WHY the seam exists.
    """
    far_past = _dt.datetime(2025, 3, 15, 9, 30, 0, tzinfo=_dt.timezone.utc)
    _seed_judge_fail_events(cfg, count=5, now=far_past)

    rendered = render_attention_section(cfg, since_event_idx=0)
    assert rendered == "", rendered

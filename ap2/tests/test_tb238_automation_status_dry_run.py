"""TB-238: focused regression-pin for the dry-run readiness signal on
the collector + status-report digest surfaces.

TB-238 (`d861d83`) extended `automation_status.collect_auto_approve_state`
with `auto_unfreeze_dry_run_enabled` + `would_auto_unfreeze_count_24h`
(parallel to TB-232's auto-approve dry-run pair) and added a
`*Dry-run window:*` sub-block to
`status_report.render_automation_loop_activity_section`. The bulk of
TB-238's tests live alongside the TB-227 / TB-228 module files (the
collector contract + the digest section's existing fixtures); this
module is the dedicated regression-pin surface the briefing-driven
verifier references for the TB-238 dry-run contract.

Two regression pins:

  (1) Collector contract — the four dry-run fields land in the dict
      with the documented types AND the `would_auto_unfreeze_count_24h`
      counter aggregates from `would_auto_unfreeze` events in the
      24h window.
  (2) Digest contract — `render_automation_loop_activity_section`
      emits a trailing `*Dry-run window:*` sub-block when either knob
      is on, with per-axis line gating, and omits the sub-block
      entirely when both knobs are off (byte-identical to TB-228
      pre-TB-238 baseline).

A future refactor that drops a dry-run field, breaks the digest
sub-block's per-axis suppression, or pollutes the default-off output
trips a focused subset of these pins with a diff-shaped error.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ap2 import automation_status, events
from ap2.config import Config
from ap2.init import init_project
from ap2.status_report import render_automation_loop_activity_section


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    init_project(tmp_path)
    c = Config.load(tmp_path)
    c.ensure_dirs()
    return c


# ---------------------------------------------------------------------------
# (1) Collector contract.
# ---------------------------------------------------------------------------


def test_collector_dry_run_fields_present_with_documented_types(
    cfg: Config, monkeypatch,
):
    """All four dry-run readiness fields land in
    `collect_auto_approve_state`'s public dict with the documented
    types (bool for the enabled flags, int for the 24h counters).

    Pin against a refactor that drops a key or returns `None` where
    an int is documented — the operator-facing CLI / web / JSON
    surfaces (TB-241) consume this dict and would crash on a key
    miss.
    """
    monkeypatch.delenv("AP2_AUTO_APPROVE_DRY_RUN", raising=False)
    monkeypatch.delenv("AP2_AUTO_UNFREEZE_DRY_RUN", raising=False)

    state = automation_status.collect_auto_approve_state(cfg)
    # Keys present.
    for k in (
        "dry_run_enabled",
        "would_auto_approve_count_24h",
        "auto_unfreeze_dry_run_enabled",
        "would_auto_unfreeze_count_24h",
    ):
        assert k in state, k
    # Types per the docstring contract.
    assert isinstance(state["dry_run_enabled"], bool)
    assert isinstance(state["auto_unfreeze_dry_run_enabled"], bool)
    assert isinstance(state["would_auto_approve_count_24h"], int)
    assert isinstance(state["would_auto_unfreeze_count_24h"], int)


def test_collector_would_auto_unfreeze_counter_aggregates(
    cfg: Config, monkeypatch,
):
    """Two seeded `would_auto_unfreeze` events → counter reads 2.
    Mirror of the TB-232 `would_auto_approve_count_24h` pin on the
    axis-2 side; pins the aggregator's tail-scan symmetry across both
    event streams."""
    monkeypatch.setenv("AP2_AUTO_UNFREEZE_DRY_RUN", "1")

    state = automation_status.collect_auto_approve_state(cfg)
    assert state["would_auto_unfreeze_count_24h"] == 0

    for tid in ("TB-2000", "TB-2001"):
        events.append(
            cfg.events_file, "would_auto_unfreeze",
            task=tid, shape="blocked_review_typo",
            **{"from": "x", "to": "y", "file": "f.md", "line": 1,
               "dry_run": True},
        )

    state = automation_status.collect_auto_approve_state(cfg)
    assert state["would_auto_unfreeze_count_24h"] == 2


# ---------------------------------------------------------------------------
# (2) Digest contract.
# ---------------------------------------------------------------------------


def _previous_status_report_idx(cfg: Config) -> int:
    """Mirror of the helper in TB-228's test file — finds the previous
    `cron_complete job=status-report` event idx, or -1 when none
    exists (fresh fixture)."""
    if not cfg.events_file.exists():
        return -1
    tail = events.tail(cfg.events_file, 2000)
    return automation_status.find_previous_status_report_idx(tail)


def test_digest_dry_run_subblock_renders_when_either_knob_on(
    cfg: Config, monkeypatch,
):
    """At least one dry-run knob on → digest section ends with a
    `*Dry-run window:*` sub-block. On-axis line surfaces the rolling
    24h count; off-axis line is suppressed (no zero-noise on the axis
    the operator hasn't opted in to)."""
    monkeypatch.setenv("AP2_COMPONENTS_AUTO_APPROVE_ENABLED", "1")
    monkeypatch.setenv("AP2_COMPONENTS_AUTO_APPROVE_DRY_RUN", "1")
    monkeypatch.delenv("AP2_COMPONENTS_AUTO_UNFREEZE_DRY_RUN", raising=False)

    events.append(
        cfg.events_file, "would_auto_approve",
        task="TB-2100", knob="1", dry_run=True,
    )

    section = render_automation_loop_activity_section(
        cfg, since_event_idx=_previous_status_report_idx(cfg),
    )
    assert "*Dry-run window:*" in section, section
    assert "auto-approve: `1` `would_auto_approve` in 24h" in section
    # Off-axis line suppressed.
    assert "would_auto_unfreeze" not in section


def test_digest_dry_run_subblock_omitted_when_both_knobs_off(
    cfg: Config, monkeypatch,
):
    """Default-off byte-identical regression pin: neither dry-run knob
    set → rendered section MUST NOT include the `*Dry-run window:*`
    header or any per-axis line. Pins the omit-on-empty rule that
    keeps the TB-228 default operator experience untouched by the
    new readiness signal.
    """
    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")
    monkeypatch.delenv("AP2_AUTO_APPROVE_DRY_RUN", raising=False)
    monkeypatch.delenv("AP2_AUTO_UNFREEZE_DRY_RUN", raising=False)

    section = render_automation_loop_activity_section(
        cfg, since_event_idx=_previous_status_report_idx(cfg),
    )
    assert "Dry-run window" not in section, section
    assert "would_auto_approve" not in section
    assert "would_auto_unfreeze" not in section

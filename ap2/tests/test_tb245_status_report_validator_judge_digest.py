"""TB-245: behavioral pinning for the status-report cron's
`*Validator-judge fail-open window (24h):*` sub-block + the axis-1
event-type allowlist extension on `_status_report_should_skip`.

TB-243 (`647b771`) shipped the pull surfaces for axis-1 validator-judge
fail-open state (`ap2 status` text/JSON + web home automation card).
TB-245 closes the push-surface gap: the operator's primary walk-away
channel — the status-report Mattermost post — was silent on
`validator_judge_fail` / `validator_judge_timeout`, which directly
weakens the goal.md L82-85 auto-approve safety claim ("upstream gates
already make this safe in practice"). The dep-coherence judge IS one
of those upstream gates, and a fail-open gate without push-channel
observability is functionally invisible during the walk-away window
goal.md L57-59 promises. A judge silently degrading at 03:00Z used to
wait for the operator's next manual `ap2 status` to surface; now it
lands in the next status-report cron post.

This module pins six arcs (briefing scope item 5):

  (a) `_status_report_should_skip` returns False when only a
      `validator_judge_fail` event sits in the since-last-report
      window (no other interesting activity).
  (b) `_status_report_should_skip` returns False when only a
      `validator_judge_timeout` event sits in the window.
  (c) Renderer emits the expected lines for a state dict with 0 / 1 /
      both-non-zero counts and exercises the `[noisy]` badge gating.
  (d) Renderer renders the per-event-type lines with the
      `validator_judge_fail:` / `validator_judge_timeout:` literals
      verbatim so the operator's grep on the Mattermost post matches
      `ap2 logs --type validator_judge_*`.
  (e) Renderer omits the entire sub-block when both counts are zero
      (byte-identical to no-renderer baseline).
  (f) `_STATUS_REPORT_AUTOMATION_INTERESTING_TYPES` frozenset
      contains both `validator_judge_fail` and `validator_judge_timeout`.

Plus an end-to-end pin that the routine threads the rendered
sub-block through `state_extras` so the agent forwards it verbatim
(parallel to TB-228 / TB-244's wiring tests).
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json as _json
from pathlib import Path

import pytest

from ap2 import automation_status, events
from ap2.config import Config
from ap2.init import init_project
from ap2.status_report import (
    _STATUS_REPORT_AUTOMATION_INTERESTING_TYPES,
    _status_report_should_skip,
    render_validator_judge_activity_section,
    run_status_report,
)


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    init_project(tmp_path)
    c = Config.load(tmp_path)
    c.ensure_dirs()
    return c


class _NoopSDK:
    """SDK stub: records `query` was called, returns an empty async gen.

    Mirrors TB-228 / TB-244's `_NoopSDK`. The routine still needs
    `ClaudeAgentOptions` on the instance even though these tests assert
    against `state_extras` rather than the SDK call site.
    """

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


def _seed_active(cfg: Config) -> None:
    """Seed a `cron_complete name=status-report` so the digest helpers
    have a previous-report anchor."""
    events.append(cfg.events_file, "cron_complete", job="status-report")


def _ts_offset(now: _dt.datetime, *, hours_ago: float) -> str:
    when = now - _dt.timedelta(hours=hours_ago)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _rewrite_last_event_ts(cfg: Config, ts: str) -> None:
    """Replace the `ts` field on the most recent events.jsonl line.

    Mirrors `test_tb243_validator_judge_surface._rewrite_last_event_ts`
    so the 24h-window edge cases are reachable without a freezegun
    dependency. Public `events.append` always stamps `now()`; tests
    that need an event "in the past" rewrite the line after append.
    """
    lines = cfg.events_file.read_text().splitlines()
    if not lines:
        return
    last = _json.loads(lines[-1])
    last["ts"] = ts
    lines[-1] = _json.dumps(last)
    cfg.events_file.write_text("\n".join(lines) + "\n")


# ===========================================================================
# (a) should-skip returns False when only validator_judge_fail in window.
# ===========================================================================


def test_should_skip_false_when_validator_judge_fail_in_window(
    cfg: Config,
):
    """A `validator_judge_fail` event past the last
    `cron_complete name=status-report` means the TB-235 dep-coherence
    judge fell back to fail-open on a briefing — the cron MUST NOT
    skip even if no other interesting activity happened. The
    operator's primary push channel must carry the silent-degradation
    signal so the auto-approve safety claim (goal.md L82-85) doesn't
    rest on an invisible gate."""
    events.append(cfg.events_file, "cron_complete", job="status-report")
    events.append(
        cfg.events_file, "validator_judge_fail",
        error="non-dict judge response",
    )

    assert _status_report_should_skip(cfg) is False, (
        "validator_judge_fail in the window must keep the report from "
        "skipping — operator must see the fail-open on the next post"
    )


# ===========================================================================
# (b) should-skip returns False when only validator_judge_timeout in window.
# ===========================================================================


def test_should_skip_false_when_validator_judge_timeout_in_window(
    cfg: Config,
):
    """A `validator_judge_timeout` event past the last
    `cron_complete name=status-report` means the TB-235 dep-coherence
    judge SDK call exceeded `AP2_VALIDATOR_JUDGE_TIMEOUT_S` — that's
    operator-visible degradation the post should carry. Pinning the
    gate's behavior on a lone axis-1 timeout with no other activity."""
    events.append(cfg.events_file, "cron_complete", job="status-report")
    events.append(
        cfg.events_file, "validator_judge_timeout",
        timeout_s=15.0, error="validator judge worker exceeded 20s",
    )

    assert _status_report_should_skip(cfg) is False, (
        "validator_judge_timeout in the window must keep the report "
        "from skipping — operator must see the timeout on the next post"
    )


# ===========================================================================
# (c) Renderer handles 0 / 1 / both-non-zero state dicts + [noisy] gating.
# ===========================================================================


def test_renderer_returns_empty_list_when_both_counts_zero():
    """Zero-state → renderer returns `[]` (omit-on-empty rule pinned at
    the source). Load-bearing default-off byte-identical pin so the
    pre-TB-245 digest stays unchanged on quiet windows. Pin against a
    refactor that accidentally always renders the heading."""
    state = {
        "validator_judge_fail_count": 0,
        "validator_judge_timeout_count": 0,
        "total": 0,
        "noisy_threshold": 5,
        "is_noisy": False,
    }
    lines = render_validator_judge_activity_section(state)
    assert lines == [], (
        f"section must be omitted when both 24h counts are zero; "
        f"got: {lines!r}"
    )


def test_renderer_emits_two_lines_when_fail_nonzero():
    """One `validator_judge_fail` count → the rendered list has the
    header + both per-event-type bullets. The timeout bullet renders
    with count 0 (always-two-rows rule symmetric to TB-243's pull-
    surface CLI text which always names both counts when either is
    non-zero)."""
    state = {
        "validator_judge_fail_count": 1,
        "validator_judge_timeout_count": 0,
        "total": 1,
        "noisy_threshold": 5,
        "is_noisy": False,
    }
    lines = render_validator_judge_activity_section(state)
    assert lines[0].startswith("*Validator-judge fail-open window (24h):*"), (
        lines[0]
    )
    # Header has NO `[noisy]` suffix because total=1 < threshold=5.
    assert "[noisy]" not in lines[0], lines[0]
    assert "- validator_judge_fail: 1" in lines
    assert "- validator_judge_timeout: 0" in lines


def test_renderer_emits_two_lines_when_timeout_nonzero():
    """One `validator_judge_timeout` count → the rendered list has the
    header + both per-event-type bullets. The fail bullet renders with
    count 0 (symmetric to the fail-nonzero case above)."""
    state = {
        "validator_judge_fail_count": 0,
        "validator_judge_timeout_count": 3,
        "total": 3,
        "noisy_threshold": 5,
        "is_noisy": False,
    }
    lines = render_validator_judge_activity_section(state)
    assert lines[0].startswith("*Validator-judge fail-open window (24h):*"), (
        lines[0]
    )
    assert "[noisy]" not in lines[0], lines[0]
    assert "- validator_judge_fail: 0" in lines
    assert "- validator_judge_timeout: 3" in lines


def test_renderer_emits_noisy_badge_when_threshold_crossed():
    """When `is_noisy=True`, the header gets a ` [noisy]` suffix
    (mirrors TB-243's pull-side CLI text `[noisy]` suffix). Both
    surfaces light up in lockstep when the operator tunes
    `AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD`."""
    state = {
        "validator_judge_fail_count": 3,
        "validator_judge_timeout_count": 2,
        "total": 5,
        "noisy_threshold": 5,
        "is_noisy": True,
    }
    lines = render_validator_judge_activity_section(state)
    assert lines[0] == "*Validator-judge fail-open window (24h):* [noisy]", (
        lines[0]
    )
    assert "- validator_judge_fail: 3" in lines
    assert "- validator_judge_timeout: 2" in lines


def test_renderer_omits_noisy_badge_when_threshold_not_crossed():
    """When `is_noisy=False` even with non-zero counts, the header
    has NO ` [noisy]` suffix. Pin the badge gating at the rendering
    layer so a refactor that always emits the badge trips here."""
    state = {
        "validator_judge_fail_count": 2,
        "validator_judge_timeout_count": 1,
        "total": 3,
        "noisy_threshold": 5,
        "is_noisy": False,
    }
    lines = render_validator_judge_activity_section(state)
    assert lines[0] == "*Validator-judge fail-open window (24h):*", lines[0]
    assert not any("[noisy]" in ln for ln in lines), lines


# ===========================================================================
# (d) Renderer emits per-event-type bullets with literal event-type names.
# ===========================================================================


def test_renderer_event_type_literals_match_ap2_logs_grep():
    """The bullets carry the literal `validator_judge_fail:` and
    `validator_judge_timeout:` tokens so an operator who copies the
    line into `ap2 logs --type ...` gets a hit. Pin the literal token
    placement so a refactor that renames the prefix breaks here too
    (parallel to TB-228's `auto-approve:` literal pin)."""
    state = {
        "validator_judge_fail_count": 1,
        "validator_judge_timeout_count": 1,
        "total": 2,
        "noisy_threshold": 5,
        "is_noisy": False,
    }
    lines = render_validator_judge_activity_section(state)
    joined = "\n".join(lines)
    assert "validator_judge_fail:" in joined, joined
    assert "validator_judge_timeout:" in joined, joined


# ===========================================================================
# (e) Renderer omits the entire sub-block when both counts are zero
# (byte-identical to no-renderer baseline) — duplicate of (c) at the
# semantic level, pinned separately as a regression marker.
# ===========================================================================


def test_renderer_byte_identical_to_baseline_when_no_axis_1_events(
    cfg: Config,
):
    """No `validator_judge_*` events at all → the collector returns
    zero-state, the renderer returns `[]`, and the wiring in
    `run_status_report` does NOT append anything to `state_extras`.
    Pins the byte-identical no-op so TB-228 / TB-244 existing digest
    tests stay green when axis 1 is quiet — this is the load-bearing
    safety contract for the parallel-renderer shape the briefing
    recommended.
    """
    # Seed a non-axis-1 event so the helper has tail content to walk
    # — without this the assertion would pass trivially on an empty
    # file.
    events.append(
        cfg.events_file, "task_complete",
        task="TB-1", status="complete", commit="abc1234",
    )
    state = automation_status.collect_window_validator_judge(cfg)
    lines = render_validator_judge_activity_section(state)
    assert lines == [], (
        f"renderer must return empty list when no axis-1 events; "
        f"got: {lines!r}"
    )


# ===========================================================================
# (f) The interesting-types frozenset carries the two new tokens.
# ===========================================================================


def test_automation_interesting_types_carries_axis_1_tokens():
    """Source-level pin: TB-245's two axis-1 event-type allowlist
    members land in `_STATUS_REPORT_AUTOMATION_INTERESTING_TYPES`
    so the gate's intent is auditable from one symbol. A refactor
    that drops the additions or renames an event type trips here
    (parallel to TB-228 / TB-244's frozenset pins)."""
    assert (
        "validator_judge_fail"
        in _STATUS_REPORT_AUTOMATION_INTERESTING_TYPES
    ), _STATUS_REPORT_AUTOMATION_INTERESTING_TYPES
    assert (
        "validator_judge_timeout"
        in _STATUS_REPORT_AUTOMATION_INTERESTING_TYPES
    ), _STATUS_REPORT_AUTOMATION_INTERESTING_TYPES
    # The TB-228 / TB-244 entries must remain (regression-pin against
    # an edit that overwrote the frozenset instead of extending it).
    assert (
        "auto_approve_paused"
        in _STATUS_REPORT_AUTOMATION_INTERESTING_TYPES
    )
    assert (
        "auto_unfreeze_applied"
        in _STATUS_REPORT_AUTOMATION_INTERESTING_TYPES
    )
    assert (
        "focus_advanced"
        in _STATUS_REPORT_AUTOMATION_INTERESTING_TYPES
    )
    assert (
        "roadmap_complete"
        in _STATUS_REPORT_AUTOMATION_INTERESTING_TYPES
    )


# ===========================================================================
# Helper contract: collect_window_validator_judge.
# ===========================================================================


def test_collect_window_validator_judge_shape(cfg: Config, monkeypatch):
    """The helper's return dict carries every key the renderer
    consumes — pin the contract so a refactor that drops a key blows
    the renderer up at runtime (not silently)."""
    monkeypatch.delenv("AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD", raising=False)
    state = automation_status.collect_window_validator_judge(cfg)
    assert set(state.keys()) == {
        "validator_judge_fail_count",
        "validator_judge_timeout_count",
        "total",
        "noisy_threshold",
        "is_noisy",
    }
    # Empty-events tail → both counts 0, total 0, default threshold 5,
    # is_noisy False.
    assert state["validator_judge_fail_count"] == 0
    assert state["validator_judge_timeout_count"] == 0
    assert state["total"] == 0
    assert state["noisy_threshold"] == 5
    assert state["is_noisy"] is False


def test_collect_window_validator_judge_counts_events_in_window(
    cfg: Config, monkeypatch,
):
    """The helper counts both event types within the rolling 24h
    window (same arithmetic as TB-243's pull-surface aggregator).
    Pin against a refactor that drops the event-type filter or
    swaps the count direction."""
    monkeypatch.delenv("AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD", raising=False)
    now = _dt.datetime(2026, 5, 17, 12, 0, 0, tzinfo=_dt.timezone.utc)

    events.append(cfg.events_file, "validator_judge_fail", error="boom1")
    _rewrite_last_event_ts(cfg, _ts_offset(now, hours_ago=1))
    events.append(cfg.events_file, "validator_judge_fail", error="boom2")
    _rewrite_last_event_ts(cfg, _ts_offset(now, hours_ago=5))
    events.append(
        cfg.events_file, "validator_judge_timeout",
        timeout_s=15.0, error="TimeoutError()",
    )
    _rewrite_last_event_ts(cfg, _ts_offset(now, hours_ago=3))

    state = automation_status.collect_window_validator_judge(cfg, now=now)
    assert state["validator_judge_fail_count"] == 2
    assert state["validator_judge_timeout_count"] == 1
    assert state["total"] == 3


def test_collect_window_validator_judge_excludes_old_events(
    cfg: Config, monkeypatch,
):
    """Events older than `window_s` MUST NOT count toward the
    aggregates (mirror TB-243's `test_collector_24h_window_excludes_
    old_events`). Pins the window arithmetic at the new collector
    level so a refactor that drops `now_s` / `window_s` semantics
    trips here."""
    monkeypatch.delenv("AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD", raising=False)
    now = _dt.datetime(2026, 5, 17, 12, 0, 0, tzinfo=_dt.timezone.utc)

    # Inside window: 1h ago.
    events.append(cfg.events_file, "validator_judge_fail", error="recent")
    _rewrite_last_event_ts(cfg, _ts_offset(now, hours_ago=1))
    # Outside window: 26h ago.
    events.append(cfg.events_file, "validator_judge_fail", error="old")
    _rewrite_last_event_ts(cfg, _ts_offset(now, hours_ago=26))
    # Outside window: 30h ago.
    events.append(
        cfg.events_file, "validator_judge_timeout",
        timeout_s=15.0, error="old",
    )
    _rewrite_last_event_ts(cfg, _ts_offset(now, hours_ago=30))

    state = automation_status.collect_window_validator_judge(cfg, now=now)
    assert state["validator_judge_fail_count"] == 1
    assert state["validator_judge_timeout_count"] == 0
    assert state["total"] == 1


def test_collect_window_validator_judge_threshold_flips_is_noisy(
    cfg: Config, monkeypatch,
):
    """When `total >= AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD`, the
    collector flips `is_noisy` to True. Pin the threshold-gating at
    the collector level so the renderer is purely presentational
    (the threshold check lives in one place)."""
    # Tune the threshold to 2 so we don't need 5 events.
    monkeypatch.setenv("AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD", "2")
    events.append(cfg.events_file, "validator_judge_fail", error="x")
    events.append(cfg.events_file, "validator_judge_fail", error="x")
    state = automation_status.collect_window_validator_judge(cfg)
    assert state["noisy_threshold"] == 2
    assert state["total"] == 2
    assert state["is_noisy"] is True


def test_collect_window_validator_judge_below_threshold_not_noisy(
    cfg: Config, monkeypatch,
):
    """When `total < AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD`, `is_noisy`
    stays False even when both counts are non-zero. Pins the
    inequality direction at the collector (`>=`, not `>`)."""
    monkeypatch.setenv("AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD", "5")
    events.append(cfg.events_file, "validator_judge_fail", error="x")
    state = automation_status.collect_window_validator_judge(cfg)
    assert state["noisy_threshold"] == 5
    assert state["total"] == 1
    assert state["is_noisy"] is False


# ===========================================================================
# End-to-end: digest threads through run_status_report → state_extras.
# ===========================================================================


def test_run_status_report_injects_validator_judge_into_state_extras(
    tmp_path, monkeypatch,
):
    """A `validator_judge_fail` event in the rolling 24h window → the
    routine appends the rendered sub-block to `state_extras` so the
    rendered prompt's `## Current state` block carries it for the
    agent to forward verbatim. Pin the wiring path so a refactor that
    drops the call site (or threads it through a different parameter)
    trips here (parallel to TB-228 / TB-244's wiring tests).
    """
    monkeypatch.delenv("AP2_AUTO_APPROVE", raising=False)
    monkeypatch.delenv("AP2_MM_REPORT_CHANNEL", raising=False)
    monkeypatch.delenv("AP2_MM_CHANNELS", raising=False)
    monkeypatch.delenv("AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD", raising=False)

    init_project(tmp_path)
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()
    _seed_active(cfg)
    # task_complete so the skip-gate doesn't fire on the routine entry.
    events.append(
        cfg.events_file, "task_complete",
        task="TB-1", status="complete", commit="abc1234",
    )
    events.append(
        cfg.events_file, "validator_judge_fail",
        error="non-dict judge response",
    )
    events.append(
        cfg.events_file, "validator_judge_timeout",
        timeout_s=15.0, error="TimeoutError()",
    )

    captured: dict[str, list[str]] = {"extras": []}

    def _capture(cfg, name, body, *, state_extras=None):
        captured["extras"] = list(state_extras or [])
        return "stub"

    monkeypatch.setattr("ap2.prompts.build_control_prompt", _capture)

    sdk = _NoopSDK()
    asyncio.run(run_status_report(cfg, sdk, mcp_server=None, trigger="cron"))

    joined = "\n".join(captured["extras"])
    assert "*Validator-judge fail-open window (24h):*" in joined, joined
    assert "validator_judge_fail: 1" in joined, joined
    assert "validator_judge_timeout: 1" in joined, joined


def test_run_status_report_omits_validator_judge_section_when_quiet(
    tmp_path, monkeypatch,
):
    """No axis-1 events in the 24h window → the routine does NOT
    append the sub-block to `state_extras`. Pins the omit-on-empty
    rule at the wiring level so axis 1 stays as quiet as TB-228's
    automation digest does on a pre-opt-in / quiet window. Load-
    bearing default-off byte-identical regression pin."""
    monkeypatch.delenv("AP2_AUTO_APPROVE", raising=False)
    monkeypatch.delenv("AP2_MM_REPORT_CHANNEL", raising=False)
    monkeypatch.delenv("AP2_MM_CHANNELS", raising=False)
    monkeypatch.delenv("AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD", raising=False)

    init_project(tmp_path)
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()
    _seed_active(cfg)
    events.append(
        cfg.events_file, "task_complete",
        task="TB-1", status="complete", commit="abc1234",
    )

    captured: dict[str, list[str]] = {"extras": []}

    def _capture(cfg, name, body, *, state_extras=None):
        captured["extras"] = list(state_extras or [])
        return "stub"

    monkeypatch.setattr("ap2.prompts.build_control_prompt", _capture)

    sdk = _NoopSDK()
    asyncio.run(run_status_report(cfg, sdk, mcp_server=None, trigger="cron"))

    joined = "\n".join(captured["extras"])
    assert "Validator-judge fail-open" not in joined, (
        f"validator-judge sub-block must not appear when no axis-1 "
        f"events in the 24h window; extras={captured['extras']!r}"
    )


# ===========================================================================
# Structural pins (briefing's grep verifiers).
# ===========================================================================


def test_status_report_module_carries_validator_judge_heading():
    """`grep -n "Validator-judge fail-open" ap2/status_report.py`
    (briefing verifier) must match: the heading literal lives in
    `status_report.py` so the rendered section's heading is
    auditable from one place."""
    from ap2 import status_report as _mod
    src = Path(_mod.__file__).read_text()
    assert "Validator-judge fail-open window (24h):" in src


def test_status_report_module_references_validator_judge_event_types():
    """`grep -n '"validator_judge_fail"' ap2/status_report.py` and
    `grep -n '"validator_judge_timeout"' ap2/status_report.py`
    (briefing verifiers) must each match: the two event-type tokens
    are named explicitly in the module so the frozenset / docstring /
    renderer contract is traceable from a single grep."""
    from ap2 import status_report as _mod
    src = Path(_mod.__file__).read_text()
    assert '"validator_judge_fail"' in src
    assert '"validator_judge_timeout"' in src


def test_automation_status_module_declares_collect_window_validator_judge():
    """`grep -n "def collect_window_validator_judge"
    ap2/automation_status.py` (briefing verifier) must match: the
    helper is declared at module level so `from ap2 import
    automation_status; automation_status.collect_window_validator_judge`
    works for the renderer + tests."""
    from ap2 import automation_status as _mod
    src = Path(_mod.__file__).read_text()
    assert "def collect_window_validator_judge(" in src


def test_status_report_module_declares_render_validator_judge_section():
    """`grep -n "def render_validator_judge_activity_section"
    ap2/status_report.py` (briefing verifier) must match: the
    renderer is declared at module level so the wiring + tests can
    import it directly."""
    from ap2 import status_report as _mod
    src = Path(_mod.__file__).read_text()
    assert "def render_validator_judge_activity_section(" in src


def test_status_report_prompt_carries_validator_judge_forwarding_contract():
    """The canonical `STATUS_REPORT_PROMPT` body teaches the agent to
    forward the daemon-injected sub-block VERBATIM. Pin the load-
    bearing markers so a paraphrase that drops the contract trips
    here (parallel to TB-228 / TB-244 prompt-contract pins)."""
    from ap2.status_report import STATUS_REPORT_PROMPT
    body = STATUS_REPORT_PROMPT
    assert "Validator-judge fail-open" in body
    # Verbatim forwarding rule (uppercase or lowercase).
    assert "verbatim" in body.lower() or "VERBATIM" in body
    # TB-245 cross-ref so future trims preserve the lineage.
    assert "TB-245" in body


def test_status_report_contract_in_prompts_carries_validator_judge_clause():
    """The `_STATUS_REPORT_CONTRACT` addendum in `ap2/prompts.py`
    also teaches the agent to forward the sub-block verbatim — both
    halves of the prompt-builder pipeline must carry the contract so
    a refactor that drops one half can't quietly weaken it."""
    import inspect
    from ap2 import prompts
    src = inspect.getsource(prompts)
    assert "Validator-judge fail-open" in src
    assert "TB-245" in src


def test_cron_default_yaml_stub_mentions_validator_judge():
    """The cron.default.yaml stub is what gets surfaced via `ap2
    cron list` for a curious operator. The stub mentions the new
    sub-block so the operator following the breadcrumb sees the
    surface exists without reading source."""
    cron_yaml = (
        Path(__file__).resolve().parent.parent / "cron.default.yaml"
    )
    text = cron_yaml.read_text()
    assert "Validator-judge fail-open" in text
    assert "TB-245" in text


def test_howto_carries_tb245_cross_reference():
    """`grep -n TB-245 ap2/howto.md` (briefing verifier) must match:
    the howto's existing TB-243 validator-judge block (TB-235
    section) cross-references TB-245 so an operator reading howto
    sees that the fail-open events also surface on the cron status-
    report (push surface) — not just `ap2 status` (pull surface)."""
    howto = (
        Path(__file__).resolve().parent.parent / "howto.md"
    )
    assert "TB-245" in howto.read_text()


def test_howto_validator_judge_section_names_push_surface():
    """The howto section cross-referencing TB-245 names the push
    surface (status-report / cron / Mattermost) so the operator
    looking at the TB-235 validator-judge block sees both halves
    of the observability story (pull surface via TB-243, push
    surface via TB-245). Pin the cross-link prose so a trim that
    drops the push-surface mention trips here."""
    howto = (
        Path(__file__).resolve().parent.parent / "howto.md"
    )
    src = howto.read_text()
    # The TB-245 paragraph must live in the TB-235 validator-judge
    # block, not somewhere structurally unrelated. Find the TB-243
    # paragraph (existing) and check that TB-245 lives within a
    # reasonable distance after it.
    tb243_idx = src.find("TB-243 surfaces")
    tb245_idx = src.find("TB-245")
    assert tb243_idx >= 0, "TB-243 paragraph anchor not found in howto"
    assert tb245_idx > tb243_idx, (
        "TB-245 cross-reference must live AFTER the TB-243 paragraph "
        "inside the TB-235 validator-judge block"
    )
    # Some lexical marker of the push surface.
    push_markers = ("status-report", "Mattermost", "cron post", "push-surface")
    assert any(
        m in src[tb245_idx:tb245_idx + 800] for m in push_markers
    ), src[tb245_idx:tb245_idx + 800]

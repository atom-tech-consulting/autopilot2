"""TB-228: behavioral pinning for the status-report cron's
`## Automation loop activity` digest section.

The walk-away operator's first-touch surface is the scheduled
status-report Mattermost post. Pre-TB-228 it carried board counts +
recent completes + pending-review TB-Ns + decisions-needed bullets,
but it was silent on the TB-223 / TB-224 / TB-225 automation loop —
an operator returning to find 12 auto-approved tasks had landed
unattended had to alt-tab to `ap2 logs` to learn it.

This module pins five arcs:

  (1) Section absent when knob off + all four event-type counters
      zero in the window (no zero-noise on pre-opt-in projects).
  (2) Section present when knob on + counters zero — renders the
      "healthy, 0 since last report" baseline so the operator's
      muscle memory sees a stable section position.
  (3) Section present + paused — renders the pause reason + the ack
      verb the operator needs to run.
  (4) Section present when counters non-zero (knob may be on or off
      historically — handles operator toggling).
  (5) `_status_report_should_skip` returns False when an
      `auto_approve_paused` event landed in the window, even if
      nothing else interesting happened.

The digest is `render_automation_loop_activity_section`'s output;
this test module exercises it directly + through `run_status_report`
to pin both the helper and the routine's `state_extras` plumbing.
"""
from __future__ import annotations

import asyncio
import json as _json
from pathlib import Path

import pytest

from ap2 import automation_status, events
from ap2.config import Config
from ap2.init import init_project
from ap2.status_report import (
    _STATUS_REPORT_AUTOMATION_INTERESTING_TYPES,
    _status_report_should_skip,
    render_automation_loop_activity_section,
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

    Mirrors `test_status_report_skip._NoopSDK`. The routine still needs
    `ClaudeAgentOptions` on the instance even though the test asserts
    against `state_extras` (not the SDK call site).
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
    """Seed a `cron_complete name=status-report` + a `task_complete`
    so the skip-gate doesn't fire — most tests need the run path."""
    events.append(cfg.events_file, "cron_complete", job="status-report")
    events.append(
        cfg.events_file, "task_complete",
        task="TB-1", status="complete", commit="abc1234",
    )


def _previous_status_report_idx(cfg: Config) -> int:
    """Find the previous `cron_complete name=status-report` idx the
    digest scopes against — same helper the routine uses internally."""
    tail = events.tail(cfg.events_file, 2000)
    return automation_status.find_previous_status_report_idx(tail)


# ===========================================================================
# Arc 1: section absent when knob off AND all counters zero.
# ===========================================================================


def test_section_absent_when_knob_off_and_all_counters_zero(
    cfg: Config, monkeypatch,
):
    """Pre-opt-in project (knob unset + no automation-loop events) →
    the renderer returns "" so a fresh project doesn't grow a perpetual
    "0 since last report" bullet on the cron post.

    Pin against a refactor that flips the omit-on-empty rule to
    "always render with zeros" — that would mean every fresh project
    starts emitting noise.
    """
    monkeypatch.delenv("AP2_AUTO_APPROVE", raising=False)
    # No automation-loop events have ever been emitted; events file is
    # either empty or carries only the bootstrap noise.
    section = render_automation_loop_activity_section(
        cfg, since_event_idx=_previous_status_report_idx(cfg),
    )
    assert section == "", (
        f"section must be omitted on a pre-opt-in / silent project; "
        f"got: {section!r}"
    )


def test_run_status_report_omits_section_when_knob_off_and_quiet(
    tmp_path, monkeypatch,
):
    """End-to-end: the routine doesn't append the digest to
    `state_extras` when the renderer returns "". Pins the omit-on-
    empty rule at the wiring level so a refactor that always appends
    (even an empty string) regresses cleanly."""
    monkeypatch.delenv("AP2_AUTO_APPROVE", raising=False)
    monkeypatch.delenv("AP2_MM_REPORT_CHANNEL", raising=False)
    monkeypatch.delenv("AP2_MM_CHANNELS", raising=False)

    init_project(tmp_path)
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()
    _seed_active(cfg)

    captured: dict[str, list[str]] = {"extras": []}

    def _capture(cfg, name, body, *, state_extras=None):
        captured["extras"] = list(state_extras or [])
        return "stub"

    monkeypatch.setattr("ap2.prompts.build_control_prompt", _capture)

    sdk = _NoopSDK()
    asyncio.run(run_status_report(cfg, sdk, mcp_server=None, trigger="cron"))

    joined = "\n".join(captured["extras"])
    assert "Automation loop activity" not in joined, (
        f"section must not appear when knob off + no loop events; "
        f"extras={captured['extras']!r}"
    )


# ===========================================================================
# Arc 2: section present when knob on + counters zero.
# ===========================================================================


def test_section_present_when_knob_on_and_counters_zero(
    cfg: Config, monkeypatch,
):
    """Knob on, no halt, no loop events in the window → render the
    healthy baseline so the operator's scanning position is stable.
    Pin the headline + the "0 tasks auto-approved" baseline bullet."""
    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")

    section = render_automation_loop_activity_section(
        cfg, since_event_idx=_previous_status_report_idx(cfg),
    )
    assert section.startswith("## Automation loop activity"), section
    # Healthy headline — no PAUSED, no cooldown.
    assert "auto-approve: healthy" in section
    assert "auto-unfreeze: healthy" in section
    # The zero-baseline bullet renders so the section has a stable
    # bullet structure even on a quiet window.
    assert "0 tasks auto-approved" in section
    # Healthy state must NOT name an ack verb.
    assert "ap2 ack" not in section


# ===========================================================================
# Arc 3: section present + paused — names the ack verb.
# ===========================================================================


def test_section_renders_paused_headline_with_ack_verb(
    cfg: Config, monkeypatch,
):
    """TB-224 cost halt landed in the window → headline carries
    `PAUSED reason=window_token_cap_exceeded` and the last bullet
    names `ap2 ack auto_approve_window_resume` so the operator's
    next action is one copy-paste away.

    Mirrors TB-227's CLI/web pause rendering (`auto-approve: PAUSED
    (reason=...)`) so the operator sees one vocabulary across all
    three surfaces.
    """
    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")
    events.append(
        cfg.events_file, "auto_approve_halted",
        task="TB-500", reason="window_cap",
        used=1_200_000, cap=1_000_000, window_used=1_200_000,
    )

    section = render_automation_loop_activity_section(
        cfg, since_event_idx=_previous_status_report_idx(cfg),
    )
    assert "auto-approve: PAUSED reason=window_token_cap_exceeded" in section
    # The ack verb the operator runs to clear the halt is named
    # literally so it's copy-pasteable from the post.
    assert "ap2 ack auto_approve_window_resume" in section
    # The pause-event timestamp + type land in the "Most recent halt"
    # bullet so post-mortems can correlate the halt with its trigger.
    assert "auto_approve_halted" in section
    assert "Most recent halt:" in section


def test_section_renders_paused_on_consecutive_freezes(
    cfg: Config, monkeypatch,
):
    """TB-223 cumulative-regression circuit-breaker fired (an
    `auto_approve_paused` event landed in the window) → headline
    carries `PAUSED reason=consecutive_freezes` and the bullet names
    `ap2 ack auto_approve_unfreeze` (the TB-223-specific ack verb,
    distinct from TB-224's window-resume verb)."""
    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")
    events.append(
        cfg.events_file, "auto_approve_paused",
        task="TB-501", threshold=3, reason="three consecutive freezes",
    )

    section = render_automation_loop_activity_section(
        cfg, since_event_idx=_previous_status_report_idx(cfg),
    )
    assert "PAUSED reason=consecutive_freezes" in section
    assert "ap2 ack auto_approve_unfreeze" in section


# ===========================================================================
# Arc 4: section present when counters non-zero (knob may toggle).
# ===========================================================================


def test_section_renders_with_nonzero_counts(
    cfg: Config, monkeypatch,
):
    """Window has auto_approved + auto_unfreeze_applied + skipped
    events → the section renders bullets with the counts. Pins the
    operator-readable breakdown shape."""
    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")
    # 2 auto-approved tasks; TB-600 completes, TB-601 froze.
    events.append(cfg.events_file, "auto_approved", task="TB-600", knob="1")
    events.append(cfg.events_file, "auto_approved", task="TB-601", knob="1")
    events.append(
        cfg.events_file, "task_complete",
        task="TB-600", status="complete", commit="aaaa111",
    )
    events.append(
        cfg.events_file, "task_complete",
        task="TB-601", status="verification_failed", commit="",
    )
    # 1 auto-unfreeze applied that subsequently completed.
    events.append(
        cfg.events_file, "auto_unfreeze_applied",
        task="TB-602", shape="blocked_review_typo",
        **{"from": "x", "to": "y"},
    )
    events.append(
        cfg.events_file, "task_complete",
        task="TB-602", status="complete", commit="bbbb222",
    )
    # 2 skipped — different reasons.
    events.append(
        cfg.events_file, "auto_unfreeze_skipped",
        task="TB-603", reason="shape_not_in_allowlist", shape="x",
    )
    events.append(
        cfg.events_file, "auto_unfreeze_skipped",
        task="TB-604", reason="per_task_cap", applied=2, cap=2,
    )

    section = render_automation_loop_activity_section(
        cfg, since_event_idx=_previous_status_report_idx(cfg),
    )
    assert "2 tasks auto-approved (1 completed, 1 froze)" in section
    # L=1 distinct task, R=1 shape applied.
    assert "1 tasks auto-unfrozen / 1 briefing-fix shapes auto-applied" in section
    assert "1 succeeded, 0 re-froze" in section
    # Skipped breakdown — deterministic alphabetical order.
    assert "2 auto-unfreeze attempts skipped" in section
    assert "per_task_cap=1" in section
    assert "shape_not_in_allowlist=1" in section


def test_section_renders_when_knob_off_but_counters_nonzero(
    cfg: Config, monkeypatch,
):
    """Operator toggled the knob off mid-cycle, but auto-approved
    events from before the toggle are still in the inter-report
    window. The section must still render so the operator sees
    recent activity — pinning the omit-on-empty rule as
    "knob off AND all counters zero" (both clauses required)."""
    monkeypatch.delenv("AP2_AUTO_APPROVE", raising=False)
    events.append(cfg.events_file, "auto_approved", task="TB-700", knob="1")

    section = render_automation_loop_activity_section(
        cfg, since_event_idx=_previous_status_report_idx(cfg),
    )
    assert section, "non-zero counter must keep the section rendered"
    assert "1 tasks auto-approved" in section


# ===========================================================================
# Arc 5: should-skip gate honors the new event-type allowlist.
# ===========================================================================


def test_should_skip_false_when_auto_approve_paused_in_window(
    cfg: Config, monkeypatch,
):
    """An `auto_approve_paused` event past the last
    `cron_complete name=status-report` means a halt fired — the cron
    MUST NOT skip even if no other interesting activity happened.

    The boring-types denylist in `_status_report_should_skip` already
    excludes this event (it's not in the set), but TB-228 makes the
    intent explicit via `_STATUS_REPORT_AUTOMATION_INTERESTING_TYPES`.
    Test asserts the gate's behavior — the named constant exists is
    pinned separately."""
    # Seed a prior cron_complete with ONLY an auto_approve_paused
    # event after it (no task_complete / verification_failed / etc).
    events.append(cfg.events_file, "cron_complete", job="status-report")
    events.append(
        cfg.events_file, "auto_approve_paused",
        task="TB-800", threshold=3, reason="halt fired",
    )

    assert _status_report_should_skip(cfg) is False, (
        "auto_approve_paused in the window must keep the report from "
        "skipping — operator must see the halt on the next post"
    )


def test_should_skip_false_when_auto_unfreeze_applied_in_window(
    cfg: Config, monkeypatch,
):
    """An `auto_unfreeze_applied` event past the last
    `cron_complete name=status-report` means a briefing-shape fix
    auto-applied — the cron MUST NOT skip; operator returns to see
    which Frozen task came back unattended."""
    events.append(cfg.events_file, "cron_complete", job="status-report")
    events.append(
        cfg.events_file, "auto_unfreeze_applied",
        task="TB-801", shape="blocked_review_typo",
        **{"from": "x", "to": "y"},
    )

    assert _status_report_should_skip(cfg) is False


def test_should_skip_false_when_auto_approve_halted_in_window(cfg: Config):
    """TB-224 cost-halt's `auto_approve_halted` event also keeps the
    report alive — same operator-attention class as the TB-223 pause."""
    events.append(cfg.events_file, "cron_complete", job="status-report")
    events.append(
        cfg.events_file, "auto_approve_halted",
        task="TB-802", reason="window_cap",
        used=1_200_000, cap=1_000_000,
    )

    assert _status_report_should_skip(cfg) is False


def test_automation_interesting_types_constant_pins_event_names():
    """Source-level pin: the briefing's four event-type allowlist
    members all land in `_STATUS_REPORT_AUTOMATION_INTERESTING_TYPES`
    so the gate's intent is auditable from one symbol. A refactor that
    drops the constant or renames an event type trips here."""
    assert "auto_approve_paused" in _STATUS_REPORT_AUTOMATION_INTERESTING_TYPES
    assert "auto_approve_halted" in _STATUS_REPORT_AUTOMATION_INTERESTING_TYPES
    assert (
        "auto_unfreeze_applied" in _STATUS_REPORT_AUTOMATION_INTERESTING_TYPES
    )
    assert (
        "auto_unfreeze_skipped" in _STATUS_REPORT_AUTOMATION_INTERESTING_TYPES
    )


# ===========================================================================
# End-to-end: digest threads through run_status_report → state_extras.
# ===========================================================================


def test_run_status_report_injects_digest_into_state_extras(
    tmp_path, monkeypatch,
):
    """Knob on + a halt fired → the routine appends the rendered
    `## Automation loop activity` section to `state_extras` so the
    rendered prompt's `## Current state` block carries it for the
    agent to forward verbatim. Pin the wiring path so a refactor that
    drops the call site (or threads it through a different parameter)
    trips here."""
    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")
    monkeypatch.delenv("AP2_MM_REPORT_CHANNEL", raising=False)
    monkeypatch.delenv("AP2_MM_CHANNELS", raising=False)

    init_project(tmp_path)
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()
    _seed_active(cfg)
    events.append(
        cfg.events_file, "auto_approve_halted",
        task="TB-900", reason="task_error",
        error_excerpt="SDK timeout",
    )

    captured: dict[str, list[str]] = {"extras": []}

    def _capture(cfg, name, body, *, state_extras=None):
        captured["extras"] = list(state_extras or [])
        return "stub"

    monkeypatch.setattr("ap2.prompts.build_control_prompt", _capture)

    sdk = _NoopSDK()
    asyncio.run(run_status_report(cfg, sdk, mcp_server=None, trigger="cron"))

    joined = "\n".join(captured["extras"])
    assert "## Automation loop activity" in joined
    assert "PAUSED reason=task_error" in joined
    assert "ap2 ack auto_approve_window_resume" in joined


def test_run_status_report_digest_scopes_to_since_last_report(
    tmp_path, monkeypatch,
):
    """The digest counts events past the previous
    `cron_complete name=status-report` event — events BEFORE that
    cron_complete don't count toward the post's window. Pins the
    inter-report scoping so a refactor that uses a wall-clock window
    instead trips here.

    Fixture: an `auto_approved` event lands BEFORE the previous
    cron_complete and another lands AFTER. The digest counts 1, not
    2.
    """
    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")
    monkeypatch.delenv("AP2_MM_REPORT_CHANNEL", raising=False)
    monkeypatch.delenv("AP2_MM_CHANNELS", raising=False)

    init_project(tmp_path)
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()

    # Pre-window: auto-approved task before the previous report — must
    # NOT count toward this report's digest.
    events.append(cfg.events_file, "auto_approved", task="TB-1000", knob="1")
    events.append(cfg.events_file, "cron_complete", job="status-report")
    # In-window: auto-approved task AFTER the previous report — counts.
    events.append(cfg.events_file, "auto_approved", task="TB-1001", knob="1")
    # Seed a `task_complete` so the skip-gate doesn't fire.
    events.append(
        cfg.events_file, "task_complete",
        task="TB-1001", status="complete", commit="ccc3333",
    )

    captured: dict[str, list[str]] = {"extras": []}

    def _capture(cfg, name, body, *, state_extras=None):
        captured["extras"] = list(state_extras or [])
        return "stub"

    monkeypatch.setattr("ap2.prompts.build_control_prompt", _capture)

    sdk = _NoopSDK()
    asyncio.run(run_status_report(cfg, sdk, mcp_server=None, trigger="cron"))

    joined = "\n".join(captured["extras"])
    assert "## Automation loop activity" in joined
    # 1 (TB-1001), not 2 (TB-1000 was pre-window).
    assert "1 tasks auto-approved" in joined


# ===========================================================================
# Helper-symbol structural pins (briefing's grep verifiers).
# ===========================================================================


def test_status_report_module_carries_automation_heading_constant():
    """`grep -nE "Automation loop activity" ap2/status_report.py`
    (briefing verifier) must match: the heading literal lives in
    `status_report.py` so the rendered section's heading is auditable
    from one place."""
    from ap2 import status_report as _mod
    src = Path(_mod.__file__).read_text()
    assert "Automation loop activity" in src


def test_status_report_module_references_auto_approve_paused():
    """`grep -nE "auto_approve_paused" ap2/status_report.py` (briefing
    verifier) must match: the pause-event type is named explicitly so
    the should-skip gate's intent + the digest renderer's headline
    branch are both traceable from a single grep."""
    from ap2 import status_report as _mod
    src = Path(_mod.__file__).read_text()
    assert "auto_approve_paused" in src


def test_status_report_prompt_carries_automation_forwarding_contract():
    """The canonical `STATUS_REPORT_PROMPT` body teaches the agent to
    forward the daemon-injected section VERBATIM. Pin the load-bearing
    markers so a paraphrase that drops the contract trips here."""
    from ap2.status_report import STATUS_REPORT_PROMPT
    body = STATUS_REPORT_PROMPT
    assert "Automation loop activity" in body
    # Verbatim forwarding rule (uppercase or lowercase) — same shape
    # the TB-151 / TB-173 forwarders use.
    assert "verbatim" in body.lower() or "VERBATIM" in body
    # TB-228 cross-ref so future trims preserve the lineage.
    assert "TB-228" in body


def test_status_report_contract_in_prompts_carries_automation_clause():
    """The `_STATUS_REPORT_CONTRACT` addendum in `ap2/prompts.py` also
    teaches the agent to forward the section verbatim — both halves of
    the prompt-builder pipeline must carry the contract so a refactor
    that drops one half can't quietly weaken it."""
    import inspect
    from ap2 import prompts
    src = inspect.getsource(prompts)
    assert "Automation loop activity" in src
    assert "TB-228" in src


def test_cron_default_yaml_stub_mentions_automation_section():
    """The cron.default.yaml stub is what gets surfaced via `ap2 cron
    list` for a curious operator. The stub mentions the new section
    so the operator following the breadcrumb sees the digest exists
    without reading source."""
    cron_yaml = (
        Path(__file__).resolve().parent.parent / "cron.default.yaml"
    )
    text = cron_yaml.read_text()
    assert "Automation loop activity" in text
    assert "TB-228" in text


# ===========================================================================
# Window-activity helper contract.
# ===========================================================================


def test_collect_window_loop_activity_shape(cfg: Config):
    """The helper's return dict carries every key the renderer consumes
    — pin the contract so a refactor that drops a key blows the
    renderer up at runtime (not silently)."""
    activity = automation_status.collect_window_loop_activity(
        cfg, since_event_idx=-1,
    )
    expected_keys = {
        "auto_approved",
        "auto_approved_completed",
        "auto_approved_froze",
        "auto_unfreeze_applied",
        "auto_unfreeze_tasks",
        "auto_unfreeze_succeeded",
        "auto_unfreeze_refroze",
        "auto_unfreeze_skipped",
        "auto_unfreeze_skipped_by_reason",
        "auto_approve_paused",
        "auto_approve_halted",
        "latest_halt",
    }
    assert set(activity.keys()) == expected_keys
    # Empty-events tail → every counter at zero, latest_halt None.
    for k in expected_keys - {
        "auto_unfreeze_skipped_by_reason", "latest_halt",
    }:
        assert activity[k] == 0, f"key {k!r} should be 0; got {activity[k]!r}"
    assert activity["auto_unfreeze_skipped_by_reason"] == {}
    assert activity["latest_halt"] is None


def test_collect_window_loop_activity_latest_halt_carries_ack_verb(
    cfg: Config,
):
    """`latest_halt` shape: `{ts, event_type, reason, ack_verb}` —
    the renderer copies these four fields into the "Most recent halt"
    bullet. Pin the field names so the renderer doesn't KeyError on a
    rename."""
    events.append(
        cfg.events_file, "auto_approve_halted",
        task="TB-1100", reason="per_task_cap",
        used=200_000, cap=150_000,
    )
    activity = automation_status.collect_window_loop_activity(
        cfg, since_event_idx=-1,
    )
    halt = activity["latest_halt"]
    assert halt is not None
    assert halt["event_type"] == "auto_approve_halted"
    assert halt["reason"] == "per_task_token_cap_exceeded"
    assert halt["ack_verb"] == "auto_approve_window_resume"
    assert halt["ts"], "ts field must be populated from the event"


def test_find_previous_status_report_idx_returns_last_match(cfg: Config):
    """The idx helper returns the MOST RECENT
    `cron_complete name=status-report` event's positional index, not
    the first. The digest scopes against "since the last report", so
    a refactor that returns the first cron_complete would surface
    weeks-old activity on every post."""
    events.append(cfg.events_file, "cron_complete", job="status-report")
    events.append(cfg.events_file, "task_complete", task="TB-1", status="complete")
    events.append(cfg.events_file, "cron_complete", job="status-report")
    events.append(cfg.events_file, "task_complete", task="TB-2", status="complete")

    tail = events.tail(cfg.events_file, 2000)
    idx = automation_status.find_previous_status_report_idx(tail)
    # The most recent cron_complete is the third event (idx 2).
    assert tail[idx].get("type") == "cron_complete"
    assert tail[idx].get("job") == "status-report"
    # Events past `idx` should be only the trailing `task_complete`.
    assert all(
        e.get("type") != "cron_complete" or e.get("job") != "status-report"
        for e in tail[idx + 1:]
    )


def test_find_previous_status_report_idx_returns_minus_one_on_empty():
    """No prior status-report cron_complete in the tail → return -1
    (first-ever run, or the previous one rolled out of the tail). The
    renderer uses this to count from the start of the tail."""
    assert automation_status.find_previous_status_report_idx([]) == -1
    assert (
        automation_status.find_previous_status_report_idx(
            [{"type": "task_complete", "task": "TB-1"}]
        )
        == -1
    )


# ===========================================================================
# TB-238: dry-run window sub-block — surfaces the readiness signal that
# TB-232 + TB-233 emit (`would_auto_approve` / `would_auto_unfreeze`
# 24h counts) in the operator's primary return surface so a knob-flip
# can be observed without alt-tabbing to `ap2 logs`.
# ===========================================================================


def test_dry_run_subblock_renders_when_either_knob_on(
    cfg: Config, monkeypatch,
):
    """At least one of `AP2_AUTO_APPROVE_DRY_RUN` /
    `AP2_AUTO_UNFREEZE_DRY_RUN` truthy → the digest section ends with
    a `*Dry-run window:*` sub-block. The on-axis line lists the rolling
    24h count; the off-axis line is suppressed (no zero-noise on the
    axis the operator hasn't opted in to).

    Pinned shape (auto-approve dry-run on, two seeded `would_auto_
    approve` events): the sub-block lists `2` `would_auto_approve` in
    the 24h window. Auto-unfreeze line is absent because that knob is
    off in this fixture.
    """
    # Auto-approve dry-run on; knob also on (real-mode toggle is
    # required to engage the gate, dry-run flips the WRITE step).
    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")
    monkeypatch.setenv("AP2_AUTO_APPROVE_DRY_RUN", "1")
    monkeypatch.delenv("AP2_AUTO_UNFREEZE_DRY_RUN", raising=False)

    events.append(
        cfg.events_file, "would_auto_approve",
        task="TB-1300", knob="1", dry_run=True,
    )
    events.append(
        cfg.events_file, "would_auto_approve",
        task="TB-1301", knob="1", dry_run=True,
    )

    section = render_automation_loop_activity_section(
        cfg, since_event_idx=_previous_status_report_idx(cfg),
    )
    assert "*Dry-run window:*" in section, section
    # On-axis line: count + event type, both code-spanned for legibility.
    assert "auto-approve: `2` `would_auto_approve` in 24h" in section
    # Off-axis line suppressed.
    assert "would_auto_unfreeze" not in section


def test_dry_run_subblock_renders_both_lines_when_both_knobs_on(
    cfg: Config, monkeypatch,
):
    """Both dry-run knobs on → the sub-block lists BOTH axes. Pins the
    parallel structure so operators reading the post during a paired
    dry-run window see both readiness signals in one block."""
    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")
    monkeypatch.setenv("AP2_AUTO_APPROVE_DRY_RUN", "1")
    monkeypatch.setenv("AP2_AUTO_UNFREEZE_DRY_RUN", "1")

    events.append(
        cfg.events_file, "would_auto_approve",
        task="TB-1400", knob="1", dry_run=True,
    )
    events.append(
        cfg.events_file, "would_auto_unfreeze",
        task="TB-1401", shape="blocked_review_typo",
        **{"from": "x", "to": "y", "file": "f.md", "line": 1,
           "dry_run": True},
    )

    section = render_automation_loop_activity_section(
        cfg, since_event_idx=_previous_status_report_idx(cfg),
    )
    assert "*Dry-run window:*" in section
    assert "auto-approve: `1` `would_auto_approve` in 24h" in section
    assert "auto-unfreeze: `1` `would_auto_unfreeze` in 24h" in section


def test_dry_run_subblock_omitted_when_both_dry_runs_off(
    cfg: Config, monkeypatch,
):
    """Default-off byte-identical regression pin (briefing's
    load-bearing safety check): when neither
    `AP2_AUTO_APPROVE_DRY_RUN` nor `AP2_AUTO_UNFREEZE_DRY_RUN` is
    set, the rendered section MUST be byte-identical to TB-228's
    pre-TB-238 output — no `*Dry-run window:*` header, no per-axis
    lines, no trailing blank line introduced by the new code path.
    Pins the omit-on-empty rule that keeps the default operator
    experience untouched by the new readiness signal.

    Fixture mirrors `test_section_present_when_knob_on_and_counters_
    zero` (knob on, no halt, no loop events) so the comparison
    captures the full pre-TB-238 baseline shape.
    """
    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")
    monkeypatch.delenv("AP2_AUTO_APPROVE_DRY_RUN", raising=False)
    monkeypatch.delenv("AP2_AUTO_UNFREEZE_DRY_RUN", raising=False)

    section = render_automation_loop_activity_section(
        cfg, since_event_idx=_previous_status_report_idx(cfg),
    )
    # Both axes' literal tokens absent — the renderer must not have
    # injected the sub-block at all.
    assert "Dry-run window" not in section, section
    assert "would_auto_approve" not in section
    assert "would_auto_unfreeze" not in section
    # The pre-TB-238 baseline ends with the auto-approved bullet
    # (no halt / no skipped → no trailing bullets / no trailing
    # blank line from the new code path). Exact-match pin on the
    # full default-off output.
    expected = (
        "## Automation loop activity\n\n"
        "auto-approve: healthy; auto-unfreeze: healthy\n\n"
        "- 0 tasks auto-approved (0 completed, 0 froze)"
    )
    assert section == expected, (
        f"default-off section must be byte-identical to TB-228 baseline; "
        f"got:\n{section!r}\nexpected:\n{expected!r}"
    )

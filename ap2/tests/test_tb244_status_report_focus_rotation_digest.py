"""TB-244 / TB-342: behavioral pinning for the status-report cron's
`## Focus rotation activity` sub-section + the axis-4 event-type
allowlist extension on `_status_report_should_skip`.

TB-242 (`6704ed52`) shipped the pull surfaces for axis-4
focus-rotation state (`ap2 status` text/JSON + web home). TB-244
closed the push-surface gap: the operator's primary walk-away
channel — the status-report Mattermost post — was silent on
`focus_advanced` / `roadmap_complete`, which contradicted axis 4's
own framing ("walk-away time scales with the operator-declared
roadmap length", goal.md L137-138). A `roadmap_complete` halt at
03:00Z used to wait for the operator's next manual `ap2 status` to
surface; now it lands in the next status-report cron post.

TB-342 then collapsed the multi-focus rotation pointer walk into a
single ideation-exhaustion detector. The `focus_advanced` rotation
event is no longer emitted; only `roadmap_complete` remains.

This module pins five arcs (briefing scope item 5, post-TB-342):

  (a) `_status_report_should_skip` returns False when only a
      `roadmap_complete` event sits in the since-last-report
      window (no other interesting activity).
  (b) Renderer emits the expected line for a `roadmap_complete`
      event with the two-verb hint verbatim
      (`ap2 update-goal` resumes; `ap2 ack roadmap_complete`
      dismisses).
  (c) Renderer suppresses the actionable nag once the operator has
      dismissed THIS episode (surfacing-vs-state split — TB-340).
  (d) Renderer omits the entire sub-block when the window has zero
      `roadmap_complete` events (byte-identical to the no-renderer
      baseline).
  (e) `_STATUS_REPORT_AUTOMATION_INTERESTING_TYPES` frozenset
      contains `roadmap_complete` (TB-342: `focus_advanced` retired).

Plus an end-to-end pin that the routine threads the rendered
section through `state_extras` so the agent forwards it verbatim
(parallel to TB-228's wiring test).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ap2 import automation_status, events
from ap2.config import Config
from ap2.init import init_project
from ap2.status_report import (
    _STATUS_REPORT_AUTOMATION_INTERESTING_TYPES,
    _status_report_should_skip,
    render_focus_rotation_activity_section,
    run_status_report,
)


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    init_project(tmp_path)
    c = Config.load(tmp_path)
    c.ensure_dirs()
    return c


class _NoopSDK:
    """SDK stub: records `query` was called, returns an empty async gen."""

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


def _previous_status_report_idx(cfg: Config) -> int:
    """Locate the inter-report scoping anchor the renderer uses."""
    tail = events.tail(cfg.events_file, 2000)
    return automation_status.find_previous_status_report_idx(tail)


# ===========================================================================
# (a) should-skip returns False when only roadmap_complete in window.
# ===========================================================================


def test_should_skip_false_when_roadmap_complete_in_window(cfg: Config):
    """A `roadmap_complete` event past the last
    `cron_complete name=status-report` means axis 4 halted on
    ideation exhaustion — the cron MUST NOT skip even if no other
    interesting activity happened."""
    events.append(cfg.events_file, "cron_complete", job="status-report")
    events.append(
        cfg.events_file, "roadmap_complete",
        exhausted_count=3, trigger="empty_cycles_heuristic",
    )

    assert _status_report_should_skip(cfg) is False, (
        "roadmap_complete in the window must keep the report from "
        "skipping — operator must see the halt on the next post"
    )


# ===========================================================================
# (b) Renderer emits the roadmap_complete line with the two-verb hint
# verbatim.
# ===========================================================================


def test_renderer_returns_empty_when_no_roadmap_complete(cfg: Config):
    """Zero events in window → renderer returns "" (omit-on-empty
    rule)."""
    section = render_focus_rotation_activity_section(
        cfg, since_event_idx=_previous_status_report_idx(cfg),
    )
    assert section == "", (
        f"section must be omitted when no axis-4 events fired; "
        f"got: {section!r}"
    )


def test_renderer_emits_roadmap_complete_line_with_two_verb_hint(
    cfg: Config,
):
    """A `roadmap_complete` event in the window (notice NOT
    dismissed) → the rendered section carries the parked line with the
    TB-340 / TB-342 two-verb hint: `ap2 update-goal` (extend goal.md
    → resume; the drain handler clears the halt via
    `reset_pointer_on_goal_updated`) and `ap2 ack roadmap_complete`
    (dismiss the notice; ideation stays parked). The pre-TB-342
    `ap2 rewind-focus` verb went away with the multi-focus rotation
    collapse."""
    events.append(
        cfg.events_file, "roadmap_complete",
        exhausted_count=2, trigger="empty_cycles_heuristic",
    )
    section = render_focus_rotation_activity_section(
        cfg, since_event_idx=_previous_status_report_idx(cfg),
    )
    assert section.startswith("## Focus rotation activity"), section
    assert "roadmap_complete: ideation exhausted" in section, section
    assert "ap2 update-goal" in section, section
    assert "ap2 ack roadmap_complete" in section, section
    # TB-342: no rewind-focus reference.
    assert "rewind-focus" not in section, section


def test_renderer_suppresses_nag_when_notice_dismissed(cfg: Config):
    """TB-340 surfacing-vs-state split: once the operator has DISMISSED
    the current exhaustion episode, the digest still emits the
    `ideation exhausted` STATE line but suppresses the actionable
    hint."""
    import json

    (cfg.project_root / "goal.md").write_text(
        "# Goal\n\n## Mission\n\n- m.\n\n"
        "## Current focus: alpha\n\n- a.\n\n"
        "## Current focus: beta\n\n- b.\n\n"
    )
    pointer_path = (
        cfg.project_root / ".cc-autopilot" / "focus_pointer.json"
    )
    pointer_path.parent.mkdir(parents=True, exist_ok=True)
    pointer_path.write_text(json.dumps({
        "schema": 1,
        "empty_cycles": 3,
        "roadmap_complete_ack_idx": 2,  # dismissed THIS episode
        "roadmap_complete_emitted": True,
        "updated_ts": "2026-05-29T00:00:00Z",
    }, indent=2, sort_keys=True) + "\n")

    events.append(
        cfg.events_file, "roadmap_complete",
        exhausted_count=2, trigger="empty_cycles_heuristic",
    )
    section = render_focus_rotation_activity_section(
        cfg, since_event_idx=_previous_status_report_idx(cfg),
    )
    assert "ideation exhausted" in section, section
    assert "notice dismissed" in section, section
    # The actionable hint is suppressed.
    assert "rewind-focus" not in section, section
    assert "to dismiss this notice" not in section, section


# ===========================================================================
# (c) Renderer omits the entire sub-block when no axis-4 events fired.
# ===========================================================================


def test_renderer_byte_identical_to_baseline_when_no_roadmap_complete(
    cfg: Config,
):
    """No `roadmap_complete` events in the window → the renderer
    returns exactly `""` (not a trailing newline, not a heading-only
    string). TB-342: `focus_advanced` no longer fires, so the renderer
    gates on the halt-event count alone."""
    events.append(
        cfg.events_file, "task_complete",
        task="TB-1", status="complete", commit="abc1234",
    )
    section = render_focus_rotation_activity_section(
        cfg, since_event_idx=_previous_status_report_idx(cfg),
    )
    assert section == "", (
        f"renderer must return empty string when no axis-4 events; "
        f"got: {section!r}"
    )


# ===========================================================================
# (d) The interesting-types frozenset carries roadmap_complete.
# ===========================================================================


def test_automation_interesting_types_carries_roadmap_complete():
    """Source-level pin: `roadmap_complete` lands in
    `_STATUS_REPORT_AUTOMATION_INTERESTING_TYPES` so the gate's intent
    is auditable from one symbol. TB-342: `focus_advanced` retired."""
    assert (
        "roadmap_complete" in _STATUS_REPORT_AUTOMATION_INTERESTING_TYPES
    ), _STATUS_REPORT_AUTOMATION_INTERESTING_TYPES
    # The TB-228 entries must remain (regression-pin against an edit
    # that overwrote the frozenset instead of extending it).
    assert (
        "auto_approve_paused" in _STATUS_REPORT_AUTOMATION_INTERESTING_TYPES
    )
    assert (
        "auto_unfreeze_applied" in _STATUS_REPORT_AUTOMATION_INTERESTING_TYPES
    )


# ===========================================================================
# Helper contract: collect_window_focus_rotation.
# ===========================================================================


def test_collect_window_focus_rotation_shape(cfg: Config):
    """The helper's return dict carries every key the renderer
    consumes."""
    activity = automation_status.collect_window_focus_rotation(
        cfg, since_event_idx=-1,
    )
    assert set(activity.keys()) == {
        "focus_advanced",
        "roadmap_complete",
        "total",
    }
    # Empty-events tail → all lists empty, total 0.
    assert activity["focus_advanced"] == []
    assert activity["roadmap_complete"] == []
    assert activity["total"] == 0


def test_collect_window_focus_rotation_carries_roadmap_payload(cfg: Config):
    """The helper preserves the `roadmap_complete` event payload
    field (`exhausted_count`)."""
    events.append(
        cfg.events_file, "roadmap_complete",
        exhausted_count=3, trigger="empty_cycles_heuristic",
    )
    activity = automation_status.collect_window_focus_rotation(
        cfg, since_event_idx=-1,
    )
    assert activity["total"] == 1
    assert len(activity["roadmap_complete"]) == 1
    assert activity["roadmap_complete"][0]["exhausted_count"] == 3


def test_collect_window_focus_rotation_scopes_to_since_event_idx(
    cfg: Config,
):
    """Events at indices <= `since_event_idx` must NOT count toward
    the helper's output."""
    # Pre-window: roadmap_complete BEFORE the previous report → excluded.
    events.append(
        cfg.events_file, "roadmap_complete",
        exhausted_count=2, trigger="empty_cycles_heuristic",
    )
    events.append(cfg.events_file, "cron_complete", job="status-report")
    # In-window: roadmap_complete AFTER the previous report → counted.
    events.append(
        cfg.events_file, "roadmap_complete",
        exhausted_count=3, trigger="empty_cycles_heuristic",
    )

    since_idx = _previous_status_report_idx(cfg)
    activity = automation_status.collect_window_focus_rotation(
        cfg, since_event_idx=since_idx,
    )
    assert activity["total"] == 1
    assert activity["roadmap_complete"][0]["exhausted_count"] == 3


# ===========================================================================
# End-to-end: digest threads through run_status_report → state_extras.
# ===========================================================================


def test_run_status_report_injects_focus_rotation_into_state_extras(
    tmp_path, monkeypatch,
):
    """A `roadmap_complete` event in the inter-report window → the
    routine appends the rendered `## Focus rotation activity`
    sub-block to `state_extras` so the rendered prompt's
    `## Current state` block carries it for the agent to forward
    verbatim."""
    monkeypatch.delenv("AP2_AUTO_APPROVE", raising=False)
    monkeypatch.delenv("AP2_MM_REPORT_CHANNEL", raising=False)
    monkeypatch.delenv("AP2_MM_CHANNELS", raising=False)

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
        cfg.events_file, "roadmap_complete",
        exhausted_count=3, trigger="empty_cycles_heuristic",
    )

    captured: dict[str, list[str]] = {"extras": []}

    def _capture(cfg, name, body, *, state_extras=None):
        captured["extras"] = list(state_extras or [])
        return "stub"

    monkeypatch.setattr("ap2.prompts.build_control_prompt", _capture)

    sdk = _NoopSDK()
    asyncio.run(run_status_report(cfg, sdk, mcp_server=None, trigger="cron"))

    joined = "\n".join(captured["extras"])
    assert "## Focus rotation activity" in joined, joined
    assert "ideation exhausted" in joined, joined
    assert "ap2 ack roadmap_complete" in joined, joined


def test_run_status_report_omits_focus_section_when_window_quiet(
    tmp_path, monkeypatch,
):
    """No axis-4 events in window → the routine does NOT append the
    sub-block to `state_extras`."""
    monkeypatch.delenv("AP2_AUTO_APPROVE", raising=False)
    monkeypatch.delenv("AP2_MM_REPORT_CHANNEL", raising=False)
    monkeypatch.delenv("AP2_MM_CHANNELS", raising=False)

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
    assert "Focus rotation activity" not in joined, (
        f"focus-rotation sub-block must not appear when no axis-4 "
        f"events in window; extras={captured['extras']!r}"
    )


# ===========================================================================
# Structural pins (briefing's grep verifiers).
# ===========================================================================


def test_status_report_module_carries_focus_rotation_heading():
    """`grep -n "Focus rotation" ap2/status_report.py` must match: the
    heading literal lives in `status_report.py`."""
    from ap2 import status_report as _mod
    src = Path(_mod.__file__).read_text()
    assert "Focus rotation activity" in src


def test_status_report_module_references_roadmap_complete():
    """`grep -n '"roadmap_complete"' ap2/status_report.py` must match:
    the event-type token is named explicitly in the module."""
    from ap2 import status_report as _mod
    src = Path(_mod.__file__).read_text()
    assert '"roadmap_complete"' in src


def test_status_report_prompt_carries_focus_rotation_forwarding_contract():
    """The canonical `STATUS_REPORT_PROMPT` body teaches the agent to
    forward the daemon-injected section VERBATIM."""
    from ap2.status_report import STATUS_REPORT_PROMPT
    body = STATUS_REPORT_PROMPT
    assert "Focus rotation activity" in body
    assert "verbatim" in body.lower() or "VERBATIM" in body
    assert "TB-244" in body


def test_status_report_contract_in_prompts_carries_focus_rotation_clause():
    """The `_STATUS_REPORT_CONTRACT` addendum in `ap2/prompts.py` also
    teaches the agent to forward the section verbatim."""
    import inspect
    from ap2 import prompts
    src = inspect.getsource(prompts)
    assert "Focus rotation activity" in src
    assert "TB-244" in src


def test_cron_default_yaml_stub_mentions_focus_rotation():
    """The cron.default.yaml stub is what gets surfaced via
    `ap2 cron list`."""
    cron_yaml = (
        Path(__file__).resolve().parent.parent / "cron.default.yaml"
    )
    text = cron_yaml.read_text()
    assert "Focus rotation activity" in text
    assert "TB-244" in text


def test_ideation_goals_skill_carries_tb244_cross_reference():
    """`grep -n TB-244 skills/ap2-ideation-goals/SKILL.md` must match.

    TB-403 carved the `## Retrospective audit workflow` section — whose
    TB-258 natural-cadence paragraph names TB-244 as a wrap-helper-into-
    status-extras pattern precedent — into the auto-triggered
    `ap2-ideation-goals` skill, making it the only home of that TB-244
    operator-docs cross-reference. The pin follows the reference onto the
    skill (its new home).
    """
    skill = (
        Path(__file__).resolve().parents[2]
        / "skills" / "ap2-ideation-goals" / "SKILL.md"
    )
    assert "TB-244" in skill.read_text()

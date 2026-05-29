"""TB-244: behavioral pinning for the status-report cron's
`## Focus rotation activity` sub-section + the axis-4 event-type
allowlist extension on `_status_report_should_skip`.

TB-242 (`6704ed52`) shipped the pull surfaces for axis-4
focus-rotation state (`ap2 status` text/JSON + web home). TB-244
closes the push-surface gap: the operator's primary walk-away
channel — the status-report Mattermost post — was silent on
`focus_advanced` / `roadmap_complete`, which contradicts axis 4's
own framing ("walk-away time scales with the operator-declared
roadmap length", goal.md L137-138). A `roadmap_complete` halt at
03:00Z used to wait for the operator's next manual `ap2 status` to
surface; now it lands in the next status-report cron post.

This module pins six arcs (briefing scope item 5):

  (a) `_status_report_should_skip` returns False when only a
      `roadmap_complete` event sits in the since-last-report
      window (no other interesting activity).
  (b) `_status_report_should_skip` returns False when only a
      `focus_advanced` event sits in the window.
  (c) Renderer emits the expected lines for a window with 0 / 1 /
      multiple `focus_advanced` events.
  (d) Renderer renders the `roadmap_complete` line with the
      `ap2 ack roadmap_complete` hint verbatim.
  (e) Renderer omits the entire sub-block when the window has zero
      axis-4 events (byte-identical to no-renderer baseline).
  (f) `_STATUS_REPORT_AUTOMATION_INTERESTING_TYPES` frozenset
      contains both `focus_advanced` and `roadmap_complete`.

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
    """SDK stub: records `query` was called, returns an empty async gen.

    Mirrors TB-228's `_NoopSDK`. The routine still needs
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
    have a previous-report anchor. Some tests (`should_skip` tests) add
    their own seed; the renderer/wiring tests use this fixture."""
    events.append(cfg.events_file, "cron_complete", job="status-report")


def _previous_status_report_idx(cfg: Config) -> int:
    """Locate the inter-report scoping anchor the renderer uses."""
    tail = events.tail(cfg.events_file, 2000)
    return automation_status.find_previous_status_report_idx(tail)


# ===========================================================================
# (a) should-skip returns False when only roadmap_complete in window.
# ===========================================================================


def test_should_skip_false_when_roadmap_complete_in_window(
    cfg: Config,
):
    """A `roadmap_complete` event past the last
    `cron_complete name=status-report` means axis 4 halted on
    roadmap exhaustion — the cron MUST NOT skip even if no other
    interesting activity happened. The operator's primary push
    channel must carry the halt signal so walk-away time isn't
    bounded by the manual `ap2 status` cadence."""
    events.append(cfg.events_file, "cron_complete", job="status-report")
    events.append(
        cfg.events_file, "roadmap_complete",
        exhausted_count=3, trigger="pointer_past_last",
    )

    assert _status_report_should_skip(cfg) is False, (
        "roadmap_complete in the window must keep the report from "
        "skipping — operator must see the halt on the next post"
    )


# ===========================================================================
# (b) should-skip returns False when only focus_advanced in window.
# ===========================================================================


def test_should_skip_false_when_focus_advanced_in_window(
    cfg: Config,
):
    """A `focus_advanced` event past the last
    `cron_complete name=status-report` means the daemon rotated to
    the next focus — that's operator-visible state-change the post
    should carry. Pinning the gate's behavior on a lone axis-4
    advance with no other activity."""
    events.append(cfg.events_file, "cron_complete", job="status-report")
    events.append(
        cfg.events_file, "focus_advanced",
        **{"from": "alpha", "to": "beta"},
        trigger="done_when_judge",
        new_index=1,
        total_foci=3,
    )

    assert _status_report_should_skip(cfg) is False, (
        "focus_advanced in the window must keep the report from "
        "skipping — operator must see the rotation on the next post"
    )


# ===========================================================================
# (c) Renderer handles 0 / 1 / multiple focus_advanced events.
# ===========================================================================


def test_renderer_returns_empty_when_no_focus_advanced(cfg: Config):
    """Zero events in window → renderer returns "" (omit-on-empty
    rule pinned at the source). Pin against a refactor that
    accidentally always renders the heading."""
    section = render_focus_rotation_activity_section(
        cfg, since_event_idx=_previous_status_report_idx(cfg),
    )
    assert section == "", (
        f"section must be omitted when no axis-4 events fired; "
        f"got: {section!r}"
    )


def test_renderer_emits_single_focus_advanced_line(cfg: Config):
    """Exactly one `focus_advanced` event in window → the rendered
    section has the heading + one bullet with `from → to (N of M)`
    formatting (1-based position to match TB-242's text render)."""
    events.append(
        cfg.events_file, "focus_advanced",
        **{"from": "alpha", "to": "beta"},
        trigger="done_when_judge",
        new_index=1,
        total_foci=3,
    )
    section = render_focus_rotation_activity_section(
        cfg, since_event_idx=_previous_status_report_idx(cfg),
    )
    assert section.startswith("## Focus rotation activity"), section
    # Renderer translates 0-based event payload to 1-based display.
    assert "- focus_advanced: alpha → beta (2 of 3)" in section, section


def test_renderer_emits_multiple_focus_advanced_lines_in_order(
    cfg: Config,
):
    """Two `focus_advanced` events in the window → the rendered
    section has both bullets in tail order so a multi-advance
    window reads chronologically (operator scans top-to-bottom)."""
    events.append(
        cfg.events_file, "focus_advanced",
        **{"from": "alpha", "to": "beta"},
        trigger="done_when_judge",
        new_index=1,
        total_foci=3,
    )
    events.append(
        cfg.events_file, "focus_advanced",
        **{"from": "beta", "to": "gamma"},
        trigger="empty_cycles_heuristic",
        new_index=2,
        total_foci=3,
    )
    section = render_focus_rotation_activity_section(
        cfg, since_event_idx=_previous_status_report_idx(cfg),
    )
    # Both bullets present.
    assert "- focus_advanced: alpha → beta (2 of 3)" in section
    assert "- focus_advanced: beta → gamma (3 of 3)" in section
    # Tail order: alpha→beta appears BEFORE beta→gamma in the text.
    assert section.index("alpha → beta") < section.index("beta → gamma"), (
        f"multi-advance bullets must render in tail order; got:\n{section}"
    )


# ===========================================================================
# (d) Renderer emits the roadmap_complete line with the ack hint verbatim.
# ===========================================================================


def test_renderer_emits_roadmap_complete_line_with_three_verb_hint(
    cfg: Config,
):
    """A `roadmap_complete` event in the window (notice NOT dismissed)
    → the rendered section carries the parked-ideation line with the
    TB-340 three-verb hint: `ap2 update-goal` (extend → resume on a
    new focus), `ap2 rewind-focus <title>` (resume on an exhausted
    focus), and `ap2 ack roadmap_complete` (dismiss the notice;
    ideation stays parked). All verbs are rendered verbatim so the
    operator can copy-paste them from the Mattermost post. TB-275:
    `roadmap_complete` is an ideation-trigger park, not a dispatch
    halt; TB-340: the ack DISMISSES (it does not resume — resume is a
    pointer move)."""
    events.append(
        cfg.events_file, "roadmap_complete",
        exhausted_count=2, trigger="pointer_past_last",
    )
    section = render_focus_rotation_activity_section(
        cfg, since_event_idx=_previous_status_report_idx(cfg),
    )
    assert section.startswith("## Focus rotation activity"), section
    assert (
        "- roadmap_complete: all foci exhausted — "
        "ideation parked; `ap2 update-goal` to extend the roadmap "
        "(resume on a new focus), `ap2 rewind-focus <title>` to "
        "resume on an exhausted focus, or `ap2 ack roadmap_complete` "
        "to dismiss this notice (ideation stays parked)"
    ) in section, section


def test_renderer_suppresses_nag_when_notice_dismissed(
    cfg: Config,
):
    """TB-340: once the operator has DISMISSED the current exhaustion
    episode (the pointer's `roadmap_complete_ack_idx` == the foci
    count), the digest still emits the `ideation parked` STATE line but
    suppresses the actionable resume/dismiss hint — surfacing-vs-state
    split. A window that both exhausted AND was acked doesn't re-nag."""
    import json

    # Two foci, pointer past the last → exhausted; mark dismissed.
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
        "active_index": 2,
        "active_title": "",
        "empty_cycles": 0,
        "exhausted_titles": [],
        "roadmap_complete_ack_idx": 2,  # dismissed THIS episode
        "roadmap_complete_emitted": True,
        "updated_ts": "2026-05-29T00:00:00Z",
    }, indent=2, sort_keys=True) + "\n")

    events.append(
        cfg.events_file, "roadmap_complete",
        exhausted_count=2, trigger="pointer_past_last",
    )
    section = render_focus_rotation_activity_section(
        cfg, since_event_idx=_previous_status_report_idx(cfg),
    )
    assert (
        "- roadmap_complete: all foci exhausted — "
        "ideation parked (notice dismissed)"
    ) in section, section
    # The actionable hint is suppressed.
    assert "rewind-focus" not in section, section
    assert "to dismiss this notice" not in section, section


def test_renderer_emits_mixed_advance_and_complete_in_order(
    cfg: Config,
):
    """A window with both `focus_advanced` and `roadmap_complete`
    events → renderer emits all bullets in their respective groups
    (advanced lines before completion lines). Pins that a multi-
    event window reads coherently when the daemon emits both on
    the same tick (focus_advanced past last focus → roadmap_complete
    follow-up)."""
    events.append(
        cfg.events_file, "focus_advanced",
        **{"from": "alpha", "to": ""},
        trigger="done_when_judge",
        new_index=2,
        total_foci=2,
    )
    events.append(
        cfg.events_file, "roadmap_complete",
        exhausted_count=2, trigger="pointer_past_last",
    )
    section = render_focus_rotation_activity_section(
        cfg, since_event_idx=_previous_status_report_idx(cfg),
    )
    assert "focus_advanced:" in section
    assert "roadmap_complete:" in section
    # The roadmap_complete bullet renders the ack hint verbatim.
    assert "`ap2 ack roadmap_complete`" in section


# ===========================================================================
# (e) Renderer omits the entire sub-block when no axis-4 events fired
# (byte-identical to no-renderer baseline).
# ===========================================================================


def test_renderer_byte_identical_to_baseline_when_no_axis_4_events(
    cfg: Config,
):
    """No `focus_advanced` / `roadmap_complete` events in the
    window → the renderer returns exactly `""` (not a trailing
    newline, not a heading-only string). Pins the byte-identical
    no-op so TB-228 / TB-238 existing digest tests stay green when
    axis 4 is quiet — this is the load-bearing safety contract for
    the parallel-renderer (option B) shape the briefing recommended.
    """
    # Seed a non-axis-4 event so the helper has tail content to
    # walk — without this the assertion would pass trivially on an
    # empty file.
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
# (f) The interesting-types frozenset carries the two new tokens.
# ===========================================================================


def test_automation_interesting_types_carries_axis_4_tokens():
    """Source-level pin: TB-244's two axis-4 event-type allowlist
    members land in `_STATUS_REPORT_AUTOMATION_INTERESTING_TYPES`
    so the gate's intent is auditable from one symbol. A refactor
    that drops the additions or renames an event type trips here
    (parallel to TB-228's `test_automation_interesting_types_
    constant_pins_event_names`)."""
    assert (
        "focus_advanced" in _STATUS_REPORT_AUTOMATION_INTERESTING_TYPES
    ), _STATUS_REPORT_AUTOMATION_INTERESTING_TYPES
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
    consumes — pin the contract so a refactor that drops a key
    blows the renderer up at runtime (not silently)."""
    activity = automation_status.collect_window_focus_rotation(
        cfg, since_event_idx=-1,
    )
    assert set(activity.keys()) == {
        "focus_advanced",
        "roadmap_complete",
        "total",
    }
    # Empty-events tail → both lists empty, total 0.
    assert activity["focus_advanced"] == []
    assert activity["roadmap_complete"] == []
    assert activity["total"] == 0


def test_collect_window_focus_rotation_carries_payload_fields(cfg: Config):
    """The helper preserves the TB-226 event payload fields
    (`from` / `to` / `new_index` / `total_foci`) so the renderer
    can emit `(N of M)` lines without re-reading goal.md. Pin the
    field names so a daemon-side rename trips this helper too."""
    events.append(
        cfg.events_file, "focus_advanced",
        **{"from": "alpha", "to": "beta"},
        trigger="done_when_judge",
        new_index=1,
        total_foci=3,
    )
    events.append(
        cfg.events_file, "roadmap_complete",
        exhausted_count=3, trigger="pointer_past_last",
    )
    activity = automation_status.collect_window_focus_rotation(
        cfg, since_event_idx=-1,
    )
    assert activity["total"] == 2
    assert len(activity["focus_advanced"]) == 1
    advanced = activity["focus_advanced"][0]
    assert advanced["from"] == "alpha"
    assert advanced["to"] == "beta"
    assert advanced["new_index"] == 1
    assert advanced["total_foci"] == 3
    assert len(activity["roadmap_complete"]) == 1
    assert activity["roadmap_complete"][0]["exhausted_count"] == 3


def test_collect_window_focus_rotation_scopes_to_since_event_idx(
    cfg: Config,
):
    """Events at indices <= `since_event_idx` must NOT count
    toward the helper's output. Pin the inter-report scoping so a
    refactor that drops the slice boundary trips here."""
    # Pre-window: focus_advanced BEFORE the previous report → excluded.
    events.append(
        cfg.events_file, "focus_advanced",
        **{"from": "alpha", "to": "beta"},
        trigger="done_when_judge",
        new_index=1, total_foci=3,
    )
    events.append(cfg.events_file, "cron_complete", job="status-report")
    # In-window: focus_advanced AFTER the previous report → counted.
    events.append(
        cfg.events_file, "focus_advanced",
        **{"from": "beta", "to": "gamma"},
        trigger="done_when_judge",
        new_index=2, total_foci=3,
    )

    since_idx = _previous_status_report_idx(cfg)
    activity = automation_status.collect_window_focus_rotation(
        cfg, since_event_idx=since_idx,
    )
    assert activity["total"] == 1
    assert activity["focus_advanced"][0]["from"] == "beta"


# ===========================================================================
# End-to-end: digest threads through run_status_report → state_extras.
# ===========================================================================


def test_run_status_report_injects_focus_rotation_into_state_extras(
    tmp_path, monkeypatch,
):
    """A `focus_advanced` event in the inter-report window → the
    routine appends the rendered `## Focus rotation activity`
    sub-block to `state_extras` so the rendered prompt's
    `## Current state` block carries it for the agent to forward
    verbatim. Pin the wiring path so a refactor that drops the
    call site (or threads it through a different parameter) trips
    here (parallel to TB-228's
    `test_run_status_report_injects_digest_into_state_extras`).
    """
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
        cfg.events_file, "focus_advanced",
        **{"from": "alpha", "to": "beta"},
        trigger="done_when_judge",
        new_index=1, total_foci=3,
    )
    events.append(
        cfg.events_file, "roadmap_complete",
        exhausted_count=3, trigger="pointer_past_last",
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
    assert "alpha → beta (2 of 3)" in joined, joined
    assert "`ap2 ack roadmap_complete`" in joined, joined


def test_run_status_report_omits_focus_section_when_window_quiet(
    tmp_path, monkeypatch,
):
    """No axis-4 events in window → the routine does NOT append the
    sub-block to `state_extras`. Pins the omit-on-empty rule at the
    wiring level so axis 4 stays as quiet as TB-228's automation
    digest does on a pre-opt-in / quiet window."""
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
    """`grep -n "Focus rotation" ap2/status_report.py` (briefing
    verifier) must match: the heading literal lives in
    `status_report.py` so the rendered section's heading is
    auditable from one place."""
    from ap2 import status_report as _mod
    src = Path(_mod.__file__).read_text()
    assert "Focus rotation activity" in src


def test_status_report_module_references_focus_advanced_and_roadmap_complete():
    """`grep -n '"focus_advanced"' ap2/status_report.py` and
    `grep -n '"roadmap_complete"' ap2/status_report.py` (briefing
    verifiers) must each match: the two event-type tokens are
    named explicitly in the module so the frozenset / docstring /
    renderer contract is traceable from a single grep."""
    from ap2 import status_report as _mod
    src = Path(_mod.__file__).read_text()
    assert '"focus_advanced"' in src
    assert '"roadmap_complete"' in src


def test_status_report_prompt_carries_focus_rotation_forwarding_contract():
    """The canonical `STATUS_REPORT_PROMPT` body teaches the agent
    to forward the daemon-injected section VERBATIM. Pin the
    load-bearing markers so a paraphrase that drops the contract
    trips here (parallel to TB-228's prompt-contract pin)."""
    from ap2.status_report import STATUS_REPORT_PROMPT
    body = STATUS_REPORT_PROMPT
    assert "Focus rotation activity" in body
    # Verbatim forwarding rule (uppercase or lowercase).
    assert "verbatim" in body.lower() or "VERBATIM" in body
    # TB-244 cross-ref so future trims preserve the lineage.
    assert "TB-244" in body


def test_status_report_contract_in_prompts_carries_focus_rotation_clause():
    """The `_STATUS_REPORT_CONTRACT` addendum in `ap2/prompts.py`
    also teaches the agent to forward the section verbatim — both
    halves of the prompt-builder pipeline must carry the contract
    so a refactor that drops one half can't quietly weaken it."""
    import inspect
    from ap2 import prompts
    src = inspect.getsource(prompts)
    assert "Focus rotation activity" in src
    assert "TB-244" in src


def test_cron_default_yaml_stub_mentions_focus_rotation():
    """The cron.default.yaml stub is what gets surfaced via `ap2
    cron list` for a curious operator. The stub mentions the new
    sub-block so the operator following the breadcrumb sees the
    surface exists without reading source."""
    cron_yaml = (
        Path(__file__).resolve().parent.parent / "cron.default.yaml"
    )
    text = cron_yaml.read_text()
    assert "Focus rotation activity" in text
    assert "TB-244" in text


def test_howto_carries_tb244_cross_reference():
    """`grep -n TB-244 ap2/howto.md` (briefing verifier) must
    match: the howto's existing TB-226 focus-rotation section
    cross-references TB-244 so an operator reading howto sees that
    the rotation events also surface on the cron status-report."""
    howto = (
        Path(__file__).resolve().parent.parent / "howto.md"
    )
    assert "TB-244" in howto.read_text()

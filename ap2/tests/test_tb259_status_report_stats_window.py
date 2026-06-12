"""TB-259: behavioral pinning for the status-report cron's
`*Stats window aggregates (<window>):*` sub-block + the
`render_stats_window_section` renderer + the `_STATUS_REPORT_CONTRACT`
clause that teaches the agent to forward the sub-block verbatim.

TB-255 (`891c406`) shipped the PULL surface — the `/stats` HTML +
`/stats.json` dashboard rendering task / bullet / ideation timing +
turn + attempt aggregates over events.jsonl via
`automation_stats.collect_stats(cfg, window_s=...)`. TB-259 closes
the push-vs-pull parity gap: the cron status-report digest (the
operator's primary walk-away PUSH channel) carried no top-line
aggregates summary. Same shape several prior tasks closed on their
axes — TB-241 (dry-run readiness), TB-242 (axis-4 focus-pointer
state), TB-244 (focus_advanced / roadmap_complete digest), TB-245
(validator-judge fail-open), TB-258 (retrospective audit
unreviewed-count).

This module pins five arcs (briefing scope item 5):

  (a) Renderer omits the sub-block entirely when the window's
      task-completion count is zero (omit-on-empty / byte-identical
      to pre-TB-259 baseline).
  (b) Renderer happy-path emits ≥3 lines for a populated stats dict.
  (c) Renderer output mentions both `tasks` and `ideation`
      substrings so the test verifies the briefing's two named
      content axes survive a refactor.
  (d) `_STATUS_REPORT_CONTRACT` in `ap2/prompts.py` enumerates the
      new `stats_window` field (verbatim-forwarding contract pin).
  (e) `run_status_report` `state_extras` plumbing pin — the
      rendered sub-block reaches `build_control_prompt` so the agent
      sees it in the `## Current state` snapshot block.

Plus structural pins for the briefing's grep verifiers
(`grep -q "collect_stats" ap2/status_report.py`,
`grep -q "def render_stats_window_section" ap2/status_report.py`,
`grep -q '"stats_window"' ap2/prompts.py`).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ap2 import events
from ap2.config import Config
from ap2.init import init_project
from ap2.status_report import (
    render_stats_window_section,
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

    Mirrors TB-228 / TB-244 / TB-245 / TB-258's `_NoopSDK`. The
    routine still needs `ClaudeAgentOptions` on the instance even
    though these tests assert against `state_extras` rather than the
    SDK call site.
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
    have a previous-report anchor (mirrors TB-244 / TB-245 / TB-258
    helpers)."""
    events.append(cfg.events_file, "cron_complete", job="status-report")


# ===========================================================================
# (a) Renderer omits the sub-block when task-completion count is zero.
# ===========================================================================


def test_renderer_returns_empty_list_when_zero_completions():
    """Zero-state → renderer returns `[]` (omit-on-empty rule pinned
    at the source). Load-bearing default-off byte-identical
    regression pin so the pre-TB-259 digest stays unchanged on
    quiet/fresh-project windows. Pin against a refactor that
    accidentally always renders the heading. Parallels TB-258's
    `test_renderer_returns_empty_list_when_zero_unreviewed`."""
    stats = {
        "window": "7d",
        "window_s": 604800,
        "computed_at": "2026-05-18T00:00:00Z",
        "tasks": {
            "total": 0,
            "complete_count": 0,
            "failure_count": 0,
            "duration_s": {"count": 0, "avg": 0.0, "p50": 0.0, "p95": 0.0},
        },
        "verifier": {
            "judge_call_count": 0,
            "validator_judge_fail_count": 0,
            "validator_judge_timeout_count": 0,
        },
        "ideation": {"cycle_count": 0, "proposals_recorded": 0},
        "cron": {"jobs": []},
    }
    lines = render_stats_window_section(stats)
    assert lines == [], (
        f"section must be omitted when zero task completions; "
        f"got: {lines!r}"
    )


def test_renderer_returns_empty_list_when_complete_count_missing():
    """Missing `complete_count` key (defensive against a future
    `collect_stats` shape change that renames or drops the field) →
    renderer still returns `[]` rather than tripping on a KeyError.
    Pins the defensive parse so a schema drift doesn't blow up the
    cron post."""
    stats = {
        "window": "7d",
        "tasks": {},
        "verifier": {},
        "ideation": {},
    }
    lines = render_stats_window_section(stats)
    assert lines == []


# ===========================================================================
# (b) Renderer happy-path emits ≥3 lines.
# ===========================================================================


def test_renderer_emits_three_or_more_lines_when_populated():
    """Populated stats dict (≥1 task completion in window) →
    renderer emits ≥3 lines (briefing's `3-5 line bullet sub-block`
    contract). Pin the lower bound so a refactor that collapses to
    a single line / drops bullets trips here."""
    stats = {
        "window": "7d",
        "window_s": 604800,
        "computed_at": "2026-05-18T00:00:00Z",
        "tasks": {
            "total": 12,
            "complete_count": 12,
            "failure_count": 0,
            "duration_s": {
                "count": 12, "avg": 600.0, "p50": 240.0, "p95": 1800.0,
            },
        },
        "verifier": {
            "judge_call_count": 35,
            "validator_judge_fail_count": 0,
            "validator_judge_timeout_count": 1,
        },
        "ideation": {"cycle_count": 4, "proposals_recorded": 8},
        "cron": {"jobs": []},
    }
    lines = render_stats_window_section(stats)
    assert len(lines) >= 3, (
        f"renderer must emit ≥3 lines when populated (briefing "
        f"specifies a 3-5 line bullet sub-block); got {len(lines)}: "
        f"{lines!r}"
    )


def test_renderer_header_carries_window_label():
    """The rendered header carries the `(<window>)` label verbatim so
    the operator scanning the digest sees the inter-report window
    the aggregates are scoped to (parallels TB-245's
    `(24h)` literal in the validator-judge header)."""
    stats = {
        "window": "3d",
        "tasks": {"complete_count": 5, "duration_s": {"p50": 100, "p95": 500}},
        "verifier": {
            "judge_call_count": 10,
            "validator_judge_fail_count": 0,
            "validator_judge_timeout_count": 0,
        },
        "ideation": {"cycle_count": 1, "proposals_recorded": 2},
    }
    lines = render_stats_window_section(stats)
    assert lines, lines
    assert "(3d)" in lines[0], lines[0]
    assert lines[0].startswith("*Stats window aggregates"), lines[0]


# ===========================================================================
# (c) Renderer output mentions both `tasks` and `ideation` substrings.
# ===========================================================================


def test_renderer_output_mentions_tasks_and_ideation_substrings():
    """Briefing scope-test (c): the renderer output must mention
    both `tasks` and `ideation` substrings so the two named content
    axes (task-volume + ideation-cadence) are auditable from a
    single grep on the rendered post. Pin against a refactor that
    drops one axis from the digest."""
    stats = {
        "window": "7d",
        "tasks": {
            "complete_count": 3,
            "duration_s": {"p50": 60.0, "p95": 300.0},
        },
        "verifier": {
            "judge_call_count": 5,
            "validator_judge_fail_count": 0,
            "validator_judge_timeout_count": 0,
        },
        "ideation": {"cycle_count": 1, "proposals_recorded": 2},
    }
    lines = render_stats_window_section(stats)
    joined = "\n".join(lines)
    assert "tasks" in joined, (
        f"renderer output must mention 'tasks' substring; got: {joined!r}"
    )
    assert "ideation" in joined, (
        f"renderer output must mention 'ideation' substring; got: {joined!r}"
    )


# ===========================================================================
# (d) `_STATUS_REPORT_CONTRACT` contract-string pin.
# ===========================================================================


def test_status_report_contract_in_prompts_carries_stats_window_field():
    """The `_STATUS_REPORT_CONTRACT` addendum in `ap2/prompts.py`
    teaches the agent to forward the `*Stats window aggregates
    (<window>):*` sub-block VERBATIM. Pin the load-bearing markers
    so a paraphrase that drops the contract trips here (parallel
    to TB-228 / TB-244 / TB-245 / TB-258 prompt-contract pins).
    The briefing's grep verifier
    (`grep -q '"stats_window"' ap2/prompts.py`) requires the literal
    `"stats_window"` token (quoted) to appear in the file.
    """
    import inspect

    from ap2 import prompts
    src = inspect.getsource(prompts)
    assert "Stats window aggregates" in src
    assert "TB-259" in src
    # Verbatim forwarding rule (uppercase form per the contract style).
    assert "VERBATIM" in src
    # The literal `"stats_window"` token (quoted) must appear in the
    # source — the briefing verifier `grep -q '"stats_window"'
    # ap2/prompts.py` runs against this exact shape.
    assert '"stats_window"' in src, (
        "ap2/prompts.py _STATUS_REPORT_CONTRACT must enumerate the "
        "literal \"stats_window\" field (briefing grep verifier)"
    )


def test_prompts_file_carries_stats_window_token():
    """File-level pin: `grep -q '"stats_window"' ap2/prompts.py`
    (briefing verifier) must match. Parallels TB-258's
    `test_prompts_contract_enumerates_audit_field`."""
    from ap2 import prompts as _mod
    src = Path(_mod.__file__).read_text()
    assert '"stats_window"' in src


# ===========================================================================
# (e) End-to-end: digest threads through run_status_report → state_extras.
# ===========================================================================


def test_run_status_report_injects_stats_window_into_state_extras(
    tmp_path, monkeypatch,
):
    """Task completions in the inter-report window → the routine
    appends the rendered sub-block to `state_extras` so the rendered
    prompt's `## Current state` block carries it for the agent to
    forward verbatim. Pin the wiring path so a refactor that drops
    the call site (or threads it through a different parameter)
    trips here (parallel to TB-244 / TB-245 / TB-258 wiring tests).
    """
    monkeypatch.delenv("AP2_AUTO_APPROVE", raising=False)
    monkeypatch.delenv("AP2_MM_REPORT_CHANNEL", raising=False)
    monkeypatch.delenv("AP2_MM_CHANNELS", raising=False)
    monkeypatch.delenv("AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD", raising=False)

    init_project(tmp_path)
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()
    _seed_active(cfg)
    # task_run_usage + task_complete pair so `collect_stats` sees
    # one populated task completion in the post-seed window. The
    # collect_stats `_build_task_metrics` joins these by task id;
    # both must land AFTER the previous status-report anchor for
    # the renderer's omit-on-empty gate to flip false.
    events.append(
        cfg.events_file, "task_run_usage",
        task="TB-1", run_id="r1",
        duration_s=180.0, num_turns=10, total_cost_usd=0.5,
    )
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
    assert "*Stats window aggregates" in joined, (
        f"stats-window sub-block must appear in state_extras when the "
        f"window has task completions; extras={captured['extras']!r}"
    )
    # The bullet body carries the literal `1 completed` (the seeded
    # task_complete) and the two named content axes the briefing's
    # test (c) requires.
    assert "1 completed" in joined, joined
    assert "tasks" in joined, joined
    assert "ideation" in joined, joined


def test_run_status_report_omits_stats_window_section_when_quiet(
    tmp_path, monkeypatch,
):
    """No task completions in the inter-report window → the routine
    does NOT append the sub-block to `state_extras`. Pins the
    omit-on-empty rule at the wiring level so the stats-window
    sub-block stays as quiet as TB-228 / TB-244 / TB-245 / TB-258
    do on a pre-opt-in / quiet window. Load-bearing default-off
    byte-identical regression pin."""
    monkeypatch.delenv("AP2_AUTO_APPROVE", raising=False)
    monkeypatch.delenv("AP2_MM_REPORT_CHANNEL", raising=False)
    monkeypatch.delenv("AP2_MM_CHANNELS", raising=False)
    monkeypatch.delenv("AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD", raising=False)

    init_project(tmp_path)
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()
    _seed_active(cfg)
    # Seed something interesting so the skip-gate doesn't fire on
    # the routine entry — but NOT a task_complete (which would
    # populate the stats window).
    events.append(
        cfg.events_file, "validator_judge_fail",
        error="non-dict judge response",
    )

    captured: dict[str, list[str]] = {"extras": []}

    def _capture(cfg, name, body, *, state_extras=None):
        captured["extras"] = list(state_extras or [])
        return "stub"

    monkeypatch.setattr("ap2.prompts.build_control_prompt", _capture)

    sdk = _NoopSDK()
    asyncio.run(run_status_report(cfg, sdk, mcp_server=None, trigger="cron"))

    joined = "\n".join(captured["extras"])
    assert "Stats window aggregates" not in joined, (
        f"stats-window sub-block must not appear when no task "
        f"completions in the inter-report window; "
        f"extras={captured['extras']!r}"
    )


# ===========================================================================
# Structural pins (briefing's grep verifiers).
# ===========================================================================


def test_status_report_module_calls_collect_stats():
    """`grep -q "collect_stats" ap2/status_report.py` (briefing
    verifier) must match: `run_status_report` wires in the existing
    `automation_stats.collect_stats` helper directly."""
    from ap2 import status_report as _mod
    src = Path(_mod.__file__).read_text()
    assert "collect_stats" in src, (
        "ap2/status_report.py must reference collect_stats so the "
        "briefing grep verifier matches"
    )


def test_status_report_module_declares_render_stats_window_section():
    """`grep -q "def render_stats_window_section" ap2/status_report.py`
    (briefing verifier) must match: the renderer is declared at
    module level so the wiring + tests can import it directly."""
    from ap2 import status_report as _mod
    src = Path(_mod.__file__).read_text()
    assert "def render_stats_window_section(" in src


def test_howto_carries_tb259_cross_reference():
    """The Stats dashboard section cross-references TB-259 so an
    operator reading the section sees that the aggregates also surface
    on the cron status-report (push surface) — not just `/stats` (pull
    surface). Mirrors TB-245's `test_howto_carries_tb245_cross_reference`
    shape.

    TB-397 carved the Stats dashboard domain into
    `skills/ap2-observability/SKILL.md` (the observability canary skill),
    so this gate follows the content to the skill.
    """
    skill = (
        Path(__file__).resolve().parent.parent.parent
        / "skills/ap2-observability/SKILL.md"
    )
    src = skill.read_text()
    assert "TB-259" in src
    # The TB-259 paragraph lives within the `## Stats dashboard`
    # section, not somewhere structurally unrelated. Anchor on the
    # section heading and a downstream marker to bound the search.
    stats_heading_idx = src.find("## Stats dashboard")
    assert stats_heading_idx >= 0, "Stats dashboard section not found"
    tb259_idx = src.find("TB-259")
    assert tb259_idx > stats_heading_idx, (
        "TB-259 cross-reference must live inside the Stats dashboard "
        "section (after the heading)"
    )

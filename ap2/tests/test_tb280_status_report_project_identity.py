"""TB-280: behavioral pinning for the status-report cron's
project-identity headline + pre-rendered `## Recent task activity`
digest section.

Closes goal.md focus-1's Done-when bullet "identifies tasks by title
+ one-line summary (never bare TB-N alone) and leads with the
project name" — the multi-project operator monitoring 5+ daemons
must be able to read a status-report post and know which project
the post comes from AND what the recent task activity was, without
alt-tabbing to the repo.

Pre-TB-280:
  - `Config` carried no `project_name` field; no `AP2_PROJECT_NAME`
    env knob existed.
  - `STATUS_REPORT_PROMPT`'s headline contract was
    `**Autopilot Status Report** — <now>` (no project identifier).
  - The agent composed bullets of shape "TB-N + 1-line outcome +
    short SHA" from scratch for every terminal task event, forcing
    the operator to translate bare TB-Ns to titles.

This module pins five arcs:

  (1) `Config.project_name` default (= `project_root.name`) +
      `AP2_PROJECT_NAME` override + whitespace-strip + fallback
      when env value is empty.
  (2) `STATUS_REPORT_PROMPT` headline substring carries the
      `**[<project_name>] Autopilot Status Report**` shape (the
      load-bearing substitution target the daemon swaps at
      build-time).
  (3) `render_recent_task_activity_section` shape on a synthetic
      window with all four terminal event types — bullet format,
      title-resolution via `Board.find`, fallback to event summary
      when board lookup misses, outcome rendering per event type.
  (4) `render_recent_task_activity_section` returns "" (omit-on-
      empty) when the window has zero terminal task events — quiet
      windows stay byte-identical to the pre-TB-280 baseline so
      prior digest tests (TB-228 / TB-244 / TB-245 / TB-258 /
      TB-259 / TB-260) continue to pass.
  (5) End-to-end wiring: `run_status_report` threads the section
      into `state_extras` so the agent sees it inside the
      `## Current state` snapshot.

Mirrors the TB-205 / TB-210 env-knob test shape for arc 1 and the
TB-228 / TB-244 / TB-258 / TB-259 / TB-260 digest test shape for
arcs 3-5.
"""
from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest

from ap2 import automation_status, events
from ap2.board import Board
from ap2.config import Config
from ap2.init import init_project
from ap2.status_report import (
    STATUS_REPORT_PROMPT,
    render_recent_task_activity_section,
    run_status_report,
)


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch) -> Config:
    """Standard scaffolded project. `AP2_PROJECT_NAME` is cleared so
    tests opt in to the env override explicitly (the default-resolution
    test asserts on the bare `project_root.name` fallback)."""
    monkeypatch.delenv("AP2_PROJECT_NAME", raising=False)
    init_project(tmp_path)
    c = Config.load(tmp_path)
    c.ensure_dirs()
    return c


class _NoopSDK:
    """SDK stub mirroring TB-228 / TB-244 / TB-258's
    `_NoopSDK` shape — captures `query()` call without actually
    running. Tests assert against `state_extras` (forwarded into the
    prompt builder), NOT against SDK call kwargs."""

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
    """Seed a previous `cron_complete name=status-report` so the
    inter-report-window scoping helper has an anchor, plus one
    `task_complete` so the skip-gate doesn't fire on routine-wiring
    tests."""
    events.append(cfg.events_file, "cron_complete", job="status-report")
    events.append(
        cfg.events_file, "task_complete",
        task="TB-1", status="complete", commit="abc1234",
        summary="seed task",
    )


def _previous_status_report_idx(cfg: Config) -> int:
    """Locate the inter-report scoping anchor — same helper the
    routine uses internally."""
    tail = events.tail(cfg.events_file, 2000)
    return automation_status.find_previous_status_report_idx(tail)


# ===========================================================================
# Arc 1: `Config.project_name` env-knob contract.
# ===========================================================================


def test_config_project_name_defaults_to_project_root_name(
    tmp_path, monkeypatch,
):
    """Happy path: `AP2_PROJECT_NAME` unset → `Config.load` resolves
    `project_name = project_root.name`. For a project rooted at
    `/tmp/.../ap2-proj`, the default identifier is `ap2-proj` — the
    operator's natural project name from the directory layout."""
    monkeypatch.delenv("AP2_PROJECT_NAME", raising=False)
    project_root = tmp_path / "ap2-test-project"
    project_root.mkdir()
    init_project(project_root)
    cfg = Config.load(project_root)

    assert cfg.project_name == "ap2-test-project", (
        "default project_name must be `project_root.name`; got "
        f"{cfg.project_name!r}"
    )


def test_config_project_name_env_override_wins_over_default(
    tmp_path, monkeypatch,
):
    """Happy path: `AP2_PROJECT_NAME="stoch"` → `Config.load` reads the
    env and returns "stoch" as the identifier — wins over the
    `project_root.name` default so a daemon hosting the project under
    a generic-named root (`/tmp/proj`, `/home/user/code/main`) can
    still post with an operator-meaningful identifier."""
    monkeypatch.setenv("AP2_PROJECT_NAME", "stoch")
    project_root = tmp_path / "generic-root"
    project_root.mkdir()
    init_project(project_root)
    cfg = Config.load(project_root)

    assert cfg.project_name == "stoch"


def test_config_project_name_empty_env_falls_back_to_project_root_name(
    tmp_path, monkeypatch,
):
    """Edge contract: `AP2_PROJECT_NAME=""` (empty string) collapses
    to the default. Mirrors the `AP2_MM_TEAM_ID` empty-string-falls-
    back-to-None pattern (TB-210 L586-637) — operator typo /
    accidental clear doesn't render a literal `**[] Autopilot Status
    Report**` headline."""
    monkeypatch.setenv("AP2_PROJECT_NAME", "")
    project_root = tmp_path / "fallback-name"
    project_root.mkdir()
    init_project(project_root)
    cfg = Config.load(project_root)

    assert cfg.project_name == "fallback-name", (
        "empty AP2_PROJECT_NAME must collapse to project_root.name; "
        f"got {cfg.project_name!r}"
    )


def test_config_project_name_whitespace_env_falls_back_to_default(
    tmp_path, monkeypatch,
):
    """Edge contract: `AP2_PROJECT_NAME="   "` (whitespace-only) is
    stripped to "" and falls back to the default. Pin the strip path
    so an accidental space in the env file doesn't render a leading
    space in the bracketed headline."""
    monkeypatch.setenv("AP2_PROJECT_NAME", "   ")
    project_root = tmp_path / "whitespace-name"
    project_root.mkdir()
    init_project(project_root)
    cfg = Config.load(project_root)

    assert cfg.project_name == "whitespace-name"


def test_config_project_name_strips_surrounding_whitespace(
    tmp_path, monkeypatch,
):
    """Edge contract: `AP2_PROJECT_NAME=" foo "` strips to "foo".
    Operator-pasted values often carry trailing whitespace; the strip
    keeps the bracketed headline tight."""
    monkeypatch.setenv("AP2_PROJECT_NAME", " foo ")
    project_root = tmp_path / "any"
    project_root.mkdir()
    init_project(project_root)
    cfg = Config.load(project_root)

    assert cfg.project_name == "foo"


def test_config_module_references_AP2_PROJECT_NAME():
    """Source-grep pin: `ap2/config.py` mentions `AP2_PROJECT_NAME`.
    Mirrors the briefing's Verification grep — a refactor that drops
    the env read flips the grep AND the override test simultaneously,
    surfacing the deliberate change."""
    from ap2 import config as _mod
    src = Path(_mod.__file__).read_text()
    assert "AP2_PROJECT_NAME" in src
    assert "project_name" in src


def test_env_reload_lists_project_name_as_hot_reloadable():
    """A name change shouldn't require a daemon restart — pin
    `AP2_PROJECT_NAME` as a hot-reloadable knob so the next tick's
    `_refresh_tunable_config_fields` rewrites `cfg.project_name`
    from the freshly-sourced env."""
    from ap2.env_reload import HOT_RELOADABLE_KNOBS, _TUNABLE_CONFIG_FIELDS

    assert "AP2_PROJECT_NAME" in HOT_RELOADABLE_KNOBS
    assert _TUNABLE_CONFIG_FIELDS.get("project_name") == "AP2_PROJECT_NAME"


# ===========================================================================
# Arc 2: STATUS_REPORT_PROMPT carries the bracketed-headline contract.
# ===========================================================================


def test_status_report_prompt_headline_carries_bracketed_project_name():
    """The canonical `STATUS_REPORT_PROMPT` body teaches the agent
    that the headline shape is `**[<project_name>] Autopilot Status
    Report** — <now>` — the `<project_name>` literal is the load-
    bearing substitution target the daemon swaps at build-time.

    Pin the substring so a paraphrase that drops the bracket
    structure or the substitution target trips here. Pre-TB-280 the
    headline shape was `**Autopilot Status Report** — <now>` (no
    bracket, no substitution); this assertion catches any silent
    regression to the old shape."""
    body = STATUS_REPORT_PROMPT
    assert "**[<project_name>] Autopilot Status Report**" in body, (
        "headline contract must lead with `**[<project_name>] "
        "Autopilot Status Report**`; got body without the bracketed "
        "substitution token"
    )
    # TB-280 cross-ref so future trims preserve the lineage.
    assert "TB-280" in body


def test_status_report_prompt_references_recent_task_activity_section():
    """The prompt body documents the `## Recent task activity`
    section the daemon now pre-renders. Pin the heading literal +
    the verbatim-forwarding contract so the agent stops composing
    bare TB-N bullets for events the daemon already pre-rendered."""
    body = STATUS_REPORT_PROMPT
    assert "Recent task activity" in body
    # Verbatim-forwarding contract (same shape TB-228 / TB-244 use).
    assert "VERBATIM" in body or "verbatim" in body.lower()


def test_status_report_module_references_render_recent_task_activity():
    """Source-grep pin: `ap2/status_report.py` mentions both the
    renderer name and the section heading. Mirrors the briefing's
    Verification grep so a rename of either trips the pin."""
    from ap2 import status_report as _mod
    src = Path(_mod.__file__).read_text()
    assert "render_recent_task_activity" in src
    assert "Recent task activity" in src
    # `project_name` must appear on the prompt-build path too.
    assert "project_name" in src


# ===========================================================================
# Arc 3: render_recent_task_activity_section bullet shape + title
# resolution.
# ===========================================================================


def test_section_renders_bullet_per_terminal_event_with_titles(
    cfg: Config,
):
    """A window with `task_complete` + `verification_failed` +
    `retry_exhausted` → the renderer emits one bullet per event in
    tail order, each shaped `- **TB-N** — <title>: <outcome>`. The
    title is resolved via `Board.find`; tasks present on the board
    surface their canonical title."""
    # Seed the board with two tasks whose titles the renderer will
    # resolve via `Board.find` lookup.
    board = Board.load(cfg.tasks_file)
    board.add("Complete", task_id="TB-300", title="Title for TB-300")
    board.add("Backlog", task_id="TB-301", title="Title for TB-301")
    board.save()

    events.append(cfg.events_file, "cron_complete", job="status-report")
    events.append(
        cfg.events_file, "task_complete",
        task="TB-300", status="complete", commit="abc1234567",
        summary="agent composed summary",
    )
    events.append(
        cfg.events_file, "verification_failed",
        task="TB-301", kind="per_task", overall="fail",
        criteria=[], duration_s=42.0,
    )
    events.append(
        cfg.events_file, "retry_exhausted",
        task="TB-301", attempts=3, last_status="blocked",
    )

    section = render_recent_task_activity_section(
        cfg, since_event_idx=_previous_status_report_idx(cfg),
    )
    # Section heading is the load-bearing string both the prompt
    # contract and the verification grep pin on.
    assert section.startswith("## Recent task activity"), section
    # Three bullets — one per terminal event.
    bullet_lines = [
        ln for ln in section.splitlines() if ln.startswith("- ")
    ]
    assert len(bullet_lines) == 3, (
        f"expected 3 bullets (one per terminal event); got "
        f"{len(bullet_lines)}: {bullet_lines!r}"
    )
    # Title from board lookup is rendered after the TB-N + em-dash.
    assert "**TB-300** — Title for TB-300:" in section
    assert "**TB-301** — Title for TB-301:" in section
    # task_complete outcome carries `<status> (<short-sha>)`.
    assert "complete (abc1234)" in section
    # verification_failed outcome carries the kind.
    assert "verification_failed (per_task)" in section
    # retry_exhausted outcome carries the attempts + last_status.
    assert "retry_exhausted (3 attempts, last=blocked)" in section


def test_section_falls_back_to_event_summary_when_board_lookup_misses(
    cfg: Config,
):
    """The briefing's title-resolution contract: `Board.find(task_id)
    .title` first, with fallback to the event's `summary` field on
    lookup miss. Pin the fallback so a task that was deleted /
    renamed / never landed on the board still renders an operator-
    readable identifier instead of a bare TB-N."""
    # No board entry for TB-999 — `Board.find` will miss.
    events.append(cfg.events_file, "cron_complete", job="status-report")
    events.append(
        cfg.events_file, "task_complete",
        task="TB-999", status="complete", commit="def4567",
        summary="first line of the agent summary\nsecond line ignored",
    )

    section = render_recent_task_activity_section(
        cfg, since_event_idx=_previous_status_report_idx(cfg),
    )
    # The first non-empty line of `summary` is used as the title
    # fallback.
    assert "first line of the agent summary" in section
    # Defense: the second line should be stripped (one-line outcome
    # rendering, not multi-line).
    assert "second line ignored" not in section


def test_section_renders_placeholder_when_no_title_or_summary(
    cfg: Config,
):
    """Edge contract: task absent from board AND no `summary` field
    on the event → render `(title unavailable)` placeholder. A stable
    marker is better than rendering a bare TB-N + colon and re-
    introducing the very ambiguity the section closes."""
    events.append(cfg.events_file, "cron_complete", job="status-report")
    events.append(
        cfg.events_file, "verification_failed",
        task="TB-888", kind="per_task", overall="fail",
        # NO summary field — verification_failed events don't carry one.
    )

    section = render_recent_task_activity_section(
        cfg, since_event_idx=_previous_status_report_idx(cfg),
    )
    assert "**TB-888** — (title unavailable):" in section


def test_section_skips_events_without_task_id(cfg: Config):
    """A terminal event missing the `task` field is skipped rather
    than emitting a `**?** — …` line. Defense for upstream-malformed
    events that lack a task identifier — the bullet would be
    unactionable for the operator."""
    events.append(cfg.events_file, "cron_complete", job="status-report")
    # Malformed event — no `task` field.
    events.append(
        cfg.events_file, "task_complete",
        status="complete", commit="aaaa111",
        summary="malformed event with no task field",
    )
    events.append(
        cfg.events_file, "task_complete",
        task="TB-700", status="complete", commit="bbbb222",
        summary="well-formed event",
    )

    section = render_recent_task_activity_section(
        cfg, since_event_idx=_previous_status_report_idx(cfg),
    )
    # Only the well-formed event renders.
    bullet_lines = [
        ln for ln in section.splitlines() if ln.startswith("- ")
    ]
    assert len(bullet_lines) == 1, (
        f"malformed event must be skipped; got {bullet_lines!r}"
    )
    assert "TB-700" in section
    assert "malformed" not in section


def test_section_renders_task_complete_without_commit(cfg: Config):
    """`task_complete` with empty `commit` (status=blocked /
    incomplete agents commit nothing) → outcome renders just the
    status, no parenthetical SHA. Pin the no-SHA branch."""
    board = Board.load(cfg.tasks_file)
    board.add("Backlog", task_id="TB-500", title="Blocked Title")
    board.save()

    events.append(cfg.events_file, "cron_complete", job="status-report")
    events.append(
        cfg.events_file, "task_complete",
        task="TB-500", status="blocked", commit="",
        summary="agent reported blocked, no commit",
    )

    section = render_recent_task_activity_section(
        cfg, since_event_idx=_previous_status_report_idx(cfg),
    )
    assert "**TB-500** — Blocked Title: blocked" in section
    # No parenthetical SHA when commit is empty.
    assert "blocked (" not in section


# ===========================================================================
# Arc 4: omit-on-empty regression pin (load-bearing).
# ===========================================================================


def test_section_absent_when_window_has_zero_terminal_task_events(
    cfg: Config,
):
    """Pre-TB-280 baseline preservation: when no terminal task event
    landed in the inter-report window, the renderer returns "" so a
    quiet window stays byte-identical to the pre-TB-280 digest. This
    is the load-bearing regression pin that lets the prior TB-228 /
    TB-244 / TB-245 / TB-258 / TB-259 / TB-260 axis-parity tests
    continue to pass when nothing task-related happened.

    A refactor that flips the omit-on-empty rule to "always render"
    would mean every fresh / quiet project starts emitting a noise
    section.
    """
    events.append(cfg.events_file, "cron_complete", job="status-report")
    # No terminal task events — only a non-terminal event.
    events.append(
        cfg.events_file, "task_start",
        task="TB-1", run_id="x",
    )
    events.append(
        cfg.events_file, "ideation_skipped",
        reason="focus_exhausted", focus_count=1,
    )

    section = render_recent_task_activity_section(
        cfg, since_event_idx=_previous_status_report_idx(cfg),
    )
    assert section == "", (
        f"section must be omitted on a window with zero terminal "
        f"task events; got: {section!r}"
    )


def test_section_absent_when_terminal_event_precedes_window_boundary(
    cfg: Config,
):
    """Window-scoping pin: a terminal event that landed BEFORE the
    previous `cron_complete name=status-report` event must not
    contribute (it was already digested in the prior report).
    Catches a refactor that scopes the section to the full tail
    instead of post-boundary events."""
    # Pre-boundary terminal event — should NOT render.
    events.append(
        cfg.events_file, "task_complete",
        task="TB-100", status="complete", commit="old1234",
        summary="should not render — pre-boundary",
    )
    # Window boundary.
    events.append(cfg.events_file, "cron_complete", job="status-report")
    # No post-boundary terminal events.

    section = render_recent_task_activity_section(
        cfg, since_event_idx=_previous_status_report_idx(cfg),
    )
    assert section == "", (
        "pre-boundary event must not contribute to the post-boundary "
        f"window; got: {section!r}"
    )


# ===========================================================================
# Arc 5: end-to-end wiring through run_status_report.
# ===========================================================================


def test_run_status_report_threads_section_into_state_extras(
    tmp_path, monkeypatch,
):
    """End-to-end: the routine appends the rendered section to
    `state_extras` when terminal task events sit in the window, so
    the agent forwards it verbatim into the post. Pin the wiring
    level (mirrors TB-228 / TB-244 / TB-258 / TB-259 / TB-260's
    parallel wiring tests)."""
    monkeypatch.delenv("AP2_MM_REPORT_CHANNEL", raising=False)
    monkeypatch.delenv("AP2_MM_CHANNELS", raising=False)
    monkeypatch.delenv("AP2_PROJECT_NAME", raising=False)

    init_project(tmp_path)
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()

    board = Board.load(cfg.tasks_file)
    board.add("Complete", task_id="TB-401", title="Wired Title")
    board.save()

    events.append(cfg.events_file, "cron_complete", job="status-report")
    events.append(
        cfg.events_file, "task_complete",
        task="TB-401", status="complete", commit="dead123beef",
        summary="agent's completion summary",
    )

    captured: dict[str, list[str]] = {"extras": []}

    def _capture(cfg, name, body, *, state_extras=None):
        captured["extras"] = list(state_extras or [])
        return "stub"

    monkeypatch.setattr("ap2.prompts.build_control_prompt", _capture)

    sdk = _NoopSDK()
    asyncio.run(run_status_report(cfg, sdk, mcp_server=None, trigger="cron"))

    joined = "\n".join(captured["extras"])
    assert "## Recent task activity" in joined, (
        f"section must appear in state_extras; got "
        f"extras={captured['extras']!r}"
    )
    assert "**TB-401** — Wired Title:" in joined
    assert "complete (dead123)" in joined


def test_run_status_report_omits_section_when_window_quiet(
    tmp_path, monkeypatch,
):
    """End-to-end: when no terminal task events landed in the window,
    the routine does NOT append the digest to `state_extras` — the
    quiet-window baseline stays byte-identical to pre-TB-280. Pins
    the omit-on-empty rule at the wiring level."""
    monkeypatch.delenv("AP2_MM_REPORT_CHANNEL", raising=False)
    monkeypatch.delenv("AP2_MM_CHANNELS", raising=False)
    monkeypatch.delenv("AP2_PROJECT_NAME", raising=False)

    init_project(tmp_path)
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()

    # Activity needed to bypass the skip-gate, but NOT a terminal
    # task event — `auto_approve_paused` is in the interesting set
    # but doesn't trip the new section.
    events.append(cfg.events_file, "cron_complete", job="status-report")
    events.append(
        cfg.events_file, "auto_approve_paused",
        task="TB-501", threshold=3, reason="three consecutive freezes",
    )

    captured: dict[str, list[str]] = {"extras": []}

    def _capture(cfg, name, body, *, state_extras=None):
        captured["extras"] = list(state_extras or [])
        return "stub"

    monkeypatch.setattr("ap2.prompts.build_control_prompt", _capture)

    sdk = _NoopSDK()
    asyncio.run(run_status_report(cfg, sdk, mcp_server=None, trigger="cron"))

    joined = "\n".join(captured["extras"])
    assert "## Recent task activity" not in joined, (
        "section must be omitted when no terminal task events landed; "
        f"got extras={captured['extras']!r}"
    )


def test_run_status_report_substitutes_project_name_into_prompt_body(
    tmp_path, monkeypatch,
):
    """End-to-end: the daemon swaps `<project_name>` in the prompt
    body for `cfg.project_name` before calling
    `build_control_prompt`. Pin the substitution so a refactor that
    drops the `.replace(...)` call surfaces — without this, the
    agent would post the literal token `<project_name>` instead of
    the project identifier."""
    monkeypatch.setenv("AP2_PROJECT_NAME", "test-project-x")
    monkeypatch.delenv("AP2_MM_REPORT_CHANNEL", raising=False)
    monkeypatch.delenv("AP2_MM_CHANNELS", raising=False)

    init_project(tmp_path)
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()
    _seed_active(cfg)

    captured: dict[str, str] = {"body": ""}

    def _capture(cfg, name, body, *, state_extras=None):  # noqa: ARG001
        captured["body"] = body
        return "stub"

    monkeypatch.setattr("ap2.prompts.build_control_prompt", _capture)

    sdk = _NoopSDK()
    asyncio.run(run_status_report(cfg, sdk, mcp_server=None, trigger="cron"))

    body = captured["body"]
    # The literal substitution target must be gone.
    assert "<project_name>" not in body, (
        "the daemon must substitute `<project_name>` before passing "
        "the prompt body to build_control_prompt; the literal token "
        "leaked through"
    )
    # The substituted name must appear in the bracketed headline shape.
    assert "**[test-project-x] Autopilot Status Report**" in body, (
        "post-substitution body must carry the bracketed project-"
        f"identity headline; got body excerpt: {body[:400]!r}"
    )


# ===========================================================================
# Source-anchor pins mirroring the briefing's Verification greps.
# ===========================================================================


def test_briefing_verification_greps_match():
    """Mirror the briefing's `## Verification` greps in test form so
    a refactor that violates the structural pins (Config field,
    env-knob, renderer/heading wiring) surfaces here as a clean test
    failure rather than only at `ap2 verify` time."""
    config_src = (
        Path(__file__).resolve().parent.parent / "config.py"
    ).read_text()
    status_report_src = (
        Path(__file__).resolve().parent.parent / "status_report.py"
    ).read_text()

    assert "project_name" in config_src, (
        "Verification grep `grep -q project_name ap2/config.py` must "
        "match — Config field absent or renamed"
    )
    assert "AP2_PROJECT_NAME" in config_src, (
        "Verification grep `grep -q AP2_PROJECT_NAME ap2/config.py` "
        "must match — env knob handling absent or renamed"
    )
    assert "project_name" in status_report_src, (
        "Verification grep `grep -q project_name ap2/status_report.py` "
        "must match — prompt + renderer don't reference the field"
    )
    assert (
        "render_recent_task_activity" in status_report_src
        or "Recent task activity" in status_report_src
    ), (
        "Verification grep `grep -Eq "
        "render_recent_task_activity|Recent task activity` must match"
    )

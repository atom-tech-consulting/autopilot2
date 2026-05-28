"""TB-302 regression pin: the roadmap-complete branch of
`ap2/components/focus_advance/__init__.py:_maybe_advance_focus` must
NOT append a `Roadmap complete: ...` bullet to
`.cc-autopilot/ideation_state.md`.

TB-313 (axis 5) relocated the module body from the flat path
`ap2/focus_advance.py` into the subpackage
`ap2/components/focus_advance/__init__.py`; the source-level pins
below import via `from ap2.components import focus_advance` so the
docstring / source-line / docstring-TB-302 checks track the new
location.

Why this matters (the two bugs the prior bullet write caused):

  (1) Priming leak past the scrub. The ideation-cycle's post-write
      scrub of exhaustion-asserting sentences from
      `ideation_state.md` runs INSIDE `_run_ideation` after the
      agent's `ideation_state_write` MCP call. The daemon's
      `_append_decisions_needed_bullet` ran in
      `_maybe_advance_focus` AFTER `_run_ideation` returned —
      bypassing the scrub. The bullet was exactly the
      verdict-language pattern the scrub catches (asserts
      exhaustion, names conditions of exhaustion, claims the
      operator should extend the roadmap), so the bypass directly
      undid the priming guarantee the scrub was designed to
      provide. The next ideation cycle (after operator extends the
      roadmap) was free to read the stale exhaustion-asserting
      sentence as authoritative context and re-emit
      pro-forma "all signals satisfied" verdicts.

  (2) Uncommitted working-tree drift. The
      `ap2/daemon.py:_changed_state_paths` snapshot diff only
      captures edits that happen DURING `_run_ideation`. Edits
      from `_maybe_advance_focus` happened outside that window, so
      they never rode along in a `state: ideation` commit. The
      working tree diverged from committed state over time;
      `ap2 rollback`'s "walk back N commits" semantics doesn't
      restore working-tree parity; `git status` was noisy.

The fix (TB-302) drops the single
`_append_decisions_needed_bullet` call from the
`roadmap_complete_emitted` branch. The roadmap-complete signal is
already surfaced redundantly via five other channels:
  (a) `focus_advanced` / `roadmap_complete` events in
      `events.jsonl`,
  (b) `focus_pointer.json` (`active_index past end`,
      `exhausted_titles`, `roadmap_complete_emitted=true`,
      empty `active_title`),
  (c) `ap2 status`'s focus line
      (`focus: ROADMAP_COMPLETE — ideation parked;
      `ap2 update-goal` to resume or `ap2 ack roadmap_complete`
      to dismiss`), derived from `focus_pointer.json` via
      `goal.roadmap_exhausted`,
  (d) the web home page's
      `Focus — ROADMAP_COMPLETE` header, derived from the same
      pointer, and
  (e) the TB-244 focus-rotation digest in the cron
      `status_report` post.

Other callers of `_append_decisions_needed_bullet` STAY
unchanged — they surface conditions that are NOT redundantly
signaled elsewhere:
  - The kill-switch branch in the SAME `_maybe_advance_focus`
    (`AP2_FOCUS_AUTO_ADVANCE_DISABLED=1` set when criteria would
    advance) — operator-killed-but-criteria-met has no equivalent
    focus-line surface; the operator needs the bullet to know to
    `ap2 update-goal` manually.
  - `ap2/auto_unfreeze.py`'s daily-cap halt.
  - `ap2/daemon.py`'s TB-224 task_error halt.

Behavioral pins covered here:

  - When `_maybe_advance_focus` advances past the last focus, the
    `roadmap_complete` event IS emitted (events.jsonl has the
    entry).
  - `pointer["roadmap_complete_emitted"]` IS set to True.
  - `ideation_state.md` is NOT modified — no
    `Roadmap complete:` bullet appended; if the file exists
    before the call, its bytes are unchanged across the call.
  - Subsequent ticks (still past-last-focus,
    `roadmap_complete_emitted=true`) emit NO duplicate
    `roadmap_complete` events and don't modify
    `ideation_state.md`.
  - The kill-switch path
    (`AP2_FOCUS_AUTO_ADVANCE_DISABLED=1` set when criteria
    would advance) DOES still emit a
    `## Decisions needed from operator` bullet via
    `_append_decisions_needed_bullet` — that's a different code
    path with a different signal (criteria met but advance
    blocked).
  - Source-level pin: the `_append_decisions_needed_bullet` call
    no longer appears within 5 lines of a
    `roadmap_complete_emitted` reference in
    `ap2/components/focus_advance/__init__.py` (the Verification
    bullet's grep surface).
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from ap2 import daemon, events, goal
from ap2.components import focus_advance
from ap2.config import Config
from ap2.init import init_project


# ===========================================================================
# Fixtures
# ===========================================================================


_GOAL_MD_TEMPLATE = (
    "# Project Goals\n\n"
    "## Mission\n\n"
    "Drive the project toward end-to-end automation.\n\n"
    "## Done when\n\n"
    "- An operator can point ap2 at a fresh project and walk away.\n\n"
    "{focus_section}"
    "## Non-goals\n\n"
    "- something out of scope.\n"
)


def _make_goal_md(focus_section: str) -> str:
    return _GOAL_MD_TEMPLATE.format(focus_section=focus_section)


def _write_goal_with_foci(cfg: Config, *titles: str) -> None:
    sections = "".join(
        f"## Current focus: {t}\n\nBody for {t}.\n\n" for t in titles
    )
    (cfg.project_root / "goal.md").write_text(_make_goal_md(sections))


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    """Project root with the standard ap2 init layout + a single-focus
    goal.md. Tests rewrite goal.md as needed."""
    init_project(tmp_path)
    (tmp_path / "goal.md").write_text(
        _make_goal_md("## Current focus: alpha\n\nAlpha body.\n\n")
    )
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()
    return cfg


def _ideation_state_path(cfg: Config) -> Path:
    return cfg.project_root / ".cc-autopilot" / "ideation_state.md"


# ===========================================================================
# Behavioral pins
# ===========================================================================


def test_roadmap_complete_emits_event_and_sets_pointer_flag(cfg, monkeypatch):
    """When the pointer crosses the last focus, the advance pass
    emits exactly one `roadmap_complete` event and sets
    `pointer['roadmap_complete_emitted']` to True. Pins that the
    side effects we KEEP still happen — only the bullet write was
    removed."""
    monkeypatch.delenv("AP2_FOCUS_AUTO_ADVANCE_DISABLED", raising=False)
    _write_goal_with_foci(cfg, "alpha")
    pointer = goal.load_pointer(cfg)
    pointer["active_index"] = 1  # past the only focus
    goal.save_pointer(cfg, pointer)

    asyncio.run(daemon._maybe_advance_focus(cfg, sdk=None))

    tail = events.tail(cfg.events_file, 50)
    rc = [e for e in tail if e.get("type") == "roadmap_complete"]
    assert len(rc) == 1, "roadmap_complete event must fire exactly once"
    assert rc[0]["exhausted_count"] == 1
    assert rc[0]["trigger"] == "pointer_past_last"

    loaded = goal.load_pointer(cfg)
    assert loaded["roadmap_complete_emitted"] is True
    assert goal.roadmap_exhausted(cfg) is True


def test_roadmap_complete_does_not_create_ideation_state(cfg, monkeypatch):
    """When `ideation_state.md` does NOT exist before the advance
    pass, the roadmap-complete branch must NOT create it. Pre-TB-302
    the daemon would call `_append_decisions_needed_bullet` which
    creates the file with a `# Ideation State` header + a
    `## Decisions needed from operator` section + the
    `Roadmap complete: ...` bullet."""
    monkeypatch.delenv("AP2_FOCUS_AUTO_ADVANCE_DISABLED", raising=False)
    _write_goal_with_foci(cfg, "alpha")
    pointer = goal.load_pointer(cfg)
    pointer["active_index"] = 1
    goal.save_pointer(cfg, pointer)

    ideation_state = _ideation_state_path(cfg)
    # Sanity: fresh init may or may not seed the file; remove it if
    # present so we can pin the no-create behavior unambiguously.
    if ideation_state.exists():
        ideation_state.unlink()
    assert not ideation_state.exists()

    asyncio.run(daemon._maybe_advance_focus(cfg, sdk=None))

    assert not ideation_state.exists(), (
        "TB-302: the roadmap-complete branch must not create "
        "ideation_state.md. The pointer-driven `ap2 status` focus "
        "line carries the operator-facing signal; the daemon-owned "
        "bullet write was the priming-leak surface this fix "
        "eliminated."
    )


def test_roadmap_complete_does_not_modify_existing_ideation_state(cfg, monkeypatch):
    """When `ideation_state.md` exists before the advance pass with
    pre-existing content, the roadmap-complete branch must leave
    its bytes unchanged. This is the single-writer invariant the
    fix restores: only the ideation agent
    (`do_ideation_state_write`) and halt-style callers in
    `_run_ideation`'s window write to the file; the focus-advance
    pass does not."""
    monkeypatch.delenv("AP2_FOCUS_AUTO_ADVANCE_DISABLED", raising=False)
    _write_goal_with_foci(cfg, "alpha")
    pointer = goal.load_pointer(cfg)
    pointer["active_index"] = 1
    goal.save_pointer(cfg, pointer)

    ideation_state = _ideation_state_path(cfg)
    ideation_state.parent.mkdir(parents=True, exist_ok=True)
    pre_content = (
        "# Ideation State\n\n"
        "## Active assessment\n\n"
        "Pre-existing agent-authored body.\n"
    )
    ideation_state.write_text(pre_content)
    pre_bytes = ideation_state.read_bytes()

    asyncio.run(daemon._maybe_advance_focus(cfg, sdk=None))

    post_bytes = ideation_state.read_bytes()
    assert post_bytes == pre_bytes, (
        "TB-302: the roadmap-complete branch must not modify "
        "ideation_state.md. Pre/post bytes must match exactly."
    )
    text = ideation_state.read_text()
    assert "Roadmap complete" not in text
    assert "Decisions needed from operator" not in text


def test_subsequent_ticks_dont_re_emit_or_modify_state(cfg, monkeypatch):
    """After the first detection sets
    `roadmap_complete_emitted=true`, subsequent calls to
    `_maybe_advance_focus` short-circuit: no duplicate
    `roadmap_complete` event, no further write to
    `ideation_state.md`."""
    monkeypatch.delenv("AP2_FOCUS_AUTO_ADVANCE_DISABLED", raising=False)
    _write_goal_with_foci(cfg, "alpha")
    pointer = goal.load_pointer(cfg)
    pointer["active_index"] = 1
    goal.save_pointer(cfg, pointer)

    # First tick: emits the event, sets the flag.
    asyncio.run(daemon._maybe_advance_focus(cfg, sdk=None))

    ideation_state = _ideation_state_path(cfg)
    snapshot_exists = ideation_state.exists()
    snapshot_bytes = (
        ideation_state.read_bytes() if snapshot_exists else None
    )

    # Subsequent ticks: short-circuit on the flag.
    for _ in range(3):
        asyncio.run(daemon._maybe_advance_focus(cfg, sdk=None))

    tail = events.tail(cfg.events_file, 50)
    rc = [e for e in tail if e.get("type") == "roadmap_complete"]
    assert len(rc) == 1, (
        "roadmap_complete must fire exactly once across multiple "
        "ticks past the last focus"
    )

    # ideation_state.md state is identical pre/post the subsequent
    # ticks (whether or not the file existed at the snapshot point).
    if snapshot_exists:
        assert ideation_state.read_bytes() == snapshot_bytes
    else:
        assert not ideation_state.exists()


def test_kill_switch_path_still_writes_decisions_needed_bullet(cfg, monkeypatch):
    """The kill-switch branch (`AP2_FOCUS_AUTO_ADVANCE_DISABLED=1`
    set when criteria would advance) DOES still write a
    `## Decisions needed from operator` bullet via
    `_append_decisions_needed_bullet`. TB-302 scoped the removal
    to the roadmap-complete branch ONLY — operator-killed-but-
    criteria-met has no naturally-observable focus-line surface, so
    the bullet is the only push channel.

    Setup mirrors `test_auto_advance_disabled_short_circuits` in
    `test_tb226_focus_rotation.py`: enough empty cycles to trip
    the heuristic, kill-switch on, multi-focus goal.md so the
    advance attempt is in-bounds (the kill-switch branch only
    fires for in-bounds advance attempts, never for the
    past-last-focus exhaustion branch)."""
    monkeypatch.setenv("AP2_FOCUS_ADVANCE_EMPTY_CYCLES", "1")
    monkeypatch.setenv("AP2_FOCUS_AUTO_ADVANCE_DISABLED", "1")
    _write_goal_with_foci(cfg, "alpha", "beta")
    # Emit one full empty ideation cycle so the heuristic counter
    # sees it (cycle-grouped: entry + exit, no proposal).
    events.append(cfg.events_file, "ideation_empty_board", cooldown_s=0)
    events.append(cfg.events_file, "ideation_complete", summary="empty cycle")

    asyncio.run(daemon._maybe_advance_focus(cfg, sdk=None))

    pointer = goal.load_pointer(cfg)
    assert pointer["active_index"] == 0, (
        "kill-switch must block the in-bounds advance"
    )

    ideation_state = _ideation_state_path(cfg)
    assert ideation_state.exists(), (
        "kill-switch path must create ideation_state.md when absent"
    )
    text = ideation_state.read_text()
    assert "Decisions needed from operator" in text
    assert "AP2_FOCUS_AUTO_ADVANCE_DISABLED" in text


def test_kill_switch_helper_import_retained():
    """Source-level pin: the `_append_decisions_needed_bullet`
    import in `ap2/components/focus_advance/__init__.py` is retained
    because the kill-switch branch below still uses it. Removing the
    import on the assumption that the only caller was the
    roadmap-complete branch would break the kill-switch surface.

    TB-313 (axis 5) relocated the module body and switched the
    relative `from .auto_approve import …` to the absolute
    `from ap2.auto_approve import …` (the relative form would
    resolve to `ap2.components.focus_advance.auto_approve`, which
    does not exist); the contract this test pins is "the helper
    import is retained somewhere in the module," not the exact
    syntactic shape of the import statement.
    """
    src = Path(focus_advance.__file__).read_text()
    assert (
        "from ap2.auto_approve import _append_decisions_needed_bullet" in src
    ), (
        "TB-302: the kill-switch branch still depends on this "
        "import. If the import is removed, the kill-switch path "
        "raises NameError on the first operator-disabled "
        "advance attempt."
    )


def test_no_bullet_call_within_roadmap_complete_branch():
    """Source-level pin matching the briefing's Verification grep:
    `_append_decisions_needed_bullet` must NOT appear within 5
    lines after any `roadmap_complete_emitted` reference in
    `ap2/components/focus_advance/__init__.py` (post-TB-313
    relocation from the flat module path `ap2/focus_advance.py`).
    This is the literal shape the briefing's first Verification
    bullet checks; pinning it here surfaces a regression even
    when the bullet call moves to a slightly different line
    number."""
    src_path = Path(focus_advance.__file__)
    lines = src_path.read_text().splitlines()
    violations = []
    for i, line in enumerate(lines):
        if "roadmap_complete_emitted" in line:
            window = lines[i + 1 : i + 6]
            for j, w in enumerate(window, start=1):
                if "_append_decisions_needed_bullet" in w:
                    violations.append(
                        f"L{i + 1} ({line.strip()!r}) is followed by "
                        f"L{i + 1 + j} ({w.strip()!r})"
                    )
    assert not violations, (
        "TB-302: `_append_decisions_needed_bullet` appears within "
        "5 lines after a `roadmap_complete_emitted` reference; the "
        "roadmap-complete branch must not call the bullet helper. "
        f"Violations: {violations}"
    )


def test_module_docstring_documents_no_bullet_behavior():
    """The `ap2/components/focus_advance/__init__.py` module
    docstring must document the TB-302 behavior change (no bullet
    write on roadmap-complete). A future docs-drift refactor that
    loses this note would surface cleanly on this test."""
    doc = focus_advance.__doc__ or ""
    assert "TB-302" in doc, (
        "module docstring must reference TB-302's no-bullet "
        "behavior change"
    )
    # Either the explicit phrase or the no-longer-appends shape
    # qualifies; the parser-friendly grep is the phrase below.
    assert re.search(
        r"no longer appends.*Roadmap complete", doc, re.S,
    ), (
        "module docstring must explain that the daemon no longer "
        "appends a `Roadmap complete: ...` bullet to "
        "ideation_state.md"
    )

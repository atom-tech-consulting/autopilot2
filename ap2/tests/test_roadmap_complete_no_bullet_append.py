"""TB-302 regression pin: the roadmap-complete branch of
`ap2/ideation_halt.py:maybe_halt_on_exhaustion` must NOT append a
`Roadmap complete: ...` bullet to `.cc-autopilot/ideation_state.md`.

TB-345 merged the `focus_advance` component's residual detector into
the new core module `ap2/ideation_halt.py`, renaming the entry point
to the synchronous `maybe_halt_on_exhaustion(cfg)`; the source-level
pins below import via `from ap2 import ideation_halt` so the
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
already surfaced redundantly via four other channels:
  (a) the `roadmap_complete` event in `events.jsonl`,
  (b) `focus_pointer.json` (`roadmap_complete_emitted=true`),
  (c) `ap2 status`'s focus line (`focus: parked — ideation
      exhausted; extend goal.md via `ap2 update-goal` to resume,
      or `ap2 ack roadmap_complete` to dismiss this notice`),
      derived from `focus_pointer.json` via
      `goal.roadmap_exhausted`,
  (d) the web home page's `Focus — parked` header, derived from
      the same pointer, and
  (e) the TB-244 focus-rotation digest in the cron `status_report`
      post.

TB-342 collapsed the pre-existing multi-focus rotation pointer walk
into a single ideation-exhaustion detector; this test's assertions
were updated to use the new `trigger=empty_cycles_heuristic` shape
(replacing the now-retired `trigger=pointer_past_last` from the
rotation pass).

Other callers of `_append_decisions_needed_bullet` STAY
unchanged — they surface conditions that are NOT redundantly
signaled elsewhere:
  - The kill-switch branch in the SAME `maybe_halt_on_exhaustion`
    (`AP2_IDEATION_HALT_DISABLED=1` set when criteria would
    advance) — operator-killed-but-criteria-met has no equivalent
    focus-line surface; the operator needs the bullet to know to
    `ap2 update-goal` manually.
  - `ap2/auto_unfreeze.py`'s daily-cap halt.
  - `ap2/daemon.py`'s TB-224 task_error halt.

Behavioral pins covered here:

  - When `maybe_halt_on_exhaustion` trips the empty-cycles
    threshold, the `roadmap_complete` event IS emitted (events.jsonl
    has the entry).
  - `pointer["roadmap_complete_emitted"]` IS set to True.
  - `ideation_state.md` is NOT modified — no
    `Roadmap complete:` bullet appended; if the file exists
    before the call, its bytes are unchanged across the call.
  - Subsequent ticks (still exhausted,
    `roadmap_complete_emitted=true`) emit NO duplicate
    `roadmap_complete` events and don't modify
    `ideation_state.md`.
  - The kill-switch path
    (`AP2_IDEATION_HALT_DISABLED=1` set when criteria
    would advance) DOES still emit a
    `## Decisions needed from operator` bullet via
    `_append_decisions_needed_bullet` — that's a different code
    path with a different signal (criteria met but advance
    blocked).
  - Source-level pin: the `_append_decisions_needed_bullet` call
    no longer appears within 5 lines of a
    `roadmap_complete_emitted` reference in
    `ap2/ideation_halt.py` (the Verification bullet's grep surface).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from ap2 import events, goal, ideation_halt
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


def _emit_empty_cycle(cfg: Config) -> None:
    """Append one full empty ideation cycle (entry + complete, no
    proposal) so the counter sees it."""
    events.append(cfg.events_file, "ideation_empty_board", cooldown_s=0)
    events.append(cfg.events_file, "ideation_complete", summary="empty cycle")


def test_roadmap_complete_emits_event_and_sets_pointer_flag(cfg, monkeypatch):
    """When the empty-cycles threshold trips, the detector pass emits
    exactly one `roadmap_complete` event and sets
    `pointer['roadmap_complete_emitted']` to True. Pins that the
    side effects we KEEP still happen — only the bullet write was
    removed (TB-302). TB-342: the trigger is now
    `empty_cycles_heuristic` (the rotation `pointer_past_last`
    value retired with the multi-focus pointer walk)."""
    monkeypatch.delenv("AP2_CORE_IDEATION_HALT_DISABLED", raising=False)
    monkeypatch.setenv("AP2_CORE_IDEATION_HALT_EMPTY_CYCLES", "1")
    _write_goal_with_foci(cfg, "alpha")
    _emit_empty_cycle(cfg)

    ideation_halt.maybe_halt_on_exhaustion(cfg)

    tail = events.tail(cfg.events_file, 50)
    rc = [e for e in tail if e.get("type") == "roadmap_complete"]
    assert len(rc) == 1, "roadmap_complete event must fire exactly once"
    assert rc[0]["exhausted_count"] == 1
    assert rc[0]["trigger"] == "empty_cycles_heuristic"

    loaded = goal.load_pointer(cfg)
    assert loaded["roadmap_complete_emitted"] is True
    assert goal.roadmap_exhausted(cfg) is True


def test_roadmap_complete_does_not_create_ideation_state(cfg, monkeypatch):
    """When `ideation_state.md` does NOT exist before the detector
    pass, the roadmap-complete branch must NOT create it."""
    monkeypatch.delenv("AP2_IDEATION_HALT_DISABLED", raising=False)
    monkeypatch.setenv("AP2_IDEATION_HALT_EMPTY_CYCLES", "1")
    _write_goal_with_foci(cfg, "alpha")
    _emit_empty_cycle(cfg)

    ideation_state = _ideation_state_path(cfg)
    if ideation_state.exists():
        ideation_state.unlink()
    assert not ideation_state.exists()

    ideation_halt.maybe_halt_on_exhaustion(cfg)

    assert not ideation_state.exists(), (
        "TB-302: the roadmap-complete branch must not create "
        "ideation_state.md."
    )


def test_roadmap_complete_does_not_modify_existing_ideation_state(cfg, monkeypatch):
    """When `ideation_state.md` exists before the detector pass with
    pre-existing content, the roadmap-complete branch must leave its
    bytes unchanged."""
    monkeypatch.delenv("AP2_IDEATION_HALT_DISABLED", raising=False)
    monkeypatch.setenv("AP2_IDEATION_HALT_EMPTY_CYCLES", "1")
    _write_goal_with_foci(cfg, "alpha")
    _emit_empty_cycle(cfg)

    ideation_state = _ideation_state_path(cfg)
    ideation_state.parent.mkdir(parents=True, exist_ok=True)
    pre_content = (
        "# Ideation State\n\n"
        "## Active assessment\n\n"
        "Pre-existing agent-authored body.\n"
    )
    ideation_state.write_text(pre_content)
    pre_bytes = ideation_state.read_bytes()

    ideation_halt.maybe_halt_on_exhaustion(cfg)

    post_bytes = ideation_state.read_bytes()
    assert post_bytes == pre_bytes
    text = ideation_state.read_text()
    assert "Roadmap complete" not in text
    assert "Decisions needed from operator" not in text


def test_subsequent_ticks_dont_re_emit_or_modify_state(cfg, monkeypatch):
    """After the first detection sets
    `roadmap_complete_emitted=true`, subsequent calls to
    `_maybe_advance_focus` short-circuit."""
    monkeypatch.delenv("AP2_CORE_IDEATION_HALT_DISABLED", raising=False)
    monkeypatch.setenv("AP2_CORE_IDEATION_HALT_EMPTY_CYCLES", "1")
    _write_goal_with_foci(cfg, "alpha")
    _emit_empty_cycle(cfg)

    # First tick: emits the event, sets the flag.
    ideation_halt.maybe_halt_on_exhaustion(cfg)

    ideation_state = _ideation_state_path(cfg)
    snapshot_exists = ideation_state.exists()
    snapshot_bytes = (
        ideation_state.read_bytes() if snapshot_exists else None
    )

    # Subsequent ticks: short-circuit on the flag.
    for _ in range(3):
        ideation_halt.maybe_halt_on_exhaustion(cfg)

    tail = events.tail(cfg.events_file, 50)
    rc = [e for e in tail if e.get("type") == "roadmap_complete"]
    assert len(rc) == 1, (
        "roadmap_complete must fire exactly once across multiple "
        "ticks"
    )

    if snapshot_exists:
        assert ideation_state.read_bytes() == snapshot_bytes
    else:
        assert not ideation_state.exists()


def test_kill_switch_path_still_writes_decisions_needed_bullet(cfg, monkeypatch):
    """The kill-switch branch (`AP2_IDEATION_HALT_DISABLED=1`
    set when criteria would advance) DOES still write a
    `## Decisions needed from operator` bullet via
    `_append_decisions_needed_bullet`. TB-302 scoped the removal
    to the roadmap-complete branch ONLY."""
    monkeypatch.setenv("AP2_CORE_IDEATION_HALT_EMPTY_CYCLES", "1")
    monkeypatch.setenv("AP2_CORE_IDEATION_HALT_DISABLED", "1")
    _write_goal_with_foci(cfg, "alpha", "beta")
    _emit_empty_cycle(cfg)

    ideation_halt.maybe_halt_on_exhaustion(cfg)

    pointer = goal.load_pointer(cfg)
    assert pointer["roadmap_complete_emitted"] is False, (
        "kill-switch must block the halt emission"
    )

    ideation_state = _ideation_state_path(cfg)
    assert ideation_state.exists(), (
        "kill-switch path must create ideation_state.md when absent"
    )
    text = ideation_state.read_text()
    assert "Decisions needed from operator" in text
    assert "AP2_IDEATION_HALT_DISABLED" in text


def test_kill_switch_helper_import_retained():
    """Source-level pin: the `_append_decisions_needed_bullet`
    writer in `ap2/ideation_halt.py` is retained because the
    kill-switch branch below still uses it. Removing it on the
    assumption that the only caller was the roadmap-complete branch
    would break the kill-switch surface.

    TB-345 merged the residual detector into the core module
    `ap2/ideation_halt.py`; TB-391 then relocated it into the `ideation`
    component (`ap2/components/ideation/impl.py`), where the bullet writer
    is resolved through the registry hook-point protocol
    (`default_registry().get("auto_approve").hook_points[...]`) inside a
    local `_append_decisions_needed_bullet` helper. The contract this test
    pins is "the helper is retained somewhere in the module," not the
    exact syntactic shape of the lookup. The body lives in the component
    impl now (`ap2.ideation_halt` is a back-compat `__getattr__` shim), so
    we read the impl source.
    """
    from ap2.components.ideation import impl as _ideation_impl

    src = Path(_ideation_impl.__file__).read_text()
    assert "_append_decisions_needed_bullet" in src, (
        "TB-302: the kill-switch branch still depends on this "
        "helper. If it is removed, the kill-switch path "
        "raises NameError on the first operator-disabled "
        "advance attempt."
    )


def test_no_bullet_call_within_roadmap_complete_branch():
    """Source-level pin matching the briefing's Verification grep:
    `_append_decisions_needed_bullet` must NOT appear within 5
    lines after any `roadmap_complete_emitted` reference in
    `ap2/ideation_halt.py` (post-TB-345 merge of the residual
    detector into the core module). This is the literal shape the
    briefing's first Verification bullet checks; pinning it here
    surfaces a regression even when the bullet call moves to a
    slightly different line number.

    TB-391: the halt body lives in the `ideation` component impl now
    (`ap2.ideation_halt` is a back-compat shim), so we read the impl
    source."""
    from ap2.components.ideation import impl as _ideation_impl

    src_path = Path(_ideation_impl.__file__)
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
    """The `ap2/ideation_halt.py` module docstring must document the
    TB-302 behavior change (no bullet write on roadmap-complete). A
    future docs-drift refactor that loses this note would surface
    cleanly on this test."""
    # TB-345: the body's module docstring (with the TB-302 note) moved to
    # the core `ap2/ideation_halt.py` module.
    doc = ideation_halt.__doc__ or ""
    assert "TB-302" in doc, (
        "module docstring must reference TB-302's no-bullet "
        "behavior change"
    )
    # Either the explicit phrase or the no-longer-appends shape
    # qualifies; the parser-friendly grep is the phrase below.
    assert re.search(
        r"does not append.*Roadmap complete", doc, re.S,
    ) or re.search(
        r"no longer append.*Roadmap complete", doc, re.S,
    ), (
        "module docstring must explain that the daemon does not "
        "append a `Roadmap complete: ...` bullet to "
        "ideation_state.md"
    )

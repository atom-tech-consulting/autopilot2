"""TB-313: `focus_advance/` subpackage migration (axis 5).

Pins the structural relocation of the focus-list advance pass from
the flat module `ap2/focus_advance.py` to the subpackage
`ap2/components/focus_advance/__init__.py`, plus the registry-side
`hook_points` exposure that lets `ap2/daemon.py`'s module-level
aliases (`_FOCUS_RECENT_TAIL_N`, `_ideation_empty_against_focus`,
`_maybe_advance_focus`) resolve via the registry rather than a
direct import from `ap2/components/` (which the TB-311
import-direction gate forbids).

What this regression covers:

  (a) The subpackage body file exists at the post-migration path
      `ap2/components/focus_advance/__init__.py` and the flat module
      `ap2/focus_advance.py` is gone — defensive against a future
      refactor that resurrects the flat module on the assumption
      that the old import path is still load-bearing somewhere.
  (b) The manifest at `ap2/components/focus_advance/manifest.py`
      exposes `maybe_advance_focus`, `ideation_empty_against_focus`,
      and `focus_recent_tail_n` in its `hook_points` dict — the
      three symbols the daemon's module-level alias triad sources
      via `default_registry().get("focus_advance").hook_points[…]`.
  (c) The manifest's PRE_DISPATCH tick hook executes
      `_maybe_advance_focus` end-to-end against a stubbed event tail
      / pointer state (no SDK calls needed — TB-283 deleted the
      LLM-judge path; `sdk` is vestigial). The hook returns without
      raising and the pointer's `active_index` advances when the
      empty-cycles threshold trips.
  (d) `AP2_FOCUS_AUTO_ADVANCE_DISABLED=1` still suppresses the inner
      advance — the kill switch (goal.md L64-67) is preserved
      verbatim across the relocation. A decisions-needed bullet is
      emitted instead.

The tests live under `ap2/tests/` and are therefore allowed to
import `ap2.components.focus_advance` directly per the TB-311 gate's
`_iter_core_py_files` skip of the tests directory.
"""
from __future__ import annotations

import asyncio
import pathlib

import pytest

from ap2 import events, goal
from ap2.components import focus_advance
from ap2.config import Config
from ap2.init import init_project
from ap2.registry import Registry


# Repository root, derived from this file's location:
# ap2/tests/test_tb313_focus_advance_migration.py -> repo/
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Structural pins: the move actually happened
# ---------------------------------------------------------------------------


def test_subpackage_init_file_exists():
    """`ap2/components/focus_advance/__init__.py` exists post-migration
    and carries the actual module body (not the pre-TB-313 stub
    marker)."""
    init_path = _REPO_ROOT / "ap2/components/focus_advance/__init__.py"
    assert init_path.is_file(), (
        "TB-313: the subpackage body should live at "
        "`ap2/components/focus_advance/__init__.py` after the axis-5 "
        "relocation."
    )
    # Sanity: the body file carries the real module — `_maybe_advance_focus`
    # and `_ideation_empty_against_focus` are defined in source, not stubs.
    src = init_path.read_text(encoding="utf-8")
    assert "async def _maybe_advance_focus" in src, (
        "TB-313: the subpackage `__init__.py` should carry the real "
        "module body, not the pre-migration stub-marker file."
    )
    assert "def _ideation_empty_against_focus" in src, (
        "TB-313: the subpackage `__init__.py` should expose "
        "`_ideation_empty_against_focus` (the cycle-grouped counter)."
    )


def test_flat_module_file_is_gone():
    """`ap2/focus_advance.py` is removed — the briefing's `test ! -f`
    Verification bullet pinned to source."""
    flat_path = _REPO_ROOT / "ap2/focus_advance.py"
    assert not flat_path.exists(), (
        "TB-313: the flat module `ap2/focus_advance.py` should be "
        "removed after the axis-5 relocation; the subpackage at "
        "`ap2/components/focus_advance/__init__.py` is the canonical "
        "location now."
    )


def test_manifest_file_preserved():
    """The manifest at `ap2/components/focus_advance/manifest.py` is
    preserved (it pre-dated this task — TB-310 axis 2 stubbed it).
    """
    manifest_path = _REPO_ROOT / "ap2/components/focus_advance/manifest.py"
    assert manifest_path.is_file(), (
        "TB-313: the focus_advance manifest must remain at "
        "`ap2/components/focus_advance/manifest.py` after the body "
        "relocation; only `__init__.py` is the destination of the "
        "move."
    )


def test_kill_switch_preserved_verbatim():
    """The TB-226 kill switch `AP2_FOCUS_AUTO_ADVANCE_DISABLED` is
    referenced in the subpackage body verbatim — goal.md L64-67's
    operator-facing env knob name is not renamed across the
    relocation.
    """
    init_path = _REPO_ROOT / "ap2/components/focus_advance/__init__.py"
    src = init_path.read_text(encoding="utf-8")
    assert "AP2_FOCUS_AUTO_ADVANCE_DISABLED" in src, (
        "TB-313: the kill switch `AP2_FOCUS_AUTO_ADVANCE_DISABLED` "
        "must appear verbatim in the relocated module body — the "
        "operator-facing env knob name (goal.md L64-67) is not "
        "renameable."
    )


# ---------------------------------------------------------------------------
# Manifest `hook_points` exposure — the registry-side contract the
# daemon's module-level aliases source from.
# ---------------------------------------------------------------------------


_EXPECTED_HOOK_POINT_KEYS = (
    "tick_hook",
    "maybe_advance_focus",
    "ideation_empty_against_focus",
    "focus_recent_tail_n",
)


def test_manifest_hook_points_expose_daemon_alias_triad():
    """The manifest's `hook_points` dict exposes the three symbols
    the daemon's module-level alias triad sources from
    (`_FOCUS_RECENT_TAIL_N`, `_ideation_empty_against_focus`,
    `_maybe_advance_focus`) plus the `tick_hook` wrapper.
    """
    registry = Registry.discover()
    manifest = registry.get("focus_advance")
    for key in _EXPECTED_HOOK_POINT_KEYS:
        assert key in manifest.hook_points, (
            f"TB-313: focus_advance manifest's `hook_points` should "
            f"expose {key!r}; got "
            f"{sorted(manifest.hook_points)}"
        )
    # Identity check: the hook_points entries are the same objects
    # the subpackage body exports. A wrapper or copy would defeat the
    # monkeypatch-via-module seam tests rely on.
    assert (
        manifest.hook_points["maybe_advance_focus"]
        is focus_advance._maybe_advance_focus
    ), (
        "TB-313: `hook_points['maybe_advance_focus']` must be the "
        "EXACT callable object reachable from "
        "`ap2.components.focus_advance._maybe_advance_focus` — a "
        "wrapper would break daemon-side aliases that rely on "
        "object identity."
    )
    assert (
        manifest.hook_points["ideation_empty_against_focus"]
        is focus_advance._ideation_empty_against_focus
    )
    assert (
        manifest.hook_points["focus_recent_tail_n"]
        == focus_advance._FOCUS_RECENT_TAIL_N
    )


def test_daemon_module_aliases_resolve_via_registry():
    """`daemon._maybe_advance_focus` resolves to the same callable
    the registry's `hook_points["maybe_advance_focus"]` exposes —
    proves the daemon's module-level alias sources from the registry
    rather than from a direct `from ap2.components.focus_advance
    import …` (the TB-311 import-direction gate forbids the latter
    for core modules).
    """
    from ap2 import daemon

    registry = Registry.discover()
    manifest = registry.get("focus_advance")
    assert (
        daemon._maybe_advance_focus
        is manifest.hook_points["maybe_advance_focus"]
    ), (
        "TB-313: `daemon._maybe_advance_focus` must be the EXACT "
        "callable the registry exposes via "
        "`hook_points['maybe_advance_focus']`."
    )
    assert (
        daemon._ideation_empty_against_focus
        is manifest.hook_points["ideation_empty_against_focus"]
    )
    assert (
        daemon._FOCUS_RECENT_TAIL_N
        == manifest.hook_points["focus_recent_tail_n"]
    )


def test_daemon_does_not_statically_import_flat_module():
    """`ap2/daemon.py` no longer carries a `from .focus_advance`,
    `from ap2.focus_advance`, or `import ap2.focus_advance` — the
    briefing's grep-shape Verification bullet pinned to source.
    """
    daemon_src = (_REPO_ROOT / "ap2/daemon.py").read_text(encoding="utf-8")
    forbidden_fragments = (
        "from .focus_advance",
        "from ap2.focus_advance",
        "import ap2.focus_advance",
    )
    # Per-line scan — a comment / docstring that quotes the forbidden
    # fragments would false-positive a plain `in` check. We accept
    # the fragment iff it appears in a non-comment, non-quoted line
    # context. Cheap approximation: scan stripped lines that start
    # with `from` or `import`.
    violations: list[str] = []
    for lineno, line in enumerate(daemon_src.splitlines(), start=1):
        stripped = line.strip()
        if not (stripped.startswith("from") or stripped.startswith("import")):
            continue
        for frag in forbidden_fragments:
            if frag in stripped:
                violations.append(f"L{lineno}: {stripped}")
    assert not violations, (
        f"TB-313: `ap2/daemon.py` must not statically import the flat "
        f"`focus_advance` module path; found {violations}"
    )


def test_manifest_does_not_wrap_flat_module():
    """The manifest's body sources `_maybe_advance_focus` from the
    intra-package `from . import …` shape, not from
    `from ap2 import focus_advance` (which would resolve to the
    flat module — now gone — or to the `ap2.focus_advance` attr if
    a future refactor accidentally re-introduced it).
    """
    manifest_src = (
        _REPO_ROOT / "ap2/components/focus_advance/manifest.py"
    ).read_text(encoding="utf-8")
    assert "from ap2 import focus_advance" not in manifest_src, (
        "TB-313: the manifest must not wrap the flat `ap2.focus_advance` "
        "module — that path no longer exists post-relocation. The "
        "manifest sources via `from . import …` intra-package."
    )
    # Positive form: the intra-package import lives in the manifest.
    assert "from . import" in manifest_src, (
        "TB-313: the manifest should source `_maybe_advance_focus` "
        "(and the other exposed symbols) via `from . import …` from "
        "the subpackage's `__init__.py`."
    )


# ---------------------------------------------------------------------------
# Behavioral pins: the manifest's tick hook executes end-to-end +
# the kill switch still suppresses the inner advance.
# ---------------------------------------------------------------------------


def _make_cfg(tmp_path: pathlib.Path, *focus_titles: str) -> Config:
    """Build a fresh project under `tmp_path` with the given focus
    list seeded in goal.md."""
    init_project(tmp_path)
    focus_section = "".join(
        f"## Current focus: {t}\n\nBody for {t}.\n\n" for t in focus_titles
    )
    goal_text = (
        "# Project Goals\n\n"
        "## Mission\n\n"
        "Drive automation.\n\n"
        "## Done when\n\n"
        "- An operator can point ap2 at a fresh project and walk away.\n\n"
        f"{focus_section}"
        "## Non-goals\n\n"
        "- something out of scope.\n"
    )
    (tmp_path / "goal.md").write_text(goal_text)
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()
    return cfg


def _emit_empty_cycles(cfg: Config, focus_title: str, n: int) -> None:
    """Emit `n` consecutive cycles, each ending in
    `ideation_cycle_summary` with no `ideation_proposal_recorded`
    in between — the shape `_ideation_empty_against_focus` counts as
    "empty against the active focus."
    """
    for _ in range(n):
        events.append(cfg.events_file, "ideation_empty_board", focus=focus_title)
        events.append(
            cfg.events_file, "ideation_cycle_summary", focus=focus_title,
        )


def test_manifest_tick_hook_executes_maybe_advance_focus_end_to_end(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The manifest's `tick_hook` (registered on PRE_DISPATCH) wraps
    `_maybe_advance_focus` and, given a goal.md with a multi-focus
    list + enough empty cycles to trip the threshold, advances the
    pointer end-to-end. SDK is `None` — vestigial since TB-283.
    """
    monkeypatch.delenv("AP2_FOCUS_AUTO_ADVANCE_DISABLED", raising=False)
    monkeypatch.setenv("AP2_FOCUS_ADVANCE_EMPTY_CYCLES", "3")
    cfg = _make_cfg(tmp_path, "first focus", "second focus")
    # Seed three consecutive empty cycles against the active focus
    # — the default threshold the test pinned above (3).
    _emit_empty_cycles(cfg, "first focus", 3)

    # Resolve the tick hook via the registry — the same path the
    # daemon walks at runtime.
    registry = Registry.discover()
    manifest = registry.get("focus_advance")
    tick_hook = manifest.hook_points["tick_hook"]
    assert callable(tick_hook)

    asyncio.run(tick_hook(cfg, None))

    # The pointer advanced from index 0 to index 1 (the second focus).
    pointer = goal.load_pointer(cfg)
    assert pointer["active_index"] == 1, (
        f"TB-313: the manifest's tick hook should have advanced the "
        f"focus pointer from 0 to 1 after 3 empty cycles tripped the "
        f"threshold; got active_index={pointer['active_index']}, "
        f"pointer={pointer}"
    )
    assert pointer["active_title"] == "second focus", pointer

    # A `focus_advanced` event landed (the briefing-promised event
    # surface, preserved across the relocation).
    tail = events.tail(cfg.events_file, 200)
    advanced = [e for e in tail if e.get("type") == "focus_advanced"]
    assert advanced, (
        "TB-313: a `focus_advanced` event should fire when the tick "
        "hook advances the pointer."
    )
    assert advanced[-1].get("to") == "second focus", advanced[-1]


def test_kill_switch_suppresses_inner_advance(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`AP2_FOCUS_AUTO_ADVANCE_DISABLED=1` short-circuits the
    advance even when the empty-cycles threshold is met — the
    kill-switch behavior the briefing pins from goal.md L64-67 is
    preserved verbatim across the relocation.

    Operator-facing surface: a `## Decisions needed from operator`
    bullet is appended instead, so the operator can advance manually
    via `ap2 update-goal`.
    """
    monkeypatch.setenv("AP2_FOCUS_AUTO_ADVANCE_DISABLED", "1")
    monkeypatch.setenv("AP2_FOCUS_ADVANCE_EMPTY_CYCLES", "3")
    cfg = _make_cfg(tmp_path, "first focus", "second focus")
    _emit_empty_cycles(cfg, "first focus", 3)

    registry = Registry.discover()
    manifest = registry.get("focus_advance")
    tick_hook = manifest.hook_points["tick_hook"]
    asyncio.run(tick_hook(cfg, None))

    # Pointer did NOT advance — kill switch held.
    pointer = goal.load_pointer(cfg)
    assert pointer["active_index"] == 0, (
        f"TB-313: AP2_FOCUS_AUTO_ADVANCE_DISABLED=1 must suppress "
        f"the advance even when criteria are met; pointer advanced "
        f"to {pointer['active_index']}"
    )

    # No `focus_advanced` event fired.
    tail = events.tail(cfg.events_file, 200)
    advanced = [e for e in tail if e.get("type") == "focus_advanced"]
    assert not advanced, (
        "TB-313: AP2_FOCUS_AUTO_ADVANCE_DISABLED=1 must suppress the "
        "`focus_advanced` event emission too."
    )

    # The operator-facing decisions-needed bullet WAS appended — the
    # briefing-pinned alternative surface.
    ideation_state = cfg.project_root / ".cc-autopilot" / "ideation_state.md"
    if ideation_state.exists():
        text = ideation_state.read_text(encoding="utf-8")
        assert "AP2_FOCUS_AUTO_ADVANCE_DISABLED" in text, (
            "TB-313: the kill-switch path must surface a "
            "decisions-needed bullet naming the env knob so the "
            "operator knows to `ap2 update-goal` or unset the knob."
        )

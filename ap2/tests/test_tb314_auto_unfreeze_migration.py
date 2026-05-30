"""TB-314: `auto_unfreeze/` subpackage migration (axis 5).

Pins the structural relocation of the briefing-shape auto-unfreeze
sweep from the flat module `ap2/auto_unfreeze.py` to the subpackage
`ap2/components/auto_unfreeze/__init__.py`, plus the registry-side
`hook_points` exposure that lets `ap2/daemon.py`'s module-level
aliases (`_maybe_auto_unfreeze`, `_apply_auto_unfreeze_patch`,
`_auto_unfreeze_allowlist`, …) resolve via the registry rather than a
direct import from `ap2/components/` (which the TB-311
import-direction gate forbids).

What this regression covers:

  (a) The subpackage body file exists at the post-migration path
      `ap2/components/auto_unfreeze/__init__.py` and the flat module
      `ap2/auto_unfreeze.py` is gone — defensive against a future
      refactor that resurrects the flat module on the assumption
      that the old import path is still load-bearing somewhere.
  (b) The manifest at `ap2/components/auto_unfreeze/manifest.py`
      exposes the full daemon-alias surface — every symbol the
      pre-TB-314 daemon `auto_unfreeze.<sym>` alias block at
      L1781-1793 sourced — in its `hook_points` dict so core can
      resolve those aliases via
      `default_registry().get("auto_unfreeze").hook_points[…]`.
  (c) The manifest's PRE_DISPATCH tick hook executes
      `_maybe_auto_unfreeze` end-to-end against a stubbed board+queue
      with a Frozen task that carries a parseable `BriefingFix:` line
      — the hook applies the patch, emits `auto_unfreeze_applied`,
      and the task moves to Backlog after the operator queue drains.
  (d) `AP2_AUTO_UNFREEZE_FIX_SHAPES` unset still short-circuits the
      sweep (no `auto_unfreeze_applied` events, no skip events,
      task stays Frozen) — the kill-switch / master-enable env knob
      is preserved verbatim across the relocation.

The tests live under `ap2/tests/` and are therefore allowed to
import `ap2.components.auto_unfreeze` directly per the TB-311 gate's
`_iter_core_py_files` skip of the tests directory.
"""
from __future__ import annotations

import json
import pathlib
from pathlib import Path

import pytest

from ap2 import events, tools
from ap2.board import Board
from ap2.components import auto_unfreeze
from ap2.config import Config
from ap2.init import init_project
from ap2.registry import Registry


# Repository root, derived from this file's location:
# ap2/tests/test_tb314_auto_unfreeze_migration.py -> repo/
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Structural pins: the move actually happened
# ---------------------------------------------------------------------------


def test_subpackage_init_file_exists():
    """`ap2/components/auto_unfreeze/__init__.py` exists post-migration
    and carries the actual module body (not the pre-TB-314 stub
    marker).
    """
    init_path = _REPO_ROOT / "ap2/components/auto_unfreeze/__init__.py"
    assert init_path.is_file(), (
        "TB-314: the subpackage body should live at "
        "`ap2/components/auto_unfreeze/__init__.py` after the axis-5 "
        "relocation."
    )
    # Sanity: the body file carries the real module — `_maybe_auto_unfreeze`
    # and `_apply_auto_unfreeze_patch` are defined in source, not stubs.
    # TB-343: the body moved to the sibling `impl.py`; read it there (the
    # `__init__.py` shim above re-exports the surface).
    src = (
        _REPO_ROOT / "ap2/components/auto_unfreeze/impl.py"
    ).read_text(encoding="utf-8")
    assert "def _maybe_auto_unfreeze" in src, (
        "TB-314: the subpackage `__init__.py` should carry the real "
        "module body, not the pre-migration stub-marker file."
    )
    assert "def _apply_auto_unfreeze_patch" in src, (
        "TB-314: the subpackage `__init__.py` should expose "
        "`_apply_auto_unfreeze_patch` (the briefing read-modify-write "
        "+ operator-queue helper)."
    )


def test_flat_module_file_is_gone():
    """`ap2/auto_unfreeze.py` is removed — the briefing's `test ! -f`
    Verification bullet pinned to source.
    """
    flat_path = _REPO_ROOT / "ap2/auto_unfreeze.py"
    assert not flat_path.exists(), (
        "TB-314: the flat module `ap2/auto_unfreeze.py` should be "
        "removed after the axis-5 relocation; the subpackage at "
        "`ap2/components/auto_unfreeze/__init__.py` is the canonical "
        "location now."
    )


def test_manifest_file_preserved():
    """The manifest at `ap2/components/auto_unfreeze/manifest.py` is
    preserved (it pre-dated this task — TB-310 axis 2 stubbed it).
    """
    manifest_path = _REPO_ROOT / "ap2/components/auto_unfreeze/manifest.py"
    assert manifest_path.is_file(), (
        "TB-314: the auto_unfreeze manifest must remain at "
        "`ap2/components/auto_unfreeze/manifest.py` after the body "
        "relocation; only `__init__.py` is the destination of the "
        "move."
    )


def test_kill_switch_env_knob_preserved_verbatim():
    """The TB-225 master-enable env knob
    `AP2_AUTO_UNFREEZE_FIX_SHAPES` (goal.md L64-67) is referenced in
    the subpackage body verbatim — the operator-facing knob name is
    not renamed across the relocation. Sibling knobs
    (`AP2_AUTO_UNFREEZE_DRY_RUN`, `AP2_AUTO_UNFREEZE_MAX_PER_TASK`,
    `AP2_AUTO_UNFREEZE_MAX_PER_DAY`) are pinned the same way — any
    rename would be an operator-visible regression.
    """
    # TB-343: the body (and its env-knob references) moved to `impl.py`.
    init_path = _REPO_ROOT / "ap2/components/auto_unfreeze/impl.py"
    src = init_path.read_text(encoding="utf-8")
    for knob in (
        "AP2_AUTO_UNFREEZE_FIX_SHAPES",
        "AP2_AUTO_UNFREEZE_DRY_RUN",
        "AP2_AUTO_UNFREEZE_MAX_PER_TASK",
        "AP2_AUTO_UNFREEZE_MAX_PER_DAY",
    ):
        assert knob in src, (
            f"TB-314: the env knob `{knob}` must appear verbatim in "
            f"the relocated module body — the operator-facing env "
            f"knob name (goal.md L64-67) is not renameable."
        )


# ---------------------------------------------------------------------------
# Manifest `hook_points` exposure — the registry-side contract the
# daemon's module-level aliases source from.
# ---------------------------------------------------------------------------


_EXPECTED_HOOK_POINT_KEYS = (
    "tick_hook",
    "AUTO_UNFREEZE_MAX_PER_DAY_DEFAULT",
    "AUTO_UNFREEZE_MAX_PER_TASK_DEFAULT",
    "AUTO_UNFREEZE_WINDOW_S",
    "apply_auto_unfreeze_patch",
    "auto_unfreeze_allowlist",
    "auto_unfreeze_dry_run",
    "auto_unfreeze_max_per_day",
    "auto_unfreeze_max_per_task",
    "count_auto_unfreeze_applied_for_task",
    "count_auto_unfreeze_applied_in_window",
    "maybe_auto_unfreeze",
    "most_recent_blocked_complete_for",
    "shared_parse",
)


def test_manifest_hook_points_expose_daemon_alias_surface():
    """The manifest's `hook_points` dict exposes every symbol the
    daemon's pre-TB-314 alias block at L1781-1793 sourced from the
    flat module, plus the `tick_hook` wrapper. Identity checks
    confirm the manifest publishes the EXACT callable / value the
    subpackage body exports — a wrapper or copy would defeat
    monkey-patch-via-module seams tests rely on.
    """
    registry = Registry.discover()
    manifest = registry.get("auto_unfreeze")
    for key in _EXPECTED_HOOK_POINT_KEYS:
        assert key in manifest.hook_points, (
            f"TB-314: auto_unfreeze manifest's `hook_points` should "
            f"expose {key!r}; got {sorted(manifest.hook_points)}"
        )
    # Identity check on each function-shaped hook point.
    assert (
        manifest.hook_points["maybe_auto_unfreeze"]
        is auto_unfreeze._maybe_auto_unfreeze
    ), (
        "TB-314: `hook_points['maybe_auto_unfreeze']` must be the "
        "EXACT callable object reachable from "
        "`ap2.components.auto_unfreeze._maybe_auto_unfreeze` — a "
        "wrapper would break daemon-side aliases that rely on object "
        "identity."
    )
    assert (
        manifest.hook_points["apply_auto_unfreeze_patch"]
        is auto_unfreeze._apply_auto_unfreeze_patch
    )
    assert (
        manifest.hook_points["auto_unfreeze_allowlist"]
        is auto_unfreeze._auto_unfreeze_allowlist
    )
    assert (
        manifest.hook_points["auto_unfreeze_dry_run"]
        is auto_unfreeze._auto_unfreeze_dry_run
    )
    assert (
        manifest.hook_points["auto_unfreeze_max_per_day"]
        is auto_unfreeze._auto_unfreeze_max_per_day
    )
    assert (
        manifest.hook_points["auto_unfreeze_max_per_task"]
        is auto_unfreeze._auto_unfreeze_max_per_task
    )
    assert (
        manifest.hook_points["count_auto_unfreeze_applied_for_task"]
        is auto_unfreeze._count_auto_unfreeze_applied_for_task
    )
    assert (
        manifest.hook_points["count_auto_unfreeze_applied_in_window"]
        is auto_unfreeze._count_auto_unfreeze_applied_in_window
    )
    assert (
        manifest.hook_points["most_recent_blocked_complete_for"]
        is auto_unfreeze._most_recent_blocked_complete_for
    )
    assert (
        manifest.hook_points["shared_parse"]
        is auto_unfreeze._shared_parse
    )
    # Constants compare by value.
    assert (
        manifest.hook_points["AUTO_UNFREEZE_MAX_PER_DAY_DEFAULT"]
        == auto_unfreeze._AUTO_UNFREEZE_MAX_PER_DAY_DEFAULT
    )
    assert (
        manifest.hook_points["AUTO_UNFREEZE_MAX_PER_TASK_DEFAULT"]
        == auto_unfreeze._AUTO_UNFREEZE_MAX_PER_TASK_DEFAULT
    )
    assert (
        manifest.hook_points["AUTO_UNFREEZE_WINDOW_S"]
        == auto_unfreeze._AUTO_UNFREEZE_WINDOW_S
    )


def test_daemon_module_aliases_resolve_via_registry():
    """`daemon._maybe_auto_unfreeze` (and the rest of the alias block)
    resolves to the same callable / value the registry's
    `hook_points` exposes — proves the daemon's module-level aliases
    source from the registry rather than from a direct
    `from ap2.components.auto_unfreeze import …` (the TB-311
    import-direction gate forbids the latter for core modules).
    """
    from ap2 import daemon

    registry = Registry.discover()
    manifest = registry.get("auto_unfreeze")
    assert (
        daemon._maybe_auto_unfreeze
        is manifest.hook_points["maybe_auto_unfreeze"]
    ), (
        "TB-314: `daemon._maybe_auto_unfreeze` must be the EXACT "
        "callable the registry exposes via "
        "`hook_points['maybe_auto_unfreeze']`."
    )
    assert (
        daemon._apply_auto_unfreeze_patch
        is manifest.hook_points["apply_auto_unfreeze_patch"]
    )
    assert (
        daemon._auto_unfreeze_allowlist
        is manifest.hook_points["auto_unfreeze_allowlist"]
    )
    assert (
        daemon._auto_unfreeze_dry_run
        is manifest.hook_points["auto_unfreeze_dry_run"]
    )
    assert (
        daemon._AUTO_UNFREEZE_WINDOW_S
        == manifest.hook_points["AUTO_UNFREEZE_WINDOW_S"]
    )


def test_daemon_does_not_statically_import_flat_module():
    """`ap2/daemon.py` no longer carries a `from .auto_unfreeze`,
    `from ap2.auto_unfreeze`, or `import ap2.auto_unfreeze` — the
    briefing's grep-shape Verification bullet pinned to source. Also
    pins that the `from . import (...)` block no longer lists
    `auto_unfreeze,` as a sibling import.
    """
    daemon_src = (_REPO_ROOT / "ap2/daemon.py").read_text(encoding="utf-8")
    forbidden_fragments = (
        "from .auto_unfreeze",
        "from ap2.auto_unfreeze",
        "import ap2.auto_unfreeze",
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
        f"TB-314: `ap2/daemon.py` must not statically import the flat "
        f"`auto_unfreeze` module path; found {violations}"
    )
    # Also pin: the multi-line `from . import (...)` block no longer
    # lists `auto_unfreeze,` as a sibling import (the briefing's
    # grep-shape Verification bullet).
    for lineno, line in enumerate(daemon_src.splitlines(), start=1):
        stripped = line.strip()
        if stripped == "auto_unfreeze,":
            pytest.fail(
                f"TB-314: `ap2/daemon.py:L{lineno}` still lists "
                f"`auto_unfreeze,` in the `from . import (...)` "
                f"block — drop it per the axis-5 migration."
            )


def test_manifest_does_not_wrap_flat_module():
    """The manifest's body sources `_maybe_auto_unfreeze` from the
    intra-package `from . import …` shape, not from
    `from ap2 import auto_unfreeze` (which would resolve to the
    flat module — now gone — or to the `ap2.auto_unfreeze` attr if
    a future refactor accidentally re-introduced it).
    """
    manifest_src = (
        _REPO_ROOT / "ap2/components/auto_unfreeze/manifest.py"
    ).read_text(encoding="utf-8")
    assert "from ap2 import auto_unfreeze" not in manifest_src, (
        "TB-314: the manifest must not wrap the flat "
        "`ap2.auto_unfreeze` module — that path no longer exists "
        "post-relocation. The manifest sources via `from . import …` "
        "intra-package."
    )
    # Positive form: the intra-package import lives in the manifest.
    assert "from . import" in manifest_src, (
        "TB-314: the manifest should source `_maybe_auto_unfreeze` "
        "(and the other exposed symbols) via `from . import …` from "
        "the subpackage's `__init__.py`."
    )


# ---------------------------------------------------------------------------
# Behavioral pins: the manifest's tick hook executes end-to-end +
# the kill switch (master-enable env knob unset) still suppresses
# the inner sweep.
# ---------------------------------------------------------------------------


# Minimal goal.md so the briefing structural validator + goal-anchor
# gate don't false-positive when we exercise the update path via the
# operator queue. Mirrors `_GOAL_MD` in test_tb225_auto_unfreeze.py.
_GOAL_MD = (
    "# Project Goals\n\n"
    "## Mission\n\n"
    "Drive the project toward end-to-end automation.\n\n"
    "## Done when\n\n"
    "- An operator can point ap2 at a fresh project and walk away "
    "without intervention.\n\n"
    "## Current focus: end-to-end automation\n\n"
    "Close the manual-approval bottleneck plus failure-recovery gaps.\n\n"
    "## Non-goals\n\n"
    "- something out of scope.\n"
)


# Briefing carries a `## Verification` shell bullet that the patch
# will rewrite end-to-end (grep -lE → grep -rlE, the canonical TB-225
# fix-shape `grep_missing_r_on_dir`).
_BRIEFING = (
    "# TB-314 fixture briefing\n\n"
    "## Goal\n\n"
    "Self-heals the briefing-shape regression class so the end-to-end "
    "automation focus (`## Current focus: end-to-end automation`) can "
    "land without operator-manual unfreeze on every recurrence.\n\n"
    "Why now: closes the failure-recovery operator dependency — without "
    "this, every briefing-shape regression cascades into operator-manual "
    "unfreeze and the walk-away envelope contracts.\n\n"
    "## Scope\n\n"
    "- ap2/daemon.py\n\n"
    "## Design\n\n"
    "Direct edit.\n\n"
    "## Verification\n"
    "- `grep -lE 'pattern' ap2/tests/` — matches at least one file.\n\n"
    "## Out of scope\n\n"
    "- nothing\n"
)


def _make_cfg(tmp_path: Path) -> Config:
    """Build a fresh project under `tmp_path` with the standard ap2 init
    layout + a real goal.md.
    """
    init_project(tmp_path)
    (tmp_path / "goal.md").write_text(_GOAL_MD)
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()
    return cfg


def _unwrap(res: dict) -> dict:
    assert not res.get("isError"), res
    return json.loads(res["content"][0]["text"])


def _add_and_freeze(cfg: Config) -> tuple[str, Path]:
    """Add a Backlog task, drain the queue to materialize it on disk,
    then move it to Frozen via the direct board edit (same shape as
    `test_tb225_auto_unfreeze.py::_add_and_freeze`).
    """
    res = tools.do_operator_queue_append(
        cfg,
        {
            "op": "add_backlog",
            "title": "tb314 fixture",
            "briefing": _BRIEFING,
        },
    )
    info = _unwrap(res)
    task_id = info["task_id"]
    tools.drain_operator_queue(cfg)
    board = Board.load(cfg.tasks_file)
    task = board.get(task_id)
    assert task is not None and task.briefing
    briefing_path = cfg.project_root / task.briefing
    assert briefing_path.exists()
    tools.do_board_edit(
        cfg, {"action": "move_to_frozen", "task_id": task_id},
    )
    return task_id, briefing_path


def _emit_blocked_complete_with_fix(
    cfg: Config, *, task_id: str, briefing_path: Path,
) -> None:
    """Emit a `task_complete status=blocked` event whose summary
    carries the canonical `BriefingFix:` line targeting the grep
    bullet in the fixture briefing.
    """
    rel = str(briefing_path.relative_to(cfg.project_root))
    text = briefing_path.read_text()
    grep_line_idx = next(
        i for i, line in enumerate(text.splitlines())
        if "grep -lE" in line
    )
    summary = (
        "Agent diagnosis: grep -lE on a directory needs -r.\n"
        f"BriefingFix: grep_missing_r_on_dir at {rel}:{grep_line_idx + 1}: "
        f"grep -lE -> grep -rlE"
    )
    events.append(
        cfg.events_file,
        "task_complete",
        task=task_id,
        status="blocked",
        commit="",
        summary=summary,
    )


def test_manifest_tick_hook_applies_patch_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The manifest's `tick_hook` (registered on PRE_DISPATCH) wraps
    `_maybe_auto_unfreeze` and, given an allowlisted fix shape + a
    Frozen task whose blocked summary carries a parseable
    `BriefingFix:` line, applies the patch end-to-end: queues the
    `update` + `unfreeze` ops, emits `auto_unfreeze_applied`, and
    (after the operator queue drains) the briefing is patched and the
    task is back in Backlog.
    """
    monkeypatch.setenv(
        "AP2_AUTO_UNFREEZE_FIX_SHAPES", "grep_missing_r_on_dir",
    )
    monkeypatch.delenv("AP2_AUTO_UNFREEZE_DRY_RUN", raising=False)
    cfg = _make_cfg(tmp_path)
    task_id, briefing_path = _add_and_freeze(cfg)
    _emit_blocked_complete_with_fix(
        cfg, task_id=task_id, briefing_path=briefing_path,
    )

    # Resolve the tick hook via the registry — the same path the
    # daemon walks at runtime.
    registry = Registry.discover()
    manifest = registry.get("auto_unfreeze")
    tick_hook = manifest.hook_points["tick_hook"]
    assert callable(tick_hook)

    tick_hook(cfg, None)

    # `auto_unfreeze_applied` fired with the expected payload shape.
    evts = events.tail(cfg.events_file, 400)
    applied = [e for e in evts if e.get("type") == "auto_unfreeze_applied"]
    assert len(applied) == 1, (
        f"TB-314: the tick hook should emit exactly one "
        f"`auto_unfreeze_applied` event; got: {applied}"
    )
    assert applied[0]["task"] == task_id
    assert applied[0]["shape"] == "grep_missing_r_on_dir"
    assert applied[0]["from"] == "grep -lE"
    assert applied[0]["to"] == "grep -rlE"

    # Drain the operator queue so the `update` + `unfreeze` ops land.
    tools.drain_operator_queue(cfg)

    # Briefing patched on disk.
    text_after = briefing_path.read_text()
    assert "grep -rlE" in text_after, (
        f"TB-314: the briefing should now contain the patched form; "
        f"got:\n{text_after}"
    )

    # Task moved Frozen → Backlog.
    board = Board.load(cfg.tasks_file)
    loc = board.find(task_id)
    assert loc is not None and loc[0] == "Backlog", (
        f"TB-314: the task must move to Backlog after auto-unfreeze "
        f"drains; got section={loc}"
    )


def test_kill_switch_suppresses_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`AP2_AUTO_UNFREEZE_FIX_SHAPES` unset (the TB-225 master-enable
    knob, preserved verbatim across the relocation) short-circuits
    the sweep even when a Frozen task has a parseable
    `BriefingFix:` line. No `auto_unfreeze_applied` event, no skip
    event (opt-in feature), task stays Frozen.
    """
    monkeypatch.delenv("AP2_AUTO_UNFREEZE_FIX_SHAPES", raising=False)
    cfg = _make_cfg(tmp_path)
    task_id, briefing_path = _add_and_freeze(cfg)
    _emit_blocked_complete_with_fix(
        cfg, task_id=task_id, briefing_path=briefing_path,
    )

    registry = Registry.discover()
    manifest = registry.get("auto_unfreeze")
    tick_hook = manifest.hook_points["tick_hook"]
    tick_hook(cfg, None)

    evts = events.tail(cfg.events_file, 400)
    applied = [e for e in evts if e.get("type") == "auto_unfreeze_applied"]
    skipped = [e for e in evts if e.get("type") == "auto_unfreeze_skipped"]
    assert applied == [], (
        f"TB-314: kill switch (master env knob unset) must suppress "
        f"`auto_unfreeze_applied`; got: {applied}"
    )
    assert skipped == [], (
        f"TB-314: kill switch (master env knob unset) is opt-in — no "
        f"skip events should fire; got: {skipped}"
    )

    board = Board.load(cfg.tasks_file)
    loc = board.find(task_id)
    assert loc is not None and loc[0] == "Frozen", (
        f"TB-314: the task must stay Frozen when the master-enable "
        f"knob is unset; got section={loc}"
    )

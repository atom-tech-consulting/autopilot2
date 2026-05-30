"""TB-318: `auto_approve/` subpackage migration (axis 5 — final).

Pins the structural relocation of the auto-approve dispatch policy from
the flat module `ap2/auto_approve.py` to the subpackage
`ap2/components/auto_approve/__init__.py`, plus the registry-side
`hook_points` exposure that lets `ap2/daemon.py`'s 18 module-level
aliases (`_AUTO_APPROVE_FAILURE_STATUSES`, `_was_auto_approved`,
`_auto_approve_paused`, …, `evaluate_auto_approve_decision`) resolve
via the registry rather than a direct import from `ap2/components/`
(which the TB-311 import-direction gate forbids).

This is the FINAL named axis-5 migration per goal.md L196-197;
`auto_approve/` was sequenced LAST because its blast radius (ideation,
proposal labeling, retry semantics, cost guards) is the largest of the
flat modules. Conservative scope: the file move + manifest update +
daemon-alias rebind only. The inline per-task gate logic in
`daemon._tick` stays in-place this cycle (each gate emits task-specific
events with observable payload; conflating the file-move with gate
extraction would risk behavior drift).

What this regression covers:

  (a) The subpackage body file exists at the post-migration path
      `ap2/components/auto_approve/__init__.py` and the flat module
      `ap2/auto_approve.py` is gone.
  (b) The manifest at `ap2/components/auto_approve/manifest.py`
      registers `name="auto_approve"`, `env_flag="AP2_AUTO_APPROVE"`,
      `default_enabled=False` (TB-320 wired the existing
      require-polarity master switch the daemon already self-gates on
      onto the manifest so the registry / `ap2 status` render the
      on/off state correctly), and exposes the full daemon-alias
      surface in its `hook_points` dict.
  (c) Importing `ap2.daemon` does not raise, and each rebound alias
      on the daemon module evaluates to the EXACT object the
      registry exposes via
      `default_registry().get("auto_approve").hook_points[…]` —
      proving the daemon's module-level aliases source from the
      registry rather than from a direct
      `from ap2.components.auto_approve import …`.
  (d) The TB-311 import-direction gate is still green — no static
      `from ap2.components.auto_approve` reference exists in any
      core file.

The tests live under `ap2/tests/` and are therefore allowed to
import `ap2.components.auto_approve` directly per the TB-311 gate's
`_iter_core_py_files` skip of the tests directory.
"""
from __future__ import annotations

import pathlib

import pytest

from ap2.components import auto_approve
from ap2.registry import Registry


# Repository root, derived from this file's location:
# ap2/tests/test_tb318_auto_approve_migration.py -> repo/
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


# The 18 symbols `ap2/daemon.py` rebinds at module load — the manifest
# must expose each one in `hook_points`. Mirrors the alias block that
# pre-TB-318 lived at `ap2/daemon.py` L1760-1777 (17 lines plus
# `evaluate_auto_approve_decision`); post-migration each alias resolves
# via `default_registry().get("auto_approve").hook_points[<name>]`.
_DAEMON_ALIAS_SYMBOLS: tuple[str, ...] = (
    "_AUTO_APPROVE_FAILURE_STATUSES",
    "_AUTO_APPROVE_UNFREEZE_TOKEN",
    "_AUTO_APPROVE_WINDOW_RESUME_TOKEN",
    "_AUTO_APPROVE_WINDOW_S",
    "_append_decisions_needed_bullet",
    "_auto_approve_already_halted",
    "_auto_approve_check_violations",
    "_auto_approve_freeze_threshold",
    "_auto_approve_paused",
    "_auto_approve_window_resume_idx",
    "_auto_approved_task_ids",
    "_event_combined_tokens",
    "_parse_event_ts",
    "_per_task_token_cap",
    "_validator_judge_noisy_paused",
    "_was_auto_approved",
    "_window_token_cap",
    "evaluate_auto_approve_decision",
)


# ---------------------------------------------------------------------------
# Structural pins: the move actually happened
# ---------------------------------------------------------------------------


def test_subpackage_init_file_exists_and_non_empty():
    """`ap2/components/auto_approve/__init__.py` exists post-migration
    and carries the actual module body (not the pre-TB-318 stub
    marker).
    """
    init_path = _REPO_ROOT / "ap2/components/auto_approve/__init__.py"
    assert init_path.is_file(), (
        "TB-318: the subpackage body should live at "
        "`ap2/components/auto_approve/__init__.py` after the axis-5 "
        "relocation."
    )
    # TB-343: the body moved to the sibling `impl.py`; read it there for the
    # body-content pins (the `__init__.py` shim above re-exports the surface).
    src = (
        _REPO_ROOT / "ap2/components/auto_approve/impl.py"
    ).read_text(encoding="utf-8")
    assert src.strip(), (
        "TB-343: the relocated `impl.py` must be non-empty — it carries "
        "the full module body that the `__init__.py` shim re-exports."
    )
    # Sanity: the body file carries the real implementation, not the
    # pre-TB-318 stub-marker placeholder.
    assert "def _auto_approve_paused" in src, (
        "TB-318: the subpackage `__init__.py` should carry the real "
        "module body, not the pre-migration stub-marker file."
    )
    assert "def evaluate_auto_approve_decision" in src, (
        "TB-318: the subpackage `__init__.py` should expose "
        "`evaluate_auto_approve_decision` (the TB-232 dispatch-time "
        "gate-chain helper)."
    )


def test_flat_module_file_is_gone():
    """`ap2/auto_approve.py` is removed — the briefing's `test ! -f`
    Verification bullet pinned to source.
    """
    flat_path = _REPO_ROOT / "ap2/auto_approve.py"
    assert not flat_path.exists(), (
        "TB-318: the flat module `ap2/auto_approve.py` should be "
        "removed after the axis-5 relocation; the subpackage at "
        "`ap2/components/auto_approve/__init__.py` is the canonical "
        "location now."
    )


def test_manifest_file_preserved():
    """The manifest at `ap2/components/auto_approve/manifest.py` is
    preserved (it pre-dated this task — TB-310 axis 2 stubbed it; TB-318
    rewrote the body to source intra-package).
    """
    manifest_path = _REPO_ROOT / "ap2/components/auto_approve/manifest.py"
    assert manifest_path.is_file(), (
        "TB-318: the auto_approve manifest must remain at "
        "`ap2/components/auto_approve/manifest.py` after the body "
        "relocation; only `__init__.py` is the destination of the "
        "move."
    )


def test_env_knobs_preserved_verbatim():
    """The operator-facing env knobs (goal.md L64-67 constraint) appear
    verbatim in the relocated module body. Any rename across the move
    would be an operator-visible regression — the briefing's
    Out-of-scope clause explicitly prohibits knob renames.
    """
    # TB-343: the body (and its env-knob references) moved to `impl.py`.
    init_path = _REPO_ROOT / "ap2/components/auto_approve/impl.py"
    src = init_path.read_text(encoding="utf-8")
    for knob in (
        "AP2_AUTO_APPROVE",
        "AP2_AUTO_APPROVE_DRY_RUN",
        "AP2_AUTO_APPROVE_FREEZE_THRESHOLD",
        "AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP",
        "AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP",
        "AP2_AUTO_APPROVE_GATE_TAGS",
        "AP2_AUTO_APPROVE_NOISY_PAUSE_DISABLED",
    ):
        assert knob in src, (
            f"TB-318: the env knob `{knob}` must appear verbatim in "
            f"the relocated module body — the operator-facing env "
            f"knob name (goal.md L64-67) is not renameable across "
            f"the axis-5 relocation."
        )


# ---------------------------------------------------------------------------
# Manifest registry shape — the registry-side contract the daemon's
# module-level aliases source from.
# ---------------------------------------------------------------------------


def test_manifest_registry_shape():
    """`default_registry().get("auto_approve")` returns a Manifest with
    `name="auto_approve"`, `env_flag="AP2_AUTO_APPROVE"` (TB-320
    wired in the operator-facing require-polarity gate),
    `default_enabled=False` (opt-in / require-polarity — matches
    TB-223's existing semantics where `AP2_AUTO_APPROVE=1` is
    required to enable autonomous approve).
    """
    registry = Registry.discover()
    manifest = registry.get("auto_approve")
    assert manifest.name == "auto_approve", (
        f"TB-318: manifest name should be 'auto_approve'; got "
        f"{manifest.name!r}"
    )
    assert manifest.env_flag == "AP2_AUTO_APPROVE", (
        f"TB-320: manifest `env_flag` should be 'AP2_AUTO_APPROVE' "
        f"(the operator-facing master switch the daemon already "
        f"self-gates on); got {manifest.env_flag!r}"
    )
    assert manifest.default_enabled is False, (
        f"TB-320: manifest `default_enabled` should be False "
        f"(require-polarity / opt-in — matches TB-223 semantics); "
        f"got {manifest.default_enabled!r}"
    )


@pytest.mark.parametrize("symbol", _DAEMON_ALIAS_SYMBOLS)
def test_manifest_hook_points_expose_daemon_alias(symbol: str):
    """The manifest's `hook_points` dict exposes every symbol the
    daemon's pre-TB-318 alias block at L1760-1777 sourced from the
    flat module — 17 alias lines plus
    `evaluate_auto_approve_decision`. Each entry is callable-or-value
    (constants like `_AUTO_APPROVE_FAILURE_STATUSES` are frozensets;
    everything else is a callable).
    """
    registry = Registry.discover()
    manifest = registry.get("auto_approve")
    assert symbol in manifest.hook_points, (
        f"TB-318: auto_approve manifest's `hook_points` should expose "
        f"{symbol!r}; got {sorted(manifest.hook_points)}"
    )
    value = manifest.hook_points[symbol]
    assert value is not None, (
        f"TB-318: `hook_points[{symbol!r}]` must not be None — the "
        f"daemon's module-level alias resolves directly through this "
        f"value at import time."
    )
    # Identity check: the manifest publishes the EXACT object the
    # subpackage body exports. A wrapper or copy would defeat
    # monkey-patch-via-module seams tests rely on.
    expected = getattr(auto_approve, symbol)
    assert value is expected, (
        f"TB-318: `hook_points[{symbol!r}]` must be the EXACT object "
        f"reachable from `ap2.components.auto_approve.{symbol}` — a "
        f"wrapper would break daemon-side aliases that rely on "
        f"object identity."
    )


def test_manifest_also_registers_tick_hook():
    """The manifest keeps the `tick_hook` POST_DISPATCH registration
    from the pre-TB-318 stub (a no-op placeholder). The inline
    per-task gate logic in `daemon._tick` stays inline this cycle, so
    the hook stays a no-op; an axis-5 follow-up may extract the gate
    and turn this into the real callable.
    """
    registry = Registry.discover()
    manifest = registry.get("auto_approve")
    assert "tick_hook" in manifest.hook_points, (
        "TB-318: the `tick_hook` registration is preserved across the "
        "migration (no-op placeholder until a follow-up extracts the "
        "inline gate logic)."
    )
    assert callable(manifest.hook_points["tick_hook"]), (
        "TB-318: `hook_points['tick_hook']` should be callable even as "
        "a no-op."
    )


# ---------------------------------------------------------------------------
# Daemon-side resolution: importing `ap2.daemon` doesn't raise and each
# alias evaluates to the registry's hook_points value.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("symbol", _DAEMON_ALIAS_SYMBOLS)
def test_daemon_alias_resolves_via_registry(symbol: str):
    """Each `daemon.<symbol>` alias evaluates to the EXACT object the
    registry exposes via
    `default_registry().get("auto_approve").hook_points[<symbol>]`.
    Proves the daemon's module-level aliases source from the registry
    rather than from a direct
    `from ap2.components.auto_approve import …` (which the TB-311
    import-direction gate forbids for core modules).
    """
    from ap2 import daemon

    registry = Registry.discover()
    manifest = registry.get("auto_approve")
    daemon_value = getattr(daemon, symbol)
    hook_value = manifest.hook_points[symbol]
    assert daemon_value is hook_value, (
        f"TB-318: `daemon.{symbol}` must be the EXACT object the "
        f"registry exposes via `hook_points[{symbol!r}]`; daemon "
        f"id={id(daemon_value)}, hook id={id(hook_value)}"
    )


def test_daemon_does_not_statically_import_flat_or_components_path():
    """`ap2/daemon.py` no longer carries a `from .auto_approve`,
    `from ap2.auto_approve`, `import ap2.auto_approve`, or any direct
    `from ap2.components.auto_approve` — the briefing's grep-shape
    Verification bullets pinned to source. Also pins that the
    `from . import (...)` block no longer lists `auto_approve,` as a
    sibling import.
    """
    daemon_src = (_REPO_ROOT / "ap2/daemon.py").read_text(encoding="utf-8")
    forbidden_fragments = (
        "from .auto_approve",
        "from ap2.auto_approve",
        "import ap2.auto_approve",
        "from ap2.components.auto_approve",
        "from .components.auto_approve",
    )
    # Per-line scan — a comment / docstring that quotes the forbidden
    # fragments would false-positive a plain `in` check. We accept
    # the fragment iff it appears in a non-comment line that starts
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
        f"TB-318: `ap2/daemon.py` must not statically import the flat "
        f"`auto_approve` module path nor the components-side subpackage "
        f"directly; found {violations}"
    )
    # Also pin: the multi-line `from . import (...)` block no longer
    # lists `auto_approve,` as a sibling import (briefing's grep-shape
    # Verification bullet).
    for lineno, line in enumerate(daemon_src.splitlines(), start=1):
        stripped = line.strip()
        if stripped == "auto_approve,":
            pytest.fail(
                f"TB-318: `ap2/daemon.py:L{lineno}` still lists "
                f"`auto_approve,` in the `from . import (...)` block — "
                f"drop it per the axis-5 migration."
            )


def test_daemon_has_no_remaining_auto_approve_dot_symbol_references():
    """The briefing's `! grep -nE '\\bauto_approve\\.[A-Za-z_]'`
    Verification bullet pinned to source — every alias in
    `daemon.py` rebinds through the registry, not through a direct
    `auto_approve.<symbol>` attribute lookup. Detects the case where
    a future refactor accidentally re-introduces a flat-module
    reference (e.g. by copying a TB-263-style direct call).
    """
    import re

    daemon_src = (_REPO_ROOT / "ap2/daemon.py").read_text(encoding="utf-8")
    pattern = re.compile(r"\bauto_approve\.[A-Za-z_]")
    violations: list[str] = []
    for lineno, line in enumerate(daemon_src.splitlines(), start=1):
        if pattern.search(line):
            violations.append(f"L{lineno}: {line}")
    assert not violations, (
        f"TB-318: `ap2/daemon.py` must not contain any "
        f"`auto_approve.<symbol>` reference — every alias rebinds "
        f"through `default_registry().get('auto_approve')."
        f"hook_points[…]`. Found:\n" + "\n".join(violations)
    )


def test_manifest_sources_intra_package():
    """The manifest's body sources the daemon-exposed symbols via
    `from . import …` intra-package, not from
    `from ap2 import auto_approve` (which would resolve to the flat
    module — now gone — or to `ap2.auto_approve` if a future refactor
    accidentally re-introduced it).
    """
    manifest_src = (
        _REPO_ROOT / "ap2/components/auto_approve/manifest.py"
    ).read_text(encoding="utf-8")
    assert "from ap2 import auto_approve" not in manifest_src, (
        "TB-318: the manifest must not wrap the flat `ap2.auto_approve` "
        "module — that path no longer exists post-relocation. The "
        "manifest sources via `from . import …` intra-package."
    )
    # Positive form: the intra-package import lives in the manifest.
    assert "from . import" in manifest_src, (
        "TB-318: the manifest should source the alias-surface symbols "
        "(`_auto_approve_paused`, `_was_auto_approved`, …) via "
        "`from . import …` from the subpackage's `__init__.py`."
    )


# ---------------------------------------------------------------------------
# Import-direction gate stays green (TB-311 cleavage holds across the
# relocation).
# ---------------------------------------------------------------------------


def test_core_import_direction_gate_still_passes():
    """Re-invoke the TB-311 import-direction gate's core walker to
    catch the case where the TB-318 relocation accidentally
    introduced a static `from ap2.components.auto_approve` import
    in any core (non-test) module. The gate's own test file lives
    under `ap2/tests/` and is exempt by definition; we re-execute
    the same check here so a TB-318 regression surfaces in this
    file's failure list rather than only via the cross-cutting gate.
    """
    from ap2.tests.test_core_import_direction import (
        _EXEMPT_FILES,
        _iter_core_py_files,
        find_violations,
    )

    all_violations: list[tuple[str, int, str]] = []
    for path in _iter_core_py_files():
        rel = path.relative_to(_REPO_ROOT).as_posix()
        if rel in _EXEMPT_FILES:
            continue
        source = path.read_text(encoding="utf-8")
        for lineno, stmt in find_violations(source, rel):
            all_violations.append((rel, lineno, stmt))
    assert not all_violations, (
        f"TB-318: the import-direction gate (TB-311) must stay green "
        f"across the relocation; found {len(all_violations)} core "
        f"file(s) statically importing from `ap2.components`: "
        f"{all_violations}"
    )

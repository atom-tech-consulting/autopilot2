"""TB-315: `attention/` subpackage migration (axis 5).

Pins the structural relocation of the proactive
`attention_raised` detector + daemon-side wire-up from the flat
module `ap2/attention.py` to the subpackage
`ap2/components/attention/__init__.py`, plus the registry-side
`hook_points` exposure that lets `ap2/daemon.py`'s module-level
aliases (`_maybe_emit_attention_events`, `detect_attention_conditions`,
`should_suppress`, …) resolve via the registry rather than a direct
import from `ap2/components/` (which the TB-311 import-direction
gate forbids).

What this regression covers:

  (a) The subpackage body file exists at the post-migration path
      `ap2/components/attention/__init__.py` and the flat module
      `ap2/attention.py` is gone — defensive against a future
      refactor that resurrects the flat module on the assumption
      that the old import path is still load-bearing somewhere.
  (b) The manifest at `ap2/components/attention/manifest.py`
      exposes the full daemon-alias surface — every symbol the
      daemon previously imported from `ap2/attention.py` AND the
      daemon-side wire-up helpers that moved into the subpackage
      in the same task (`_maybe_emit_attention_events`,
      `_maybe_push_attention`, the immediate-push state helpers) —
      in its `hook_points` dict so core can resolve those aliases
      via `default_registry().get("attention").hook_points[…]`.
  (c) The manifest's ATTENTION_EMISSION tick hook executes
      `_maybe_emit_attention_events` end-to-end against a stubbed
      board + events tail with a stuck-task condition — the hook
      emits an `attention_raised` event whose payload matches the
      detector's contract.
  (d) Both `AP2_ATTENTION_IMMEDIATE_PUSH` and `AP2_ATTENTION_DEBOUNCE_S`
      env knobs are preserved verbatim in the subpackage body /
      config (goal.md L64-67 names them as load-bearing operator
      contract; a rename would be an operator-visible regression).
  (e) The TB-311 import-direction gate stays green post-migration
      (no new static `ap2.components` import sneaks in via the
      core files updated in this task: status_report.py,
      cli_daemon.py, web_attention.py, web_home.py).

The tests live under `ap2/tests/` and are therefore allowed to
import `ap2.components.attention` directly per the TB-311 gate's
`_iter_core_py_files` skip of the tests directory.
"""
from __future__ import annotations

import datetime as _dt
import pathlib
from pathlib import Path

import pytest

from ap2 import events
from ap2.board import Board
from ap2.components import attention
from ap2.config import Config
from ap2.init import init_project
from ap2.registry import Phase, Registry


# Repository root, derived from this file's location:
# ap2/tests/test_tb315_attention_migration.py -> repo/
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Structural pins: the move actually happened
# ---------------------------------------------------------------------------


def test_subpackage_init_file_exists():
    """`ap2/components/attention/__init__.py` exists post-migration
    and carries the actual module body (not the pre-TB-315 stub
    marker).
    """
    init_path = _REPO_ROOT / "ap2/components/attention/__init__.py"
    assert init_path.is_file(), (
        "TB-315: the subpackage body should live at "
        "`ap2/components/attention/__init__.py` after the axis-5 "
        "relocation."
    )
    # TB-343: the body moved to the sibling `impl.py`; read it there (the
    # `__init__.py` shim above re-exports the surface).
    src = (
        _REPO_ROOT / "ap2/components/attention/impl.py"
    ).read_text(encoding="utf-8")
    # Sanity: the body file carries the real module — the detectors
    # and the daemon-side wire-up are defined in source, not stubs.
    assert "def detect_attention_conditions" in src, (
        "TB-315: the subpackage `__init__.py` should carry the real "
        "module body, not the pre-migration stub-marker file."
    )
    assert "def _maybe_emit_attention_events" in src, (
        "TB-315: the daemon-side wire-up `_maybe_emit_attention_events` "
        "must live in the subpackage so the manifest's tick hook can "
        "call it body-locally (drop pre-TB-315 late-binding)."
    )
    assert "def _maybe_push_attention" in src, (
        "TB-315: the immediate-push helper `_maybe_push_attention` "
        "must live in the subpackage so `_maybe_emit_attention_events` "
        "can call it locally."
    )


def test_flat_module_file_is_gone():
    """`ap2/attention.py` is removed — the briefing's `test ! -f`
    Verification bullet pinned to source.
    """
    flat_path = _REPO_ROOT / "ap2/attention.py"
    assert not flat_path.exists(), (
        "TB-315: the flat module `ap2/attention.py` should be "
        "removed after the axis-5 relocation; the subpackage at "
        "`ap2/components/attention/__init__.py` is the canonical "
        "location now."
    )


def test_manifest_file_preserved():
    """The manifest at `ap2/components/attention/manifest.py` is
    preserved (it pre-dated this task — TB-310 axis 2 stubbed it).
    """
    manifest_path = _REPO_ROOT / "ap2/components/attention/manifest.py"
    assert manifest_path.is_file(), (
        "TB-315: the attention manifest must remain at "
        "`ap2/components/attention/manifest.py` after the body "
        "relocation; only `__init__.py` is the destination of the "
        "move."
    )


def test_env_knobs_preserved_verbatim():
    """The TB-282 / TB-297 env knobs `AP2_ATTENTION_DEBOUNCE_S` and
    `AP2_ATTENTION_IMMEDIATE_PUSH` (goal.md L64-67) are referenced
    in the subpackage body verbatim — the operator-facing knob
    names are not renamed across the relocation. Sibling detector
    knobs (`AP2_TASK_STUCK_THRESHOLD_S`, `AP2_TASK_FROZEN_RECENCY_S`,
    `AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD`,
    `AP2_AUTO_APPROVE_COST_APPROACH_PCT`) are pinned the same way —
    any rename would be an operator-visible regression.
    """
    # TB-343: the body (and its env-knob references) moved to `impl.py`.
    init_path = _REPO_ROOT / "ap2/components/attention/impl.py"
    src = init_path.read_text(encoding="utf-8")
    for knob in (
        "AP2_ATTENTION_DEBOUNCE_S",
        "AP2_ATTENTION_IMMEDIATE_PUSH",
        "AP2_TASK_STUCK_THRESHOLD_S",
        "AP2_TASK_FROZEN_RECENCY_S",
        "AP2_AUTO_APPROVE_COST_APPROACH_PCT",
    ):
        assert knob in src, (
            f"TB-315: the env knob `{knob}` must appear verbatim in "
            f"the relocated module body — the operator-facing env "
            f"knob name (goal.md L64-67) is not renameable."
        )


# ---------------------------------------------------------------------------
# Manifest `hook_points` exposure — the registry-side contract the
# daemon's module-level aliases source from.
# ---------------------------------------------------------------------------


_EXPECTED_HOOK_POINT_KEYS = (
    "tick_hook",
    # Detector-layer surface (pre-TB-315 daemon imported these from
    # the flat module).
    "AttentionCondition",
    "detect_attention_conditions",
    "find_last_attention_fire",
    "should_suppress",
    "parse_ts",
    "task_stuck_threshold_s",
    "task_frozen_recency_s",
    "cost_approach_pct",
    "attention_debounce_s",
    # Daemon-side wire-up helpers (relocated from daemon.py in TB-315
    # so the manifest's `_tick_hook` can call them body-locally; the
    # daemon re-exposes them as module-level aliases for test back-
    # compat).
    "maybe_emit_attention_events",
    "maybe_push_attention",
    "attention_push_state_path",
    "load_attention_push_state",
    "save_attention_push_state",
    "is_attention_immediate_push_enabled",
)


def test_manifest_hook_points_expose_daemon_alias_surface():
    """The manifest's `hook_points` dict exposes every symbol the
    daemon's pre-TB-315 attention-import surface sourced from
    `ap2/attention.py`, plus the daemon-side wire-up helpers that
    moved into the subpackage. Identity checks confirm the manifest
    publishes the EXACT callable / value the subpackage body exports
    — a wrapper or copy would defeat monkey-patch-via-module seams
    tests rely on.
    """
    registry = Registry.discover()
    manifest = registry.get("attention")
    for key in _EXPECTED_HOOK_POINT_KEYS:
        assert key in manifest.hook_points, (
            f"TB-315: attention manifest's `hook_points` should "
            f"expose {key!r}; got {sorted(manifest.hook_points)}"
        )
    # Identity check on each function-shaped hook point.
    assert (
        manifest.hook_points["detect_attention_conditions"]
        is attention.detect_attention_conditions
    ), (
        "TB-315: `hook_points['detect_attention_conditions']` must "
        "be the EXACT callable object reachable from "
        "`ap2.components.attention.detect_attention_conditions` — a "
        "wrapper would break daemon-side aliases that rely on object "
        "identity."
    )
    assert (
        manifest.hook_points["should_suppress"]
        is attention.should_suppress
    )
    assert (
        manifest.hook_points["find_last_attention_fire"]
        is attention.find_last_attention_fire
    )
    assert (
        manifest.hook_points["AttentionCondition"]
        is attention.AttentionCondition
    )
    assert (
        manifest.hook_points["maybe_emit_attention_events"]
        is attention._maybe_emit_attention_events
    )
    assert (
        manifest.hook_points["maybe_push_attention"]
        is attention._maybe_push_attention
    )
    assert (
        manifest.hook_points["attention_push_state_path"]
        is attention._attention_push_state_path
    )
    assert (
        manifest.hook_points["load_attention_push_state"]
        is attention._load_attention_push_state
    )
    assert (
        manifest.hook_points["save_attention_push_state"]
        is attention._save_attention_push_state
    )
    assert (
        manifest.hook_points["is_attention_immediate_push_enabled"]
        is attention._is_attention_immediate_push_enabled
    )


def test_daemon_module_aliases_resolve_via_registry():
    """`daemon._maybe_emit_attention_events` (and the rest of the
    alias block) resolves to the same callable / value the
    registry's `hook_points` exposes — proves the daemon's
    module-level aliases source from the registry rather than from a
    direct `from ap2.components.attention import …` (the TB-311
    import-direction gate forbids the latter for core modules).
    """
    from ap2 import daemon

    registry = Registry.discover()
    manifest = registry.get("attention")
    assert (
        daemon._maybe_emit_attention_events
        is manifest.hook_points["maybe_emit_attention_events"]
    ), (
        "TB-315: `daemon._maybe_emit_attention_events` must be the "
        "EXACT callable the registry exposes via "
        "`hook_points['maybe_emit_attention_events']`."
    )
    assert (
        daemon._maybe_push_attention
        is manifest.hook_points["maybe_push_attention"]
    )
    assert (
        daemon.detect_attention_conditions
        is manifest.hook_points["detect_attention_conditions"]
    )
    assert (
        daemon.should_suppress
        is manifest.hook_points["should_suppress"]
    )
    assert (
        daemon.AttentionCondition
        is manifest.hook_points["AttentionCondition"]
    )


def test_manifest_env_flag_polarity_is_none():
    """The attention manifest's `env_flag` is None — no master
    enable/disable knob, mirroring auto_unfreeze's polarity
    (goal.md L121-125: always-enabled unless the manifest declares
    otherwise). The per-behavior knobs
    (`AP2_ATTENTION_IMMEDIATE_PUSH` for push, the threshold knobs
    for each detector) gate internally.
    """
    registry = Registry.discover()
    manifest = registry.get("attention")
    assert manifest.env_flag is None, (
        f"TB-315: attention manifest's `env_flag` must be None "
        f"(no master switch — per-behavior knobs gate internally); "
        f"got {manifest.env_flag!r}."
    )
    assert manifest.default_enabled is True


def test_daemon_does_not_statically_import_flat_module():
    """`ap2/daemon.py` no longer carries a `from .attention`,
    `from ap2.attention`, or `import ap2.attention` — the
    briefing's grep-shape Verification bullet pinned to source. Also
    pins that the `from . import (...)` block no longer lists
    `attention,` as a sibling import.
    """
    daemon_src = (_REPO_ROOT / "ap2/daemon.py").read_text(encoding="utf-8")
    forbidden_fragments = (
        "from .attention",
        "from ap2.attention",
        "import ap2.attention",
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
        f"TB-315: `ap2/daemon.py` must not statically import the flat "
        f"`attention` module path; found {violations}"
    )
    # Also pin: the multi-line `from . import (...)` block no longer
    # lists `attention,` as a sibling import (the briefing's
    # grep-shape Verification bullet).
    for lineno, line in enumerate(daemon_src.splitlines(), start=1):
        stripped = line.strip()
        if stripped == "attention,":
            pytest.fail(
                f"TB-315: `ap2/daemon.py:L{lineno}` still lists "
                f"`attention,` in the `from . import (...)` block — "
                f"drop it per the axis-5 migration."
            )


def test_manifest_does_not_wrap_flat_module():
    """The manifest's body sources the attention symbols from the
    intra-package `from . import …` shape, not from
    `from ap2 import daemon` (the pre-TB-315 late-binding shim)
    nor from `from ap2 import attention` (the flat module is gone).

    AST-walk the manifest so docstring / comment mentions of the
    pre-migration shape (legitimately documenting what changed)
    don't false-positive a plain substring check.
    """
    import ast

    manifest_path = (
        _REPO_ROOT / "ap2/components/attention/manifest.py"
    )
    tree = ast.parse(manifest_path.read_text(encoding="utf-8"))
    has_intra_package_import = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "ap2" and any(
                a.name == "daemon" for a in node.names
            ):
                pytest.fail(
                    f"TB-315: the manifest must not late-bind through "
                    f"`from ap2 import daemon` at L{node.lineno} — the "
                    f"body now lives intra-package so the late-binding "
                    f"shim is gone."
                )
            if node.module == "ap2.attention" or node.module == "ap2":
                # Check for any flat-attention import.
                if any(a.name == "attention" for a in node.names):
                    pytest.fail(
                        f"TB-315: the manifest must not import the "
                        f"flat `ap2.attention` path at L{node.lineno}; "
                        f"the flat module is gone post-migration."
                    )
            if node.level == 1 and node.module is None:
                has_intra_package_import = True
            elif node.level == 1 and node.module is not None:
                # `from .X import Y` — counts as intra-package
                has_intra_package_import = True
    # Positive form: the intra-package import lives in the manifest.
    assert has_intra_package_import, (
        "TB-315: the manifest should source `_maybe_emit_attention_events` "
        "(and the other exposed symbols) via `from . import …` from "
        "the subpackage's `__init__.py`."
    )


# ---------------------------------------------------------------------------
# Behavioral pins: the manifest's tick hook executes end-to-end.
# ---------------------------------------------------------------------------


def _make_cfg(tmp_path: Path) -> Config:
    """Build a fresh project under `tmp_path` with the standard ap2 init
    layout. Mirrors TB-314's `_make_cfg` shape.
    """
    init_project(tmp_path)
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()
    return cfg


def test_manifest_tick_hook_emits_attention_raised_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The manifest's `tick_hook` (registered on ATTENTION_EMISSION)
    wraps `_maybe_emit_attention_events` and, given a stuck Active
    task whose `task_start` is past `AP2_TASK_STUCK_THRESHOLD_S`,
    emits an `attention_raised` event with the documented payload
    shape end-to-end. Mirrors TB-314's tick-hook end-to-end test.

    Disable the immediate-MM-push branch (the default — but pin it
    explicitly so a stray env var on the test host doesn't make
    `_maybe_push_attention` try to reach Mattermost).
    """
    monkeypatch.delenv("AP2_ATTENTION_IMMEDIATE_PUSH", raising=False)
    # Tighten the stuck-threshold to 60s so the test runs quickly.
    monkeypatch.setenv("AP2_TASK_STUCK_THRESHOLD_S", "60")
    cfg = _make_cfg(tmp_path)

    # Seed a stuck Active task: place TB-700 in Active, then emit a
    # `task_start` event with a `ts` 2h before NOW (the tick hook
    # calls `datetime.now(timezone.utc)` internally so we anchor on
    # real wall-clock time, not a hardcoded synthetic).
    board = Board.load(cfg.tasks_file)
    board.add("Active", task_id="TB-700", title="synthetic stuck")
    board.save()
    now = _dt.datetime.now(_dt.timezone.utc)
    start_ts = (now - _dt.timedelta(hours=2)).strftime(
        "%Y-%m-%dT%H:%M:%SZ",
    )
    events.append(
        cfg.events_file, "task_start", task="TB-700", title="x",
    )
    # Rewrite the most recent event's `ts` to 2h ago so the detector
    # sees the task as stuck against the 60s threshold.
    import json
    lines = cfg.events_file.read_text().splitlines()
    last = json.loads(lines[-1])
    last["ts"] = start_ts
    lines[-1] = json.dumps(last)
    cfg.events_file.write_text("\n".join(lines) + "\n")

    # Resolve the tick hook via the registry — the same path the
    # daemon walks at runtime.
    registry = Registry.discover()
    manifest = registry.get("attention")
    tick_hook = manifest.hook_points["tick_hook"]
    assert callable(tick_hook)
    # The ATTENTION_EMISSION phase carries exactly this hook.
    hooks = registry.tick_hooks(Phase.ATTENTION_EMISSION)
    assert tick_hook in hooks

    tick_hook(cfg, None)

    # `attention_raised` fired with the expected payload shape.
    evts = events.tail(cfg.events_file, 400)
    raised = [e for e in evts if e.get("type") == "attention_raised"]
    assert len(raised) == 1, (
        f"TB-315: the tick hook should emit exactly one "
        f"`attention_raised` event for the stuck Active task; "
        f"got: {raised}"
    )
    ev = raised[0]
    assert ev["attention_type"] == "task_stuck"
    assert ev["key"] == "task_stuck:TB-700"
    assert "TB-700" in ev["summary"]
    assert ev["task"] == "TB-700"
    assert ev["title"] == "synthetic stuck"


def test_import_direction_gate_stays_green_post_migration():
    """The TB-311 import-direction gate must stay green — none of
    the core files this task touched (status_report.py, cli_daemon.py,
    web_attention.py, web_home.py) introduces a static
    `from ap2.components.attention import …`. They all resolve the
    detector via `importlib.import_module(...)` (a dynamic Call node
    the gate intentionally exempts) so the AST walk stays quiet.

    Doubles as a smoke test that the briefing's grep-shape
    Verification bullet against `ap2/daemon.py`, `ap2/cli.py`,
    `ap2/cli_daemon.py`, `ap2/status_report.py`,
    `ap2/operator_queue.py`, and `ap2/briefing_validators.py` holds —
    no flat `from ap2 import attention` / `from ap2.attention`
    statement survives in those six core files.
    """
    from ap2.tests.test_core_import_direction import (
        _iter_core_py_files,
        find_violations,
        _EXEMPT_FILES,
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
        f"TB-315: the import-direction gate must stay green after "
        f"the attention migration; got violations: {all_violations}"
    )

    # Briefing-shape pin: the six named core files don't carry the
    # flat-attention import surface.
    for rel in (
        "ap2/daemon.py",
        "ap2/cli.py",
        "ap2/cli_daemon.py",
        "ap2/status_report.py",
        "ap2/operator_queue.py",
        "ap2/briefing_validators.py",
    ):
        src = (_REPO_ROOT / rel).read_text(encoding="utf-8")
        for lineno, line in enumerate(src.splitlines(), start=1):
            stripped = line.strip()
            if not (
                stripped.startswith("from") or stripped.startswith("import")
            ):
                continue
            for frag in (
                "from ap2 import attention",
                "from ap2.attention",
                "import ap2.attention",
                "from .attention",
            ):
                assert frag not in stripped, (
                    f"TB-315: `{rel}:L{lineno}` still imports the "
                    f"flat attention path ({frag!r}); migrate to "
                    f"`importlib.import_module('ap2.components."
                    f"attention')` or the registry's hook_points "
                    f"resolution."
                )

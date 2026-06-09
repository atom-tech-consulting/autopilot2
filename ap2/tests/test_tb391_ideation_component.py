"""TB-391 (axis 4): ideation proposal-engine component extraction.

Pins the structural cleavage required by the **get the component boundary
right — loop-level participants only** focus: the ideation proposal engine
— the last genuine loop subsystem welded into core — runs as a registry
component (`ap2/components/ideation/`) behind the reserved `Phase.IDEATION`
tick hook plus a `Phase.PRE_DISPATCH` roadmap-exhaustion halt hook, instead
of inline in `daemon._tick`.

  - The natural empty-board trigger gate (`_maybe_ideate`), the operator-
    forced run (`force_ideate`), the shared SDK-invocation helper
    (`_run_ideation`), the slot budget (`_compute_slots`), the scrub
    (`_maybe_scrub_ideation_state`), the `AP2_IDEATION_*` knob readers, and
    the roadmap-exhaustion detector (`maybe_halt_on_exhaustion` + the
    empty-cycles accounting) moved into `ap2/components/ideation/impl.py`.
  - `ap2/ideation.py` and `ap2/ideation_halt.py` survive as back-compat
    `__getattr__` shims re-exporting the moved symbols.
  - `daemon._tick` drives ideation purely through the registry — the
    natural path via `tick_hooks(Phase.IDEATION)`, the halt via the
    PRE_DISPATCH walk, the operator-forced run via the `force_ideate`
    hook-point. Core no longer imports `ideation` / `ideation_halt`.
  - Core never statically imports `ap2/components/ideation/`; the import-
    direction CI gate (`test_core_import_direction.py`) still passes.
"""
from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest

from ap2 import daemon
from ap2 import ideation as _ideation
from ap2 import ideation_halt as _ideation_halt
from ap2.components.ideation import (
    run_force_ideate,
    run_ideation_halt,
    run_ideation_tick,
)
from ap2.registry import Phase, Registry, default_registry


# ---------------------------------------------------------------------------
# (1) Component is registered + discoverable with the kill-switch env_flag
# ---------------------------------------------------------------------------


def test_ideation_component_discoverable_and_registered():
    """`Registry.discover()` surfaces the `ideation` component with its
    `tick_hook` registered on `Phase.IDEATION` and a `halt_hook` on
    `Phase.PRE_DISPATCH` (the manifest + impl land at
    `ap2/components/ideation/`)."""
    registry = Registry.discover()
    names = {m.name for m in registry.components}
    assert "ideation" in names, (
        f"TB-391: the ideation component should be discoverable via the "
        f"filesystem walk of `ap2/components/*/manifest.py`; "
        f"discovered={sorted(names)}"
    )
    manifest = registry.get("ideation")
    assert "tick_hook" in manifest.hook_points
    assert "halt_hook" in manifest.hook_points
    assert "force_ideate" in manifest.hook_points
    phases = [p for p, _ in manifest.tick_hooks]
    assert Phase.IDEATION in phases, phases
    assert Phase.PRE_DISPATCH in phases, phases


def test_ideation_component_has_kill_switch_default_on():
    """The ideation component is a default-on toggle-able component with the
    `AP2_IDEATION_DISABLED` kill switch (mirrors the janitor / cron /
    auto_unfreeze kill-switch family). Verification bullet:
    `m.env_flag == "AP2_IDEATION_DISABLED"`."""
    manifest = default_registry().get("ideation")
    assert manifest.env_flag == "AP2_IDEATION_DISABLED", manifest.env_flag
    assert manifest.default_enabled is True


def test_manifest_file_exists():
    """Verification bullet `test -f ap2/components/ideation/manifest.py`."""
    here = Path(__file__).resolve().parents[1]  # ap2/
    assert (here / "components" / "ideation" / "manifest.py").is_file()


def test_ideation_tick_hook_registered_on_ideation_phase():
    """The natural empty-board trigger gate (`run_ideation_tick`) is the
    registered `Phase.IDEATION` hook — filling the phase TB-381 reserved
    (and walked empty) for this extraction."""
    hooks = default_registry().tick_hooks(Phase.IDEATION)
    assert run_ideation_tick in hooks, hooks


def test_halt_hook_registered_on_pre_dispatch_after_auto_sweeps():
    """The roadmap-exhaustion halt (`run_ideation_halt`) is a PRE_DISPATCH
    hook; name-sorted component order runs it AFTER the auto-* sweeps and
    before the cron stage — exactly the slot the inline step-0.6 call
    occupied. The registry returns PRE_DISPATCH hooks name-sorted, so
    `ideation` lands after `auto_approve` / `auto_unfreeze`."""
    registry = Registry.discover()
    hooks = registry.tick_hooks(Phase.PRE_DISPATCH)
    assert run_ideation_halt in hooks, hooks
    # Map each hook to its component for the ordering pin.
    name_for_hook: dict[int, str] = {}
    for manifest in registry.components:
        for phase, fn in manifest.tick_hooks:
            if phase is Phase.PRE_DISPATCH:
                name_for_hook[id(fn)] = manifest.name
    ordered = [name_for_hook[id(h)] for h in hooks]
    assert ordered == sorted(ordered), ordered
    assert ordered[-1] == "ideation", (
        f"TB-391: ideation must be the last PRE_DISPATCH hook (name-sorted) "
        f"so the halt fires after the auto-* sweeps; got {ordered}"
    )


# ---------------------------------------------------------------------------
# (2) Daemon drives ideation purely via the registry — no inline import/call
# ---------------------------------------------------------------------------


def test_daemon_does_not_import_ideation_modules():
    """Verification bullet (criterion 6): `daemon._tick` no longer imports
    `ideation` / `ideation_halt`. If it did (`from . import ideation`), the
    module would be an attribute of `ap2.daemon`."""
    assert not hasattr(daemon, "ideation"), (
        "TB-391: daemon must not import the `ideation` module."
    )
    assert not hasattr(daemon, "ideation_halt"), (
        "TB-391: daemon must not import the `ideation_halt` module."
    )


def test_daemon_tick_drives_ideation_via_registry():
    """Source-pin: `_tick` walks `tick_hooks(Phase.IDEATION)` for the
    natural path and resolves the operator-forced run via the
    `force_ideate` registry hook-point."""
    src = inspect.getsource(daemon._tick)
    assert "tick_hooks(Phase.IDEATION)" in src, (
        "TB-391: `_tick` must walk `registry.tick_hooks(Phase.IDEATION)`."
    )
    assert '"force_ideate"' in src, (
        "TB-391: `_tick` must resolve the forced run via the "
        "`force_ideate` registry hook-point."
    )


def test_core_does_not_statically_import_ideation_component():
    """Verification bullet (import-direction): daemon.py / tools.py /
    cli*.py do not statically import the ideation component."""
    ap2_root = Path(__file__).resolve().parents[1]
    targets = [ap2_root / "daemon.py", ap2_root / "tools.py"]
    targets += list(ap2_root.glob("cli*.py"))
    for path in targets:
        src = path.read_text()
        assert "from ap2.components.ideation" not in src, path
        assert "import ap2.components.ideation" not in src, path


# ---------------------------------------------------------------------------
# (3) Back-compat shims re-export the moved symbols
# ---------------------------------------------------------------------------


def test_ideation_shim_reexports_moved_symbols():
    """`ap2.ideation`'s `__getattr__` shim re-exports the moved
    proposal-engine symbols from the component impl."""
    for name in (
        "_maybe_ideate",
        "force_ideate",
        "_run_ideation",
        "_compute_slots",
        "_maybe_scrub_ideation_state",
        "_cooldown_s",
        "_trigger_task_count",
        "_ideation_disabled",
    ):
        fn = getattr(_ideation, name)
        assert fn.__module__ == "ap2.components.ideation.impl", (name, fn.__module__)
    # Read-layer / shared data STAY in core `ap2.ideation`.
    assert _ideation.IDEATION_NAME == "ideation"
    assert _ideation.AUTO_APPROVE_FREEZE_THRESHOLD_DEFAULT == 3
    assert callable(_ideation.parse_operator_decisions)
    assert callable(_ideation.parse_focus_statuses)
    assert callable(_ideation.load_prompt)


def test_ideation_halt_shim_reexports_moved_symbols():
    """`ap2.ideation_halt`'s `__getattr__` shim re-exports the moved halt
    symbols, and its docstring still documents the TB-302 no-bullet
    behavior (the docs-drift pin in `test_roadmap_complete_no_bullet_append`
    reads `ideation_halt.__doc__`)."""
    for name in (
        "maybe_halt_on_exhaustion",
        "_consecutive_empty_ideation_cycles",
        "_ideation_halt_disabled",
        "_ideation_halt_empty_cycles_threshold",
        "_append_decisions_needed_bullet",
    ):
        obj = getattr(_ideation_halt, name)
        assert getattr(obj, "__module__", None) == "ap2.components.ideation.impl", name
    assert "TB-302" in (_ideation_halt.__doc__ or "")


# ---------------------------------------------------------------------------
# (4) Tick-hook wrappers: self-gate + monkeypatch-routing contract
# ---------------------------------------------------------------------------


def test_run_ideation_tick_self_gates_on_disabled(monkeypatch):
    """`run_ideation_tick` self-gates on `AP2_IDEATION_DISABLED`: truthy →
    no call to `_maybe_ideate`; falsy → routes through the core
    `ideation._maybe_ideate` attribute (so the daemon-tick monkeypatch
    contract holds)."""
    calls: list = []

    async def _rec(cfg, sdk, mcp_server):  # noqa: ARG001
        calls.append((sdk, mcp_server))

    monkeypatch.setattr(_ideation, "_maybe_ideate", _rec)

    monkeypatch.setenv("AP2_IDEATION_DISABLED", "1")
    asyncio.run(run_ideation_tick(cfg=object(), sdk="SDK"))
    assert calls == [], "disabled ideation tick must not dispatch"

    monkeypatch.setenv("AP2_IDEATION_DISABLED", "0")
    asyncio.run(run_ideation_tick(cfg=object(), sdk="SDK"))
    assert len(calls) == 1, calls
    assert calls[0][0] == "SDK"


def test_force_ideate_hook_routes_through_core(monkeypatch):
    """The `force_ideate` registry hook-point routes through
    `ideation.force_ideate` (so a patch on the core module controls it)
    and passes `(cfg, sdk, mcp_server)` through."""
    seen: list = []

    async def _rec(cfg, sdk, mcp_server):  # noqa: ARG001
        seen.append((sdk, mcp_server))

    monkeypatch.setattr(_ideation, "force_ideate", _rec)
    hook = default_registry().get("ideation").hook_points["force_ideate"]
    asyncio.run(hook(cfg=object(), sdk="SDK", mcp_server="MCP"))
    assert seen == [("SDK", "MCP")]


def test_run_ideation_halt_routes_through_core_and_always_runs(monkeypatch):
    """`run_ideation_halt` routes through `ideation_halt.maybe_halt_on_exhaustion`
    and runs REGARDLESS of `AP2_IDEATION_DISABLED` (the halt is core
    ideation lifecycle — only `AP2_IDEATION_HALT_DISABLED` suppresses the
    auto-halt). Pins the bit-for-bit pre-TB-391 semantics."""
    seen: list = []
    monkeypatch.setattr(
        _ideation_halt, "maybe_halt_on_exhaustion", lambda cfg: seen.append(cfg)
    )
    # Even with empty-board ideation disabled, the halt still runs.
    monkeypatch.setenv("AP2_IDEATION_DISABLED", "1")
    run_ideation_halt(cfg="CFG", sdk=None)
    assert seen == ["CFG"], "halt must run even when AP2_IDEATION_DISABLED=1"


def test_run_ideation_halt_self_handles_errors(monkeypatch, capsys):
    """The PRE_DISPATCH walk has no outer try/except, so `run_ideation_halt`
    self-handles its exception surface with the same `[ap2]
    maybe_halt_on_exhaustion error: ...` stderr line the inline step-0.6
    try/except emitted."""
    def _boom(cfg):  # noqa: ARG001
        raise RuntimeError("kaboom")

    monkeypatch.setattr(_ideation_halt, "maybe_halt_on_exhaustion", _boom)
    # Must NOT raise.
    run_ideation_halt(cfg=object(), sdk=None)
    err = capsys.readouterr().err
    assert "maybe_halt_on_exhaustion error" in err, err
    assert "RuntimeError" in err, err

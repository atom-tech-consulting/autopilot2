"""Ideation component manifest (TB-391 axis 4 — proposal-engine extraction).

Declares the ideation proposal engine's registry-visible shape — the
last genuine loop subsystem extracted out of core. The component owns
the natural empty-board trigger gate, the operator-forced run, the
roadmap-exhaustion halt, the ideation knob cluster, and all
`ideation_*` events; `daemon._tick` drives it purely through the registry
(no inline `ideation` / `ideation_halt` import or call).

`env_flag` = `AP2_IDEATION_DISABLED` — the default-on kill switch
(mirroring `AP2_JANITOR_DISABLED` / `AP2_CRON_DISABLED`). Set it to a
truthy value to disable empty-board ideation entirely. The knob is NOT
new — it has been ideation's opt-out since the module was first written
(the test suite + manual-only projects use it); this manifest simply
declares it as the component's `env_flag` so the registry / `ap2 status`
render its state and the `Phase.IDEATION` tick hook self-gates on it via
`Manifest.is_enabled()`. The `_maybe_ideate` body keeps its own
`_ideation_disabled(cfg)` check (the cfg-routed resolution path), so the
gate is double-checked and value-identical either way.

tick_hooks:
  - `(Phase.IDEATION, run_ideation_tick)` — the natural empty-board
    trigger gate the daemon walks at step 3.9 (it self-gates on the kill
    switch, resolves the daemon's `mcp_server`, and dispatches
    `_maybe_ideate`). This fills the phase TB-381 reserved (and walked
    empty) for exactly this extraction.
  - `(Phase.PRE_DISPATCH, run_ideation_halt)` — the roadmap-exhaustion
    detector. Registered on PRE_DISPATCH so name-sorted component order
    (`auto_approve` < `auto_unfreeze` < `ideation`) runs it after the
    auto-* sweeps and before the cron stage — exactly where the inline
    step-0.6 call fired. Unlike the trigger gate it does NOT self-gate on
    `AP2_IDEATION_DISABLED`; it always runs (only
    `AP2_IDEATION_HALT_DISABLED` suppresses the auto-halt), preserving the
    pre-TB-391 "core ideation lifecycle, always runs" semantics.

hook_points:
  - `tick_hook`     — `run_ideation_tick`, also exposed by name for
                      direct invocation / discovery.
  - `halt_hook`     — `run_ideation_halt`, the PRE_DISPATCH halt wrapper.
  - `force_ideate`  — `run_force_ideate`, the operator-forced run the
                      daemon resolves via the registry hook-point protocol
                      when the operator-queue drain sets
                      `force_ideate=True` (it can't ride the uniform
                      `(cfg, sdk)` tick-hook signature — it needs the
                      daemon's `mcp_server`).

`config_schema` is intentionally empty: the ideation operator
knobs (`AP2_IDEATION_COOLDOWN_S`, `AP2_IDEATION_TRIGGER_TASK_COUNT`,
`AP2_IDEATION_MAX_TURNS`, `AP2_IDEATION_DISABLED`,
`AP2_IDEATION_HALT_EMPTY_CYCLES`, `AP2_IDEATION_HALT_DISABLED`) stay in
the core-config cluster (`core_config_schema.CORE_CONFIG_SCHEMA`, read via
`cfg.get_core_value(...)` inside the moved bodies — value-identical), so
there is no `[components.ideation]` sub-table for the TB-322 parity walk
to require here. A future TB can migrate them to per-component config.

The registry discovers this subpackage via `pkgutil.iter_modules` over
`ap2/components/` — no hardcoded list in `ap2.registry` mentions
"ideation". Import-direction: core resolves the component via the registry
walk; it never statically imports `ap2/components/ideation/`. The CI
import-direction gate (`test_core_import_direction.py`) stays green.
"""
from __future__ import annotations

from ap2.registry import Manifest, Phase

from .impl import run_force_ideate, run_ideation_halt, run_ideation_tick


MANIFEST = Manifest(
    name="ideation",
    env_flag="AP2_IDEATION_DISABLED",
    default_enabled=True,
    hook_points={
        # The natural empty-board trigger gate (TB-391). Signature:
        # `async def run_ideation_tick(cfg, sdk) -> None`.
        "tick_hook": run_ideation_tick,
        # The roadmap-exhaustion halt wrapper (TB-391). Signature:
        # `def run_ideation_halt(cfg, sdk) -> None`.
        "halt_hook": run_ideation_halt,
        # The operator-forced run (TB-159). Resolved by `daemon._tick`
        # via the registry hook-point protocol (it needs `mcp_server`,
        # so it can't ride the uniform `(cfg, sdk)` tick-hook
        # signature). Signature:
        # `async def run_force_ideate(cfg, sdk, mcp_server) -> None`.
        "force_ideate": run_force_ideate,
    },
    tick_hooks=[
        # TB-391: the natural ideation trigger fills the `Phase.IDEATION`
        # phase TB-381 reserved (and walked empty) for this extraction.
        (Phase.IDEATION, run_ideation_tick),
        # TB-391: the roadmap-exhaustion halt runs at PRE_DISPATCH so it
        # fires after the auto-* sweeps and before cron — the slot the
        # inline step-0.6 call occupied.
        (Phase.PRE_DISPATCH, run_ideation_halt),
    ],
    dependencies=[],
)

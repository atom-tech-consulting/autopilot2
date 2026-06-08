"""Cron scheduler component manifest (TB-381 axis 1 — first tick-stage extraction).

Declares the cron scheduler's registry-visible shape: the
`AP2_CRON_DISABLED` kill switch, default-on state, and the
`Phase.CRON_DISPATCH` tick hook the daemon walks at step 1 of `_tick`
instead of running the cron loop inline.

`AP2_CRON_DISABLED` is the kill switch — set it to a truthy value
(`1`, `true`, etc.) to disable the cron scheduler entirely (no jobs
fire). Default-on (backwards-compat: existing operators don't opt in to
keep cron firing). The knob is NEW with this componentization — like the
`AP2_JANITOR_DISABLED` / `AP2_AUTO_UNFREEZE_DISABLED` kill switches added
when those subsystems became components — so default-on means existing
behavior is unchanged. It flows through `Manifest.is_enabled()`'s
`env_flag` mechanism (consulted in `ap2/registry.py` and in the
scheduler's self-gate), so it never appears as a direct
`os.environ.get(...)` call inside the component body. No other env-knob
names change: the cron interval engine, `cron_state` advance, and the
`cron_propose` / `cron_edit` write-path all keep their existing contracts
(see `impl.py`).

The registry walks `ap2/components/*/manifest.py` via
`pkgutil.iter_modules` and reads each module's `MANIFEST` attribute. No
hardcoded list in `ap2.registry` mentions "cron" — discovery is
filesystem-driven, so this component is picked up with zero registry-side
edits (goal.md L188-201). This is the canary that pins the tick-stage
extraction shape (new `Phase` members, tick-hook wiring, import-direction
boundary) that the ideation extraction (axis 3) reuses.

`config_schema` is intentionally empty: the only operator knob is the
`AP2_CRON_DISABLED` kill switch, which is consulted via
`Manifest.is_enabled()`'s `env_flag` mechanism (not via a component-body
`os.environ.get`), so there is no env-read for the TB-322 parity walk to
require a schema entry for. A future TB can add a `[components.cron]`
schema if structured-config knobs are introduced.
"""
from __future__ import annotations

from ap2.registry import Manifest, Phase

from .impl import run_cron_scheduler


MANIFEST = Manifest(
    name="cron",
    env_flag="AP2_CRON_DISABLED",
    default_enabled=True,
    hook_points={
        # TB-381: the cron scheduler tick hook the daemon dispatches by
        # walking `registry.tick_hooks(Phase.CRON_DISPATCH)`. Signature:
        # `async def run_cron_scheduler(cfg, sdk) -> None`.
        "tick_hook": run_cron_scheduler,
    },
    # TB-381 (axis 1): the scheduler is registered on the new
    # `Phase.CRON_DISPATCH` phase. Unlike janitor's `POST_CRON`
    # registration (which the daemon does NOT walk — the scheduler owns
    # janitor's invocation cadence), CRON_DISPATCH IS walked by
    # `daemon._tick` at step 1, replacing the pre-TB-381 inline
    # `load_jobs` → `due_jobs` → `run_cron` block.
    tick_hooks=[(Phase.CRON_DISPATCH, run_cron_scheduler)],
    dependencies=[],
)

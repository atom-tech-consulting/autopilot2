"""ideation component — thin package shim (TB-391 axis 4).

The implementation lives in the sibling :mod:`impl` module; this
``__init__`` re-exports the public surface so ``import
ap2.components.ideation``, every ``from ap2.components.ideation import X``
call site (the TB-391 component tests), and the sibling ``manifest.py``'s
``from .impl import …`` all keep resolving.

The proposal-engine bodies (`_maybe_ideate` / `force_ideate` /
`_run_ideation` / `_compute_slots` / `_maybe_scrub_ideation_state` / the
ideation knob readers / `maybe_halt_on_exhaustion` + the
empty-cycles accounting) moved here from `ap2/ideation.py` /
`ap2/ideation_halt.py`, which survive as back-compat ``__getattr__``
shims that re-export these symbols for non-core callers.
"""
from .impl import (
    COMPONENT_NAME,
    _compute_slots,
    _consecutive_empty_ideation_cycles,
    _cooldown_s,
    _ideation_disabled,
    _ideation_halt_disabled,
    _ideation_halt_empty_cycles_threshold,
    _maybe_ideate,
    _maybe_scrub_ideation_state,
    _run_ideation,
    _trigger_task_count,
    force_ideate,
    maybe_halt_on_exhaustion,
    run_force_ideate,
    run_ideation_halt,
    run_ideation_tick,
)

__all__ = [
    "COMPONENT_NAME",
    "_compute_slots",
    "_consecutive_empty_ideation_cycles",
    "_cooldown_s",
    "_ideation_disabled",
    "_ideation_halt_disabled",
    "_ideation_halt_empty_cycles_threshold",
    "_maybe_ideate",
    "_maybe_scrub_ideation_state",
    "_run_ideation",
    "_trigger_task_count",
    "force_ideate",
    "maybe_halt_on_exhaustion",
    "run_force_ideate",
    "run_ideation_halt",
    "run_ideation_tick",
]

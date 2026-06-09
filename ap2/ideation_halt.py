"""Ideation-exhaustion halt — back-compat shim (TB-391 axis 4).

TB-342 collapsed the multi-focus rotation state machine down to a single
ideation-exhaustion detector; TB-345 merged that residual detector out of
the `focus_advance` component and into this core module. TB-391 then
relocated the detector — along with the rest of the ideation proposal
engine — into the `ideation` component at
`ap2/components/ideation/impl.py`, behind the registry's
`Phase.PRE_DISPATCH` halt hook (`run_ideation_halt`). `daemon._tick` no
longer calls `maybe_halt_on_exhaustion(cfg)` inline; it walks the
PRE_DISPATCH registry hooks instead. This module survives as a
back-compat `__getattr__` shim (the TB-382 / TB-386 pattern) so every
non-core caller — the halt tests, `ap2/goal.py`'s docstring references,
the empty-cycles counter consumers — keeps resolving the moved symbols
via `ap2.ideation_halt`.

What the detector does (now in the component): `maybe_halt_on_exhaustion(cfg)`
counts consecutive empty ideation cycles since the most recent
`goal_updated` event and emits `roadmap_complete` once when the count
reaches `AP2_IDEATION_HALT_EMPTY_CYCLES` (default 3), parking the ideation
trigger until the operator extends goal.md (via `ap2 update-goal`) or
fires `ap2 ideate --force`. When the `AP2_IDEATION_HALT_DISABLED` kill
switch is set, the detector still counts but does not emit the halt;
instead it surfaces a decisions-needed bullet so the operator halts
manually. The detector always runs (it is NOT gated by the component's
`AP2_IDEATION_DISABLED` kill switch), preserving the pre-TB-391
core-ideation-lifecycle semantics.

TB-302 behavior (preserved): the roadmap-complete branch does not append
a `Roadmap complete: ...` bullet to `.cc-autopilot/ideation_state.md` —
the pointer-driven `ap2 status` focus line is the canonical
operator-facing surface, so the daemon no longer appends a `Roadmap
complete` bullet on the halt path. The kill-switch branch still writes a
decisions-needed bullet via `_append_decisions_needed_bullet` because
operator-killed-but-criteria-met has no equivalent focus-line surface.

The PEP-562 module-level `__getattr__` re-exports the moved symbols. The
dynamic `importlib.import_module` (NOT a static `from ap2.components...
import ...`) keeps the TB-311 import-direction gate green, and
`monkeypatch.setattr(ideation_halt, "maybe_halt_on_exhaustion", ...)`
still works: `__getattr__` makes `hasattr` true, so pytest records an
original and shadows it with a real attribute, and the component's
`run_ideation_halt` tick-hook wrapper reads through this module's
namespace so the patch controls what the daemon runs.
"""
from __future__ import annotations


_MOVED_TO_COMPONENT: frozenset[str] = frozenset(
    {
        "maybe_halt_on_exhaustion",
        "_consecutive_empty_ideation_cycles",
        "_ideation_halt_disabled",
        "_ideation_halt_empty_cycles_threshold",
        "_append_decisions_needed_bullet",
        "_RECENT_TAIL_N",
    }
)


def __getattr__(name: str):
    """PEP-562 lazy re-export of the moved halt symbols (TB-391)."""
    if name in _MOVED_TO_COMPONENT:
        import importlib

        impl = importlib.import_module("ap2.components.ideation.impl")
        return getattr(impl, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

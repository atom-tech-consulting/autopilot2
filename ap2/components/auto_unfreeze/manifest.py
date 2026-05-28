"""auto_unfreeze component manifest stub (TB-310 axis 2).

Registers `ap2.auto_unfreeze._maybe_auto_unfreeze` as the
`PRE_DISPATCH` tick hook so daemon._tick can walk
`registry.tick_hooks(Phase.PRE_DISPATCH)` instead of importing the
flat module directly. The flat module path (`ap2/auto_unfreeze.py`)
stays for now — the structural subpackage move belongs to axis (5).
This file is a thin shim; when axis (5) relocates auto_unfreeze
fully, this manifest becomes the canonical home and the wrapper
function inlines into `__init__.py`.

Observable-behavior preservation: the wrapping `_tick_hook` function
self-handles the same `auto_unfreeze_skipped reason=sweep_error`
error-event shape that daemon._tick previously emitted via an outer
try/except, so the registry-walked dispatch in `_tick` doesn't need
its own per-hook try/except (which would change the event shape).
The briefing pins observable behavior bit-for-bit.
"""
from __future__ import annotations

from ap2 import auto_unfreeze as _auto_unfreeze_mod
from ap2 import events as _events_mod
from ap2.registry import Manifest, Phase


def _tick_hook(cfg, sdk) -> None:
    """Wrap `_maybe_auto_unfreeze(cfg)` with its original error-event
    surface (the `auto_unfreeze_skipped reason=sweep_error` row the
    pre-TB-310 outer try/except emitted in `daemon._tick`).
    """
    try:
        _auto_unfreeze_mod._maybe_auto_unfreeze(cfg)
    except Exception as e:  # noqa: BLE001
        _events_mod.append(
            cfg.events_file,
            "auto_unfreeze_skipped",
            reason="sweep_error",
            error=f"{type(e).__name__}: {e}",
        )


MANIFEST = Manifest(
    name="auto_unfreeze",
    # The TB-225 / TB-233 knobs (`AP2_AUTO_UNFREEZE_FIX_SHAPES`,
    # `AP2_AUTO_UNFREEZE_DRY_RUN`, etc.) tune behavior per-call inside
    # the flat module; there is no global enable/disable knob today.
    # Default-on with `env_flag=None` per the registry's polarity rule
    # (goal.md L121-125): always-enabled unless the manifest declares
    # otherwise. Axis (5)'s structural migration may add an explicit
    # kill switch; deferred to that TB to keep this stub minimal.
    env_flag=None,
    default_enabled=True,
    hook_points={"tick_hook": _tick_hook},
    tick_hooks=[(Phase.PRE_DISPATCH, _tick_hook)],
    dependencies=[],
)

"""focus_advance component manifest (TB-313 axis 5).

Registers the focus-list pointer advance pass as the `PRE_DISPATCH`
tick hook so `daemon._tick` walks
`registry.tick_hooks(Phase.PRE_DISPATCH)` instead of importing the
flat module directly. The implementation lives intra-package at
`ap2/components/focus_advance/__init__.py` (relocated from
`ap2/focus_advance.py` by TB-313); the manifest references the
runtime symbols via `from . import …`.

Observable-behavior preservation: the wrapping `_tick_hook` function
self-handles the original stderr-print error surface that
daemon._tick previously emitted via an outer try/except (no
dedicated event type — focus_advance's only briefing-promised events
are `focus_advanced` and `roadmap_complete`; a hook-body exception
surfaces as a stderr diagnostic). The async signature matches the
existing `await _maybe_advance_focus(cfg, sdk)` call shape; the
daemon-side walk checks `asyncio.iscoroutine` on each return value
and awaits when present.

`hook_points` exposure (TB-313): the manifest publishes
`maybe_advance_focus`, `ideation_empty_against_focus`, and
`focus_recent_tail_n` so `ap2/daemon.py`'s module-level alias
rebinds (the pre-TB-313 `daemon._maybe_advance_focus = focus_advance.
_maybe_advance_focus` triad) resolve via
`default_registry().get("focus_advance").hook_points[…]` rather
than a direct `from ap2.components.focus_advance import …`. Core
must not statically import from `ap2/components/` per the TB-311
import-direction gate; the registry's hook-point dict is the
declared cross-reference path (goal.md L57-59). Constants vs.
functions both live in `hook_points`; the dict's value is just a
callable-or-value.
"""
from __future__ import annotations

import sys

from ap2.registry import Manifest, Phase

from . import (
    _FOCUS_RECENT_TAIL_N,
    _ideation_empty_against_focus,
    _maybe_advance_focus,
)


async def _tick_hook(cfg, sdk) -> None:
    """Wrap `_maybe_advance_focus(cfg, sdk)` with its original stderr
    error surface (the `[ap2] _maybe_advance_focus error: ...` line
    the pre-TB-310 outer try/except printed in `daemon._tick`).
    """
    try:
        await _maybe_advance_focus(cfg, sdk)
    except Exception as e:  # noqa: BLE001
        print(
            f"[ap2] _maybe_advance_focus error: {type(e).__name__}: {e}",
            file=sys.stderr,
        )


MANIFEST = Manifest(
    name="focus_advance",
    # TB-226's `AP2_FOCUS_AUTO_ADVANCE_DISABLED` is the kill switch the
    # subpackage already honors internally (reads `os.environ` at
    # call time inside `_maybe_advance_focus`). Mirroring that knob
    # here as the manifest-level env_flag would mean two enforcement
    # sites — the subpackage's existing kill switch is the canonical
    # one. Default-on with `env_flag=None` per the registry's polarity
    # rule.
    env_flag=None,
    default_enabled=True,
    hook_points={
        "tick_hook": _tick_hook,
        # TB-313: expose the three symbols `daemon.py` used to alias
        # from the flat module. The daemon resolves them through this
        # dict at module-load time so core never statically imports
        # from `ap2/components/focus_advance/`. Tests that import
        # these directly are exempt per the TB-311 gate's
        # `_iter_core_py_files` skip of `ap2/tests/`.
        "maybe_advance_focus": _maybe_advance_focus,
        "ideation_empty_against_focus": _ideation_empty_against_focus,
        "focus_recent_tail_n": _FOCUS_RECENT_TAIL_N,
    },
    tick_hooks=[(Phase.PRE_DISPATCH, _tick_hook)],
    dependencies=[],
)

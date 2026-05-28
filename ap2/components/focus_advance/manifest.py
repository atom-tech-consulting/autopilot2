"""focus_advance component manifest stub (TB-310 axis 2).

Registers `ap2.focus_advance._maybe_advance_focus` as the
`PRE_DISPATCH` tick hook so daemon._tick can walk
`registry.tick_hooks(Phase.PRE_DISPATCH)` instead of importing the
flat module directly. The flat module path
(`ap2/focus_advance.py`) stays for now — the structural subpackage
move belongs to axis (5).

Observable-behavior preservation: the wrapping `_tick_hook` function
self-handles the original stderr-print error surface that
daemon._tick previously emitted via an outer try/except (no
dedicated event type — focus_advance's only briefing-promised events
are `focus_advanced` and `roadmap_complete`; a hook-body exception
surfaces as a stderr diagnostic). The async signature matches the
existing `await _maybe_advance_focus(cfg, sdk)` call shape; the
daemon-side walk checks `asyncio.iscoroutine` on each return value
and awaits when present.
"""
from __future__ import annotations

import sys

from ap2 import focus_advance as _focus_advance_mod
from ap2.registry import Manifest, Phase


async def _tick_hook(cfg, sdk) -> None:
    """Wrap `_maybe_advance_focus(cfg, sdk)` with its original stderr
    error surface (the `[ap2] _maybe_advance_focus error: ...` line
    the pre-TB-310 outer try/except printed in `daemon._tick`).
    """
    try:
        await _focus_advance_mod._maybe_advance_focus(cfg, sdk)
    except Exception as e:  # noqa: BLE001
        print(
            f"[ap2] _maybe_advance_focus error: {type(e).__name__}: {e}",
            file=sys.stderr,
        )


MANIFEST = Manifest(
    name="focus_advance",
    # TB-226's `AP2_FOCUS_AUTO_ADVANCE_DISABLED` is the kill switch the
    # flat module already honors internally (reads `os.environ` at
    # call time inside `_maybe_advance_focus`). Mirroring that knob
    # here as the manifest-level env_flag would mean two enforcement
    # sites — the flat module's existing kill switch is the canonical
    # one. Default-on with `env_flag=None` per the registry's polarity
    # rule.
    env_flag=None,
    default_enabled=True,
    hook_points={"tick_hook": _tick_hook},
    tick_hooks=[(Phase.PRE_DISPATCH, _tick_hook)],
    dependencies=[],
)

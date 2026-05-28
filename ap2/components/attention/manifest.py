"""attention component manifest stub (TB-310 axis 2).

Registers the daemon-side attention wire-up
(`ap2.daemon._maybe_emit_attention_events`) as the
`ATTENTION_EMISSION` tick hook so daemon._tick can walk
`registry.tick_hooks(Phase.ATTENTION_EMISSION)` instead of calling
the helper directly.

Late-bound import: `_tick_hook` reaches into `ap2.daemon` inside its
body (not at module import) because `daemon.py` imports the
`attention` flat module + the registry, and the registry walks
`ap2/components/*/manifest.py` at discovery time — a top-level
`from ap2 import daemon` here would create a circular import. By
the time `_tick_hook` is called (inside daemon's tick), daemon is
fully loaded.

When axis (5) relocates the wire-up helper out of `daemon.py` into
this subpackage's `__init__.py`, the late-binding goes away.

The wrapper preserves the original stderr-print error surface that
daemon._tick previously emitted via an outer try/except (matching
the existing `[ap2] _maybe_emit_attention_events error: ...`
diagnostic line — no dedicated event type, just stderr).
"""
from __future__ import annotations

import sys

from ap2.registry import Manifest, Phase


def _tick_hook(cfg, sdk) -> None:
    """Wrap `daemon._maybe_emit_attention_events(cfg)` with its original
    stderr error surface (the `[ap2] _maybe_emit_attention_events error:
    ...` line the pre-TB-310 outer try/except printed in `_tick`).
    """
    from ap2 import daemon as _daemon_mod
    try:
        _daemon_mod._maybe_emit_attention_events(cfg)
    except Exception as e:  # noqa: BLE001
        print(
            f"[ap2] _maybe_emit_attention_events error: "
            f"{type(e).__name__}: {e}",
            file=sys.stderr,
        )


MANIFEST = Manifest(
    name="attention",
    # The TB-282 attention-debounce knob (`AP2_ATTENTION_DEBOUNCE_S`)
    # tunes per-(type, key) suppression inside the flat module's
    # `should_suppress`; the immediate-push knob
    # (`AP2_ATTENTION_IMMEDIATE_PUSH`) is daemon-side. Neither is a
    # component enable/disable. Default-on with `env_flag=None`.
    env_flag=None,
    default_enabled=True,
    hook_points={"tick_hook": _tick_hook},
    tick_hooks=[(Phase.ATTENTION_EMISSION, _tick_hook)],
    dependencies=[],
)

"""attention component manifest (TB-315 axis 5).

Registers the proactive attention-detector → `attention_raised` event
wire-up as the `ATTENTION_EMISSION` tick hook so `daemon._tick` walks
`registry.tick_hooks(Phase.ATTENTION_EMISSION)` instead of importing
the flat module directly. The implementation lives intra-package at
`ap2/components/attention/__init__.py` (relocated from
`ap2/attention.py` by TB-315); the manifest references the runtime
symbols via `from . import …` rather than the pre-TB-315
late-binding `from ap2 import daemon as _daemon_mod` shape.

Observable-behavior preservation: the wrapping `_tick_hook` function
preserves the same `[ap2] _maybe_emit_attention_events error: ...`
stderr-print error surface the pre-TB-310 outer try/except emitted in
`daemon._tick`. Since attention emits no dedicated `*_error` event
type today, a hook-body exception surfaces as a stderr diagnostic
line — bit-for-bit pin against the briefing's "purely structural
refactor, zero behavior change" promise.

`hook_points` exposure (TB-315): the manifest publishes every symbol
the daemon previously imported from `ap2/attention.py` (the flat
module path that's gone now) PLUS the daemon-side wire-up helpers
that moved into the subpackage in the same task
(`_maybe_emit_attention_events`, `_maybe_push_attention`, and the
push-state file helpers). The daemon's module-level alias block
resolves each via
`default_registry().get("attention").hook_points[…]` rather than a
direct `from ap2.components.attention import …`. Core must not
statically import from `ap2/components/` per the TB-311 import-
direction gate; the registry's hook-point dict is the declared
cross-reference path (goal.md L57-59). Constants vs. functions /
classes both live in `hook_points`; the dict's value is just a
callable-or-value.
"""
from __future__ import annotations

import sys

from ap2.registry import Manifest, Phase

from . import (
    AttentionCondition,
    _attention_debounce_s,
    _attention_push_state_path,
    _cost_approach_pct,
    _is_attention_immediate_push_enabled,
    _load_attention_push_state,
    _maybe_emit_attention_events,
    _maybe_push_attention,
    _parse_ts,
    _save_attention_push_state,
    _task_frozen_recency_s,
    _task_stuck_threshold_s,
    detect_attention_conditions,
    find_last_attention_fire,
    should_suppress,
)


def _tick_hook(cfg, sdk) -> None:
    """Wrap `_maybe_emit_attention_events(cfg)` with its original
    stderr error surface (the `[ap2] _maybe_emit_attention_events
    error: ...` line the pre-TB-310 outer try/except printed in
    `daemon._tick`).

    TB-315: body-local call into the subpackage's
    `_maybe_emit_attention_events`. Pre-TB-315 this function
    late-bound via `from ap2 import daemon as _daemon_mod`; the body
    now lives intra-package so the late-binding shim is gone.
    """
    try:
        _maybe_emit_attention_events(cfg)
    except Exception as e:  # noqa: BLE001
        print(
            f"[ap2] _maybe_emit_attention_events error: "
            f"{type(e).__name__}: {e}",
            file=sys.stderr,
        )


MANIFEST = Manifest(
    name="attention",
    # The TB-282 attention-debounce knob (`AP2_ATTENTION_DEBOUNCE_S`)
    # tunes per-(type, key) suppression inside `should_suppress`; the
    # immediate-push knob (`AP2_ATTENTION_IMMEDIATE_PUSH`) tunes the
    # push branch of `_maybe_push_attention`. Neither is a component
    # enable/disable. Default-on with `env_flag=None` per the
    # registry's polarity rule (goal.md L121-125): always-enabled
    # unless the manifest declares otherwise.
    env_flag=None,
    default_enabled=True,
    hook_points={
        "tick_hook": _tick_hook,
        # TB-315: expose the full daemon-alias surface — every symbol
        # the daemon's pre-TB-315 `attention.<sym>` direct call sites
        # AND the daemon-side wire-up helpers (`_maybe_emit_attention_events`
        # and friends) that moved into the subpackage in this task. The
        # daemon's module-level rebind block resolves each via
        # `default_registry().get("attention").hook_points[…]` rather
        # than a direct `from ap2.components.attention import …`.
        # Tests that import these symbols directly are exempt per the
        # TB-311 gate's `_iter_core_py_files` skip of `ap2/tests/`.
        "AttentionCondition": AttentionCondition,
        "detect_attention_conditions": detect_attention_conditions,
        "find_last_attention_fire": find_last_attention_fire,
        "should_suppress": should_suppress,
        "parse_ts": _parse_ts,
        "task_stuck_threshold_s": _task_stuck_threshold_s,
        "task_frozen_recency_s": _task_frozen_recency_s,
        "cost_approach_pct": _cost_approach_pct,
        "attention_debounce_s": _attention_debounce_s,
        # Daemon-side wire-up helpers (relocated from daemon.py by
        # TB-315; the daemon's module-level alias block re-exposes
        # them as `daemon._maybe_emit_attention_events` etc. so test
        # paths that monkey-patch via `from ap2.daemon import …` stay
        # working).
        "maybe_emit_attention_events": _maybe_emit_attention_events,
        "maybe_push_attention": _maybe_push_attention,
        "attention_push_state_path": _attention_push_state_path,
        "load_attention_push_state": _load_attention_push_state,
        "save_attention_push_state": _save_attention_push_state,
        "is_attention_immediate_push_enabled": _is_attention_immediate_push_enabled,
    },
    tick_hooks=[(Phase.ATTENTION_EMISSION, _tick_hook)],
    dependencies=[],
)

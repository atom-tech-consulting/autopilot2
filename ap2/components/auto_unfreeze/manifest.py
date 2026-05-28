"""auto_unfreeze component manifest (TB-314 axis 5).

Registers the auto-unfreeze briefing-shape fix sweep as the
`PRE_DISPATCH` tick hook so `daemon._tick` walks
`registry.tick_hooks(Phase.PRE_DISPATCH)` instead of importing the
flat module directly. The implementation lives intra-package at
`ap2/components/auto_unfreeze/__init__.py` (relocated from
`ap2/auto_unfreeze.py` by TB-314); the manifest references the
runtime symbols via `from . import …`.

Observable-behavior preservation: the wrapping `_tick_hook` function
self-handles the same `auto_unfreeze_skipped reason=sweep_error`
error-event shape that daemon._tick previously emitted via an outer
try/except, so the registry-walked dispatch in `_tick` doesn't need
its own per-hook try/except (which would change the event shape).
The briefing pins observable behavior bit-for-bit.

`hook_points` exposure (TB-314): the manifest publishes every symbol
the daemon's pre-TB-314 module-level alias block at L1781-1793
sourced from the flat module so the rebinds in `ap2/daemon.py`
resolve via `default_registry().get("auto_unfreeze").hook_points[…]`
rather than a direct `from ap2.components.auto_unfreeze import …`.
Core must not statically import from `ap2/components/` per the
TB-311 import-direction gate; the registry's hook-point dict is the
declared cross-reference path (goal.md L57-59). Constants vs.
functions both live in `hook_points`; the dict's value is just a
callable-or-value.
"""
from __future__ import annotations

from ap2 import events as _events_mod
from ap2.config_loader import ConfigKey
from ap2.registry import Manifest, Phase

from . import (
    _AUTO_UNFREEZE_MAX_PER_DAY_DEFAULT,
    _AUTO_UNFREEZE_MAX_PER_TASK_DEFAULT,
    _AUTO_UNFREEZE_WINDOW_S,
    _apply_auto_unfreeze_patch,
    _auto_unfreeze_allowlist,
    _auto_unfreeze_dry_run,
    _auto_unfreeze_max_per_day,
    _auto_unfreeze_max_per_task,
    _count_auto_unfreeze_applied_for_task,
    _count_auto_unfreeze_applied_in_window,
    _maybe_auto_unfreeze,
    _most_recent_blocked_complete_for,
    _shared_parse,
)


def _tick_hook(cfg, sdk) -> None:
    """Wrap `_maybe_auto_unfreeze(cfg)` with its original error-event
    surface (the `auto_unfreeze_skipped reason=sweep_error` row the
    pre-TB-310 outer try/except emitted in `daemon._tick`).
    """
    try:
        _maybe_auto_unfreeze(cfg)
    except Exception as e:  # noqa: BLE001
        _events_mod.append(
            cfg.events_file,
            "auto_unfreeze_skipped",
            reason="sweep_error",
            error=f"{type(e).__name__}: {e}",
        )


MANIFEST = Manifest(
    name="auto_unfreeze",
    # TB-320: kill-switch knob `AP2_AUTO_UNFREEZE_DISABLED` (new in
    # TB-320 — mirrors the polarity / naming of `AP2_JANITOR_DISABLED`
    # and `AP2_VALIDATOR_JUDGE_DISABLED`). `default_enabled=True` →
    # suppress-polarity per `registry.Manifest.is_enabled`'s "env_flag
    # set + default_enabled True → truthy disables" branch
    # (`ap2/registry.py:189-194`). The TB-225 / TB-233 knobs
    # (`AP2_AUTO_UNFREEZE_FIX_SHAPES`, `AP2_AUTO_UNFREEZE_DRY_RUN`,
    # etc.) still tune behavior per-call inside the subpackage; the
    # new kill switch is the operator's coarser-grained "disable the
    # entire sweep" lever (existing fix-shapes opt-in still applies
    # for finer-grained gating). The subpackage's `_tick_hook` self-
    # gates on the new knob at the top so observable behavior matches
    # what the registry advertises (registry-level filtering is
    # informational; the daemon walks every component's tick hook
    # regardless of env_flag).
    env_flag="AP2_AUTO_UNFREEZE_DISABLED",
    default_enabled=True,
    hook_points={
        "tick_hook": _tick_hook,
        # TB-314: expose every symbol `daemon.py`'s pre-migration alias
        # block at L1781-1793 sourced from the flat module so core
        # resolves the rebinds via the registry rather than statically
        # importing from `ap2/components/auto_unfreeze/`. Tests that
        # import these symbols directly are exempt per the TB-311
        # gate's `_iter_core_py_files` skip of `ap2/tests/`.
        "AUTO_UNFREEZE_MAX_PER_DAY_DEFAULT": _AUTO_UNFREEZE_MAX_PER_DAY_DEFAULT,
        "AUTO_UNFREEZE_MAX_PER_TASK_DEFAULT": _AUTO_UNFREEZE_MAX_PER_TASK_DEFAULT,
        "AUTO_UNFREEZE_WINDOW_S": _AUTO_UNFREEZE_WINDOW_S,
        "apply_auto_unfreeze_patch": _apply_auto_unfreeze_patch,
        "auto_unfreeze_allowlist": _auto_unfreeze_allowlist,
        "auto_unfreeze_dry_run": _auto_unfreeze_dry_run,
        "auto_unfreeze_max_per_day": _auto_unfreeze_max_per_day,
        "auto_unfreeze_max_per_task": _auto_unfreeze_max_per_task,
        "count_auto_unfreeze_applied_for_task": _count_auto_unfreeze_applied_for_task,
        "count_auto_unfreeze_applied_in_window": _count_auto_unfreeze_applied_in_window,
        "maybe_auto_unfreeze": _maybe_auto_unfreeze,
        "most_recent_blocked_complete_for": _most_recent_blocked_complete_for,
        "shared_parse": _shared_parse,
    },
    tick_hooks=[(Phase.PRE_DISPATCH, _tick_hook)],
    dependencies=[],
    # TB-322 (axis 3): per-component `config_schema` declarations for
    # every auto-unfreeze knob the subpackage reads via
    # `os.environ.get` in `ap2/components/auto_unfreeze/__init__.py`
    # (the five `AP2_AUTO_UNFREEZE_DISABLED` /
    # `AP2_AUTO_UNFREEZE_FIX_SHAPES` / `AP2_AUTO_UNFREEZE_DRY_RUN` /
    # `AP2_AUTO_UNFREEZE_MAX_PER_TASK` /
    # `AP2_AUTO_UNFREEZE_MAX_PER_DAY` call sites).
    # Defaults mirror the in-source `_AUTO_UNFREEZE_*_DEFAULT`
    # constants (`__init__.py` L61-63 + the falsy-default booleans);
    # every knob here is in `env_reload.HOT_RELOADABLE_KNOBS`
    # (per-call lazy reads — tune-without-restart per TB-225 / TB-233 /
    # TB-320), so `hot_reloadable=True` across the board.
    config_schema={
        "disabled": ConfigKey(
            name="disabled",
            type=bool,
            default=False,
            description=(
                "Kill switch for the auto-unfreeze sweep (TB-320). "
                "True short-circuits `_maybe_auto_unfreeze` entirely. "
                "Mirrors `AP2_AUTO_UNFREEZE_DISABLED`; in "
                "`HOT_RELOADABLE_KNOBS`."
            ),
            hot_reloadable=True,
        ),
        "fix_shapes": ConfigKey(
            name="fix_shapes",
            type=str,
            default="",
            description=(
                "Comma-separated allowlist of fix-shape tokens "
                "(TB-225). Non-empty enables auto-unfreeze attempts "
                "for the listed shapes; default empty means the "
                "feature is off. Mirrors `AP2_AUTO_UNFREEZE_FIX_SHAPES`."
            ),
            hot_reloadable=True,
        ),
        "dry_run": ConfigKey(
            name="dry_run",
            type=bool,
            default=False,
            description=(
                "Monitor-only on-ramp (TB-233). When True alongside "
                "a non-empty `fix_shapes`, the sweep runs the entire "
                "guard chain but emits `would_auto_unfreeze` instead "
                "of patching the briefing. Mirrors "
                "`AP2_AUTO_UNFREEZE_DRY_RUN`."
            ),
            hot_reloadable=True,
        ),
        "max_per_task": ConfigKey(
            name="max_per_task",
            type=int,
            default=1,
            description=(
                "Per-task cap on auto-unfreeze applications across "
                "the rolling 24h window (TB-225). A task that's been "
                "auto-unfrozen `max_per_task` times falls back to "
                "manual `ap2 unfreeze`. Mirrors "
                "`AP2_AUTO_UNFREEZE_MAX_PER_TASK`; 0 disables the cap."
            ),
            hot_reloadable=True,
        ),
        "max_per_day": ConfigKey(
            name="max_per_day",
            type=int,
            default=3,
            description=(
                "Per-day cap on total auto-unfreeze applications "
                "across all tasks (rolling 24h window). When "
                "exceeded the daemon halts and surfaces a "
                "`## Decisions needed from operator` bullet. Mirrors "
                "`AP2_AUTO_UNFREEZE_MAX_PER_DAY`; 0 disables the cap."
            ),
            hot_reloadable=True,
        ),
    },
)

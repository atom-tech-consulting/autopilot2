"""attention component — thin package shim (TB-343).

The implementation lives in the sibling :mod:`impl` module; this
``__init__`` re-exports the public surface so ``import
ap2.components.attention``, every ``from ap2.components.attention
import X`` call site, and the sibling ``manifest.py``'s ``from . import
…`` all keep resolving unchanged.

TB-343 moved the module body out of ``__init__.py`` into ``impl.py``
(``git mv``, history-preserving) to match the conventional package
shape — ``__init__`` is thin glue, the implementation is a named
module. The re-export list below is the component's full symbol
surface (functions, classes, constants, and the four ``DEFAULT_*``
knob defaults forwarded from ``ap2.config``); mutable module-level
state stays private to ``impl``.
"""
from .impl import (
    AttentionCondition,
    DEFAULT_ATTENTION_DEBOUNCE_S,
    DEFAULT_AUTO_APPROVE_COST_APPROACH_PCT,
    DEFAULT_TASK_FROZEN_RECENCY_S,
    DEFAULT_TASK_STUCK_THRESHOLD_S,
    _FREEZE_ENTRY_EVENT_TYPES,
    _FREEZE_RESOLVED_EVENT_TYPES,
    _TERMINAL_TASK_EVENT_TYPES,
    _attention_debounce_s,
    _attention_push_state_path,
    _cost_approach_pct,
    _detect_auto_approve_paused,
    _detect_cost_cap_approach,
    _detect_task_frozen,
    _detect_task_stuck,
    _detect_validator_judge_noisy,
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

__all__ = [
    "AttentionCondition",
    "DEFAULT_ATTENTION_DEBOUNCE_S",
    "DEFAULT_AUTO_APPROVE_COST_APPROACH_PCT",
    "DEFAULT_TASK_FROZEN_RECENCY_S",
    "DEFAULT_TASK_STUCK_THRESHOLD_S",
    "_FREEZE_ENTRY_EVENT_TYPES",
    "_FREEZE_RESOLVED_EVENT_TYPES",
    "_TERMINAL_TASK_EVENT_TYPES",
    "_attention_debounce_s",
    "_attention_push_state_path",
    "_cost_approach_pct",
    "_detect_auto_approve_paused",
    "_detect_cost_cap_approach",
    "_detect_task_frozen",
    "_detect_task_stuck",
    "_detect_validator_judge_noisy",
    "_is_attention_immediate_push_enabled",
    "_load_attention_push_state",
    "_maybe_emit_attention_events",
    "_maybe_push_attention",
    "_parse_ts",
    "_save_attention_push_state",
    "_task_frozen_recency_s",
    "_task_stuck_threshold_s",
    "detect_attention_conditions",
    "find_last_attention_fire",
    "should_suppress",
]

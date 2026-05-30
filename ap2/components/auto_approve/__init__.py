"""auto_approve component — thin package shim (TB-343).

The implementation lives in the sibling :mod:`impl` module; this
``__init__`` re-exports the public surface so ``import
ap2.components.auto_approve``, every ``from ap2.components.auto_approve
import X`` call site, and the sibling ``manifest.py``'s ``from . import
…`` all keep resolving unchanged.

TB-343 moved the module body out of ``__init__.py`` into ``impl.py``
(``git mv``, history-preserving) to match the conventional package
shape. The re-export list below is the component's full symbol surface.
"""
from .impl import (
    _AUTO_APPROVE_FAILURE_STATUSES,
    _AUTO_APPROVE_UNFREEZE_TOKEN,
    _AUTO_APPROVE_WINDOW_RESUME_TOKEN,
    _AUTO_APPROVE_WINDOW_S,
    _append_decisions_needed_bullet,
    _auto_approve_already_halted,
    _auto_approve_check_violations,
    _auto_approve_freeze_threshold,
    _auto_approve_paused,
    _auto_approve_window_resume_idx,
    _auto_approved_task_ids,
    _event_combined_tokens,
    _parse_event_ts,
    _per_task_token_cap,
    _validator_judge_noisy_paused,
    _was_auto_approved,
    _window_token_cap,
    evaluate_auto_approve_decision,
)

__all__ = [
    "_AUTO_APPROVE_FAILURE_STATUSES",
    "_AUTO_APPROVE_UNFREEZE_TOKEN",
    "_AUTO_APPROVE_WINDOW_RESUME_TOKEN",
    "_AUTO_APPROVE_WINDOW_S",
    "_append_decisions_needed_bullet",
    "_auto_approve_already_halted",
    "_auto_approve_check_violations",
    "_auto_approve_freeze_threshold",
    "_auto_approve_paused",
    "_auto_approve_window_resume_idx",
    "_auto_approved_task_ids",
    "_event_combined_tokens",
    "_parse_event_ts",
    "_per_task_token_cap",
    "_validator_judge_noisy_paused",
    "_was_auto_approved",
    "_window_token_cap",
    "evaluate_auto_approve_decision",
]

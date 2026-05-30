"""auto_unfreeze component — thin package shim (TB-343).

The implementation lives in the sibling :mod:`impl` module; this
``__init__`` re-exports the public surface so ``import
ap2.components.auto_unfreeze``, every ``from
ap2.components.auto_unfreeze import X`` call site, and the sibling
``manifest.py``'s ``from . import …`` all keep resolving unchanged.

TB-343 moved the module body out of ``__init__.py`` into ``impl.py``
(``git mv``, history-preserving) to match the conventional package
shape. The re-export list below is the component's symbol surface; the
mutable ``_DISABLED_EVENT_EMITTED`` flag stays private to ``impl`` (it
is toggled via ``global`` and is reachable through the re-exported
``_reset_disabled_event_emitted_for_tests`` helper).
"""
from .impl import (
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
    _is_auto_unfreeze_disabled,
    _maybe_auto_unfreeze,
    _most_recent_blocked_complete_for,
    _reset_disabled_event_emitted_for_tests,
    _shared_parse,
)

__all__ = [
    "_AUTO_UNFREEZE_MAX_PER_DAY_DEFAULT",
    "_AUTO_UNFREEZE_MAX_PER_TASK_DEFAULT",
    "_AUTO_UNFREEZE_WINDOW_S",
    "_apply_auto_unfreeze_patch",
    "_auto_unfreeze_allowlist",
    "_auto_unfreeze_dry_run",
    "_auto_unfreeze_max_per_day",
    "_auto_unfreeze_max_per_task",
    "_count_auto_unfreeze_applied_for_task",
    "_count_auto_unfreeze_applied_in_window",
    "_is_auto_unfreeze_disabled",
    "_maybe_auto_unfreeze",
    "_most_recent_blocked_complete_for",
    "_reset_disabled_event_emitted_for_tests",
    "_shared_parse",
]

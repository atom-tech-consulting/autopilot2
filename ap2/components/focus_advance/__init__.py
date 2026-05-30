"""focus_advance component — thin package shim (TB-343).

The implementation lives in the sibling :mod:`impl` module; this
``__init__`` re-exports the public surface so ``import
ap2.components.focus_advance``, every ``from
ap2.components.focus_advance import X`` call site, and the sibling
``manifest.py``'s ``from . import …`` all keep resolving unchanged.

TB-343 moved the module body out of ``__init__.py`` into ``impl.py``
(``git mv``, history-preserving) to match the conventional package
shape. The TB-302 no-bullet-on-roadmap-complete behavior and its
docstring now live in ``impl.py`` alongside the body.
"""
from .impl import (
    _FOCUS_RECENT_TAIL_N,
    _advance_empty_cycles_threshold,
    _focus_auto_advance_disabled,
    _ideation_empty_against_focus,
    _maybe_advance_focus,
)

__all__ = [
    "_FOCUS_RECENT_TAIL_N",
    "_advance_empty_cycles_threshold",
    "_focus_auto_advance_disabled",
    "_ideation_empty_against_focus",
    "_maybe_advance_focus",
]

"""cron scheduler component — thin package shim (TB-381 axis 1).

The implementation lives in the sibling :mod:`impl` module; this
``__init__`` re-exports the public surface so ``import
ap2.components.cron``, every ``from ap2.components.cron import X`` call
site (e.g. the cron-dispatch tests that previously imported
``run_cron`` from ``ap2.daemon``), and the sibling ``manifest.py``'s
``from .impl import …`` all keep resolving.
"""
from .impl import (
    COMPONENT_NAME,
    resolve_cron_handler,
    run_cron,
    run_cron_scheduler,
)

__all__ = [
    "COMPONENT_NAME",
    "resolve_cron_handler",
    "run_cron",
    "run_cron_scheduler",
]

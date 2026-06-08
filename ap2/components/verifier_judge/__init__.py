"""verifier_judge component — thin package shim (TB-382).

The implementation lives in the sibling :mod:`impl` module; this
``__init__`` re-exports the public surface so ``from
ap2.components.verifier_judge import _judge_prose_bullet`` and the
sibling ``manifest.py``'s ``from . import …`` both resolve unchanged.

Mirrors the package shape established by ``validator_judge`` /
``janitor`` (TB-343): the module body lives in ``impl.py`` and the
manifest is the registry-registration file.
"""
from .impl import _judge_prose_bullet

__all__ = [
    "_judge_prose_bullet",
]

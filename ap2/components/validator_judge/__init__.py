"""validator_judge component — thin package shim (TB-343).

The implementation lives in the sibling :mod:`impl` module; this
``__init__`` re-exports the public surface so ``import
ap2.components.validator_judge``, every ``from
ap2.components.validator_judge import X`` call site, and the sibling
``manifest.py``'s ``from . import …`` all keep resolving unchanged.

TB-343 moved the module body out of ``__init__.py`` into ``impl.py``
(``git mv``, history-preserving) to match the conventional package
shape. The re-export list below is the component's full symbol surface.
"""
from .impl import (
    _BRIEFING_SLICE_HEADINGS,
    _DEP_JUDGE_PARSE_ERRORS,
    _DepJudgeOutcome,
    _DepJudgeTimeout,
    _VALIDATOR_JUDGE_DEPRECATED_KNOB_CEIL,
    _VALIDATOR_JUDGE_DEPRECATED_KNOB_LOGGED,
    _VALIDATOR_JUDGE_MAX_TOKENS_DEFAULT,
    _VALIDATOR_JUDGE_MAX_TURNS_DEFAULT,
    _VALIDATOR_JUDGE_MODEL,
    _VALIDATOR_JUDGE_TIMEOUT_S_DEFAULT,
    _check_dependency_coherence,
    _judge_dep_coherence_default,
    _parse_dep_judge_response,
    _slice_briefing_for_dep_judge,
    _validator_judge_disabled,
    _validator_judge_max_tokens_legacy,
    _validator_judge_max_turns,
    _validator_judge_timeout_s,
)

__all__ = [
    "_BRIEFING_SLICE_HEADINGS",
    "_DEP_JUDGE_PARSE_ERRORS",
    "_DepJudgeOutcome",
    "_DepJudgeTimeout",
    "_VALIDATOR_JUDGE_DEPRECATED_KNOB_CEIL",
    "_VALIDATOR_JUDGE_DEPRECATED_KNOB_LOGGED",
    "_VALIDATOR_JUDGE_MAX_TOKENS_DEFAULT",
    "_VALIDATOR_JUDGE_MAX_TURNS_DEFAULT",
    "_VALIDATOR_JUDGE_MODEL",
    "_VALIDATOR_JUDGE_TIMEOUT_S_DEFAULT",
    "_check_dependency_coherence",
    "_judge_dep_coherence_default",
    "_parse_dep_judge_response",
    "_slice_briefing_for_dep_judge",
    "_validator_judge_disabled",
    "_validator_judge_max_tokens_legacy",
    "_validator_judge_max_turns",
    "_validator_judge_timeout_s",
]

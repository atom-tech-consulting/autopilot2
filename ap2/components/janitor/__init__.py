"""janitor component — thin package shim (TB-343).

The implementation lives in the sibling :mod:`impl` module; this
``__init__`` re-exports the public surface so ``import
ap2.components.janitor``, every ``from ap2.components.janitor import
X`` call site, and the sibling ``manifest.py``'s ``from . import …``
all keep resolving unchanged.

TB-343 moved the module body out of ``__init__.py`` into ``impl.py``
(``git mv``, history-preserving) to match the conventional package
shape. The re-export list below is the component's full symbol surface.
"""
from .impl import (
    JUDGE_REPO_READ_TOOLS,
    JanitorFinding,
    JanitorReport,
    KNOWN_VERDICTS,
    MIN_MODIFIED_AGE_S,
    RECENT_FINDING_WINDOW_S,
    VERDICT_AMBIGUOUS,
    VERDICT_OPERATOR_DRAFT,
    VERDICT_REAL_STRAND,
    _AP2_JANITOR_JUDGE_MAX_TURNS_DEFAULT,
    _AP2_JANITOR_MAX_FINDINGS_LLM_DEFAULT,
    _EXCLUDED_FILES,
    _EXCLUDED_PREFIXES,
    _JUDGE_EVENTS_TAIL_N,
    _JUDGE_LIFECYCLE_EVENT_TYPES,
    _JUDGE_REASONING_MAX_CHARS,
    _JUDGE_RECENT_COMMITS_N,
    _build_judge_shared_context,
    _check_modified_not_staged,
    _check_staged_uncommitted,
    _check_untracked_non_ignored,
    _is_excluded,
    _judge_effort,
    _judge_finding,
    _judge_max_turns,
    _max_findings_llm,
    _parse_judge_response,
    _path_mtime,
    _porcelain_lines,
    _run_git,
    _staged_paths,
    recent_finding_count,
    recent_finding_counts_by_verdict,
    run_janitor,
)

__all__ = [
    "JUDGE_REPO_READ_TOOLS",
    "JanitorFinding",
    "JanitorReport",
    "KNOWN_VERDICTS",
    "MIN_MODIFIED_AGE_S",
    "RECENT_FINDING_WINDOW_S",
    "VERDICT_AMBIGUOUS",
    "VERDICT_OPERATOR_DRAFT",
    "VERDICT_REAL_STRAND",
    "_AP2_JANITOR_JUDGE_MAX_TURNS_DEFAULT",
    "_AP2_JANITOR_MAX_FINDINGS_LLM_DEFAULT",
    "_EXCLUDED_FILES",
    "_EXCLUDED_PREFIXES",
    "_JUDGE_EVENTS_TAIL_N",
    "_JUDGE_LIFECYCLE_EVENT_TYPES",
    "_JUDGE_REASONING_MAX_CHARS",
    "_JUDGE_RECENT_COMMITS_N",
    "_build_judge_shared_context",
    "_check_modified_not_staged",
    "_check_staged_uncommitted",
    "_check_untracked_non_ignored",
    "_is_excluded",
    "_judge_effort",
    "_judge_finding",
    "_judge_max_turns",
    "_max_findings_llm",
    "_parse_judge_response",
    "_path_mtime",
    "_porcelain_lines",
    "_run_git",
    "_staged_paths",
    "recent_finding_count",
    "recent_finding_counts_by_verdict",
    "run_janitor",
]

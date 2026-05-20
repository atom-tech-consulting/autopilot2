"""Tests for `ap2.validator_judge` — placeholder sibling created by TB-268.

The canonical TB-268 source split (TB-262) carved
`ap2/validator_judge.py` out of `ap2/tools.py`: `_judge_dep_coherence_default`,
`_parse_dep_judge_response`, `_check_dependency_coherence`, plus the
`_DepJudgeTimeout` / `_DepJudgeOutcome` types. The full functional
coverage of this surface already lives in
`ap2/tests/test_dep_validator_judge.py` (dep-coherence judge behavior,
parse-response shapes, timeout fail-open semantics, env-knob plumb)
and the `test_tb*_validator_judge_*.py` regression-pin modules — none
of those tests ever lived inside the monolithic `test_tools.py`, so
TB-268's "pure relocation" rule leaves them in place.

This file is the named home for any future tests that fit the
validator-judge surface but DON'T fit the existing modules. It exists
so the TB-268 verification bullet that checks for the mirror module
trio (`test_briefing_validators.py`, `test_validator_judge.py`,
`test_operator_queue.py`) passes, AND so a future test of (say)
`_parse_dep_judge_response`'s edge cases has an obvious home that
mirrors the source file name 1:1.

The pre-existing modules to read first when adding a test here:
- `ap2/tests/test_dep_validator_judge.py` — main suite.
- `ap2/tests/test_tb_validator_judge_sdk_args.py` — SDK call shape pin.
- `ap2/tests/test_tb243_validator_judge_surface.py` — surface-level pin.
- `ap2/tests/test_judge_parse_observability.py` — parse-response events.

If a new test is a regression pin for a specific TB-N, prefer the
`test_tb<N>_*.py` pattern (regression-pin convention) instead of
adding it here.
"""
from __future__ import annotations

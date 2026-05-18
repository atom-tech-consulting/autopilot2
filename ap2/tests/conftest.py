"""Top-level pytest conftest for `ap2/tests/` — judge-shield default.

TB-254 (surgical mirror of `ap2/tests/e2e/conftest.py`'s shield, line 66):
set `AP2_VALIDATOR_JUDGE_DISABLED=1` by default for the entire unit-test
session. Without this shield, any unit test that exercises
`tools.do_board_edit({"action": "add_*"})` or
`tools.do_operator_queue_append({"op": "add_*"})` would dispatch real
Haiku-4.5 SDK calls per invocation via TB-235's
`_check_dependency_coherence` (check #7 of
`_validate_briefing_structure`). That is expensive in cumulative test
wall-clock (10-18s per call; n=18 of the top-20 slowest tests in
TB-253's investigation artifact were dominated by this leak) and
potentially makes live API calls from CI.

The shield is the smallest-blast-radius fix identified by TB-253's
investigation at `.cc-autopilot/insights/test-suite-slowness-2026-05-17.md`
(Option 1 in the headline finding). The e2e directory already had its
own shield in `ap2/tests/e2e/conftest.py` for the same reason — this
top-level conftest is the surgical mirror for the unit-test surface.

Why `os.environ.setdefault` rather than direct assignment: an operator
who wants to verify the validator IS firing locally can override via
the shell (`AP2_VALIDATOR_JUDGE_DISABLED=0 uv run pytest -q ap2/tests/`)
without editing this file. Direct assignment would shadow operator
intent silently.

Why module-level (import-time) rather than a session-scoped autouse
fixture: pytest imports conftest.py once per session before collecting
tests, so the env var is set before any test or fixture runs. An
autouse fixture only activates on first test invocation — same effect
in practice, but the import-time form matches the existing
`e2e/conftest.py` pattern exactly and skips one layer of indirection.

The two intentional-judge-exercising modules
(`ap2/tests/test_dep_validator_judge.py` and
`ap2/tests/test_tb243_validator_judge_surface.py`) remain free to
override the shield per-test via `monkeypatch.delenv`. The shield is
the safe default; the override is the explicit opt-in for the
modules that test the judge itself.
"""
from __future__ import annotations

import os

# Surgical mirror of `ap2/tests/e2e/conftest.py:66`. Set as the
# session default so every unit test under `ap2/tests/` inherits the
# shield. Tests that need the judge to fire override via
# `monkeypatch.delenv("AP2_VALIDATOR_JUDGE_DISABLED", raising=False)`.
#
# Edge case: shells that `export AP2_VALIDATOR_JUDGE_DISABLED=` (no
# value, empty string) make the key present in `os.environ` so
# `setdefault` would leave the empty string alone and the shield
# wouldn't take effect (the validator's
# `os.environ.get(...).lower() in {"1","true","yes"}` test rejects
# the empty string and fires the judge anyway). Treat an unset OR
# empty value as "operator did not opt out" — strip it first so
# `setdefault` then installs the shield value. Any other operator-set
# value (e.g. `0` to opt out and verify the judge fires locally) is
# preserved untouched.
if not os.environ.get("AP2_VALIDATOR_JUDGE_DISABLED", "").strip():
    os.environ.pop("AP2_VALIDATOR_JUDGE_DISABLED", None)

# `setdefault` preserves the operator-shell override
# (`AP2_VALIDATOR_JUDGE_DISABLED=0 uv run pytest -q ap2/tests/`) so a
# local "did the judge actually fire?" check is one knob away.
os.environ.setdefault("AP2_VALIDATOR_JUDGE_DISABLED", "1")

"""TB-254: regression-pin tests for the top-level `ap2/tests/conftest.py`
judge-shield.

Three behavioral cases (mirror the briefing's Scope §4 contract):

  (1) `test_validator_judge_disabled_env_is_set_during_test_session` —
      sanity check: the shield is observable at test time. If the
      conftest stops setting the env var (e.g. accidentally renamed to
      a different knob), this test fails with a clear message rather
      than the whole suite silently re-leaking SDK calls.
  (2) `test_do_board_edit_add_does_not_invoke_judge_under_shield` —
      end-to-end pin: under the shield, exercising
      `_validate_briefing_structure` with a sentinel `dep_judge_fn` that
      RAISES on any call confirms the env-var-gate short-circuits
      before reaching the judge. The validator returns `None` (briefing
      passes) and the sentinel is never invoked.
  (3) `test_local_override_unsets_shield` — per-test override path
      still works (this is what the two intentional-judge-exercising
      modules under `test_dep_validator_judge.py` and
      `test_tb243_validator_judge_surface.py` rely on). After
      `monkeypatch.delenv("AP2_VALIDATOR_JUDGE_DISABLED")` the same
      validator call DOES invoke the judge stub — the override path
      is not broken by the shield.

Together these three pin both directions of the shield: it works by
default, and tests can opt out when they need to.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from ap2 import tools
from ap2.tests._briefing_fixtures import canonical_briefing


_CANONICAL = canonical_briefing("TB-300", title="conftest-shield target")


def _events_file(tmp_path: Path) -> Path:
    """Per-test events file path; same shape as
    `test_dep_validator_judge.py`'s helper."""
    return tmp_path / "events.jsonl"


# (1) Sanity: the conftest's `setdefault` took effect for this session.
def test_validator_judge_disabled_env_is_set_during_test_session():
    """`AP2_VALIDATOR_JUDGE_DISABLED` is truthy when the test runs.

    The top-level `ap2/tests/conftest.py` sets this at import time via
    `os.environ.setdefault`. If the shield drifts (e.g. renamed knob,
    deleted conftest), the whole unit-test surface re-leaks Haiku-4.5
    SDK calls on every `add_*` invocation; this test is the canary.
    """
    val = os.environ.get("AP2_VALIDATOR_JUDGE_DISABLED", "")
    assert val.lower() in {"1", "true", "yes"}, (
        f"AP2_VALIDATOR_JUDGE_DISABLED expected truthy under the "
        f"ap2/tests/conftest.py shield; got {val!r}. Did the conftest "
        "shield get removed or renamed?"
    )


# (2) Under the shield, the judge is never invoked even when
#     `_validate_briefing_structure` is called with a sentinel judge_fn.
def test_do_board_edit_add_does_not_invoke_judge_under_shield(tmp_path):
    """The env-var gate at the top of `_check_dependency_coherence`
    short-circuits before reaching the judge. If the gate breaks, the
    sentinel `_explode` stub raises and the test fails with a clear
    trace.

    Mirrors `test_dep_validator_judge.py::test_dep_judge_disabled_skips_check`
    but from the conftest-shield POV: that test sets the env var
    locally to pin the off-switch; this test relies on the conftest
    already having set it. The two tests together pin the gate from
    both ends.
    """
    def _explode(**_kwargs):
        raise AssertionError(
            "judge MUST NOT be invoked under the ap2/tests/conftest.py "
            "shield; AP2_VALIDATOR_JUDGE_DISABLED should short-circuit "
            "_check_dependency_coherence first"
        )

    err = tools._validate_briefing_structure(
        _CANONICAL,
        description="conftest-shield smoke",
        blocked_csv="",
        events_file=_events_file(tmp_path),
        dep_judge_fn=_explode,
    )
    # Shield → check #7 returns None (clean skip); briefing passes
    # structural validation overall.
    assert err is None
    # No `validator_judge_*` event emitted (the disable path is a
    # clean skip, not a fail-open).
    ev_file = _events_file(tmp_path)
    assert (
        not ev_file.exists()
        or ev_file.read_text() == ""
    ), "shield path must not emit fail-open events"


# (3) Per-test override path still works (used by the two
#     intentional-judge-exercising modules).
def test_local_override_unsets_shield(tmp_path, monkeypatch):
    """After `monkeypatch.delenv("AP2_VALIDATOR_JUDGE_DISABLED")` the
    validator DOES invoke the judge stub. Without this property, the
    two judge-exercising modules
    (`test_dep_validator_judge.py`, `test_tb243_validator_judge_surface.py`)
    would silently lose their coverage when the shield landed.

    Uses a `captured` list (same shape as
    `test_dep_validator_judge.py::_make_judge`) so the assertion is
    "judge stub was called" rather than "didn't fail".
    """
    monkeypatch.delenv("AP2_VALIDATOR_JUDGE_DISABLED", raising=False)
    captured: list[dict] = []

    def _judge(**kwargs):
        captured.append(dict(kwargs))
        return {"hard_predecessors": [], "reasoning": "no deps"}

    err = tools._validate_briefing_structure(
        _CANONICAL,
        description="override-path smoke",
        blocked_csv="",
        events_file=_events_file(tmp_path),
        dep_judge_fn=_judge,
    )
    assert err is None
    assert captured, (
        "judge stub was never called after monkeypatch.delenv — the "
        "per-test override path is broken; intentional-judge modules "
        "would lose their coverage"
    )

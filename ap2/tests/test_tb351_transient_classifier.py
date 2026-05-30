"""TB-351: non-live unit test for the real-SDK smokes' transient-error
classifier.

This pins ``ap2.tests.smoke._transient.is_transient_sdk_error`` WITHOUT
spending a live Claude call — it runs in the normal gate (it is NOT
gated behind ``AP2_REAL_SDK``, and lives outside ``ap2/tests/smoke/`` so
the descoped gate ``pytest ... --ignore=ap2/tests/smoke`` still collects
it). The smokes import the same classifier, so this is the cheap,
deterministic pin on the transient-vs-real mapping that the live smokes
rely on.

Two contract anchors:

  1. The exact note observed in the wild on 2026-05-30 —
     ``judge error: ... returned an error result`` on an ``unverified``
     result — maps to TRANSIENT (the smoke should skip).
  2. A clean ``status='fail'`` (wrong-but-confident) maps to REAL — the
     smoke must still fail on it. The override must never mask a genuine
     regression.
"""
from __future__ import annotations

import pytest

from ap2.tests.smoke._transient import (
    TRANSIENT_SIGNATURES,
    is_transient_sdk_error,
    transient_signature,
)
from ap2.verify import CriterionResult


# --- Anchor 1: the observed transient note maps to transient ---------------

def test_observed_judge_error_note_maps_to_transient():
    """The exact 2026-05-30 shape from
    test_prose_judge_passes_obvious_pass_case."""
    result = CriterionResult(
        bullet="`scripts/run_foo.py` exists ...",
        kind="prose",
        status="unverified",
        notes=(
            "judge error: Exception: Claude Code returned an error "
            "result: success"
        ),
    )
    assert is_transient_sdk_error(result) is True
    # Either observed substring is an acceptable match.
    assert transient_signature(result) in (
        "judge error",
        "returned an error result",
    )


# --- Anchor 2: a clean (even wrong) verdict maps to real -------------------

def test_clean_wrong_fail_verdict_maps_to_real():
    """A confident ``fail`` is a REAL verdict even when the smoke expected
    pass — it must still fail the smoke, never skip."""
    result = CriterionResult(
        bullet="`scripts/run_foo.py` exists with build_grid ...",
        kind="prose",
        status="fail",
        notes="the diff does not add build_grid; README-only change",
    )
    assert is_transient_sdk_error(result) is False
    assert transient_signature(result) is None


def test_clean_pass_verdict_maps_to_real():
    result = CriterionResult(
        bullet="...", kind="prose", status="pass", notes=""
    )
    assert is_transient_sdk_error(result) is False


def test_clean_fail_with_transient_word_in_notes_still_real():
    """Narrowness check: a confident verdict is never transient, even if
    its prose happens to contain a transient-signature word."""
    result = CriterionResult(
        bullet="...",
        kind="prose",
        status="fail",
        notes="the build_grid helper timed out under load — wrong shape",
    )
    assert is_transient_sdk_error(result) is False


# --- Every centralized signature is recognized ----------------------------

@pytest.mark.parametrize("sig", TRANSIENT_SIGNATURES)
def test_each_signature_in_unverified_result_is_transient(sig):
    result = CriterionResult(
        bullet="b",
        kind="prose",
        status="unverified",
        notes=f"judge unreachable -- {sig} -- while contacting the service",
    )
    assert is_transient_sdk_error(result) is True


# --- Validator-smoke shape: event dicts -----------------------------------

def test_validator_fail_event_with_transient_error_is_transient():
    """A `validator_judge_fail` whose SDK error string is a transient
    service blip → skip."""
    ev = {
        "type": "validator_judge_fail",
        "error": (
            "Exception: Claude Code returned an error result: "
            "error_during_execution"
        ),
        "parse_error": "sdk_exception",
    }
    assert is_transient_sdk_error(ev) is True


def test_validator_fail_event_for_arg_rejection_is_real():
    """The TB-249 regression shape: the CLI rejects an unknown option at
    arg-parse and the SDK surfaces a ProcessError string. This must NOT be
    classified transient, or the validator smoke would mask the very
    regression it exists to catch."""
    ev = {
        "type": "validator_judge_fail",
        "error": (
            "ProcessError: Command failed with exit code 1: "
            "error: unknown option '--max-tokens'"
        ),
        "parse_error": "sdk_exception",
    }
    assert is_transient_sdk_error(ev) is False


def test_non_dict_judge_response_event_is_real():
    """A malformed (non-JSON / non-object) judge response is a genuine
    problem, not a transport blip — must not skip."""
    ev = {"type": "validator_judge_fail", "error": "non-dict judge response"}
    assert is_transient_sdk_error(ev) is False


# --- MCP-smoke shape: raised exceptions -----------------------------------

def test_raised_sdk_error_result_exception_is_transient():
    exc = Exception(
        "Claude Code returned an error result: error_during_execution"
    )
    assert is_transient_sdk_error(exc) is True


def test_raised_arg_rejection_exception_is_real():
    exc = Exception(
        "Command failed: error: unknown option '--max-tokens'"
    )
    assert is_transient_sdk_error(exc) is False


# --- Unverified WITHOUT a transient signature is real ----------------------

def test_unverified_without_signature_is_real():
    """The judge returned an unparseable verdict — a genuine 'judge can't
    decide' problem, not a transport blip. Must NOT skip."""
    result = CriterionResult(
        bullet="b",
        kind="prose",
        status="unverified",
        notes="could not parse a verdict from the response",
    )
    assert is_transient_sdk_error(result) is False

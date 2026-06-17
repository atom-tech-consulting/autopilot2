"""TB-261: regression-pin for ``ap2.json_extract.extract_rightmost_json_object``.

Why this module exists
----------------------
Four call sites in the codebase (prose judge, janitor judge, validator-
judge dep-coherence, and the prose-judge parse-error categorizer) all
used to hand-roll JSON-from-LLM-response extraction with the unbalanced
"first ``{`` to last ``}``" boundary-finding pattern. TB-261 centralized
the extraction in ``ap2.json_extract`` using stdlib ``raw_decode`` and
returns the **rightmost top-level** JSON object — so a preamble that
holds literal braces (set notation, code blocks, parameter sweeps) no
longer shadows the verdict at the end of the response.

This module pins the util's six core behaviors so the next
``find("{")`` / ``rfind("}")`` regression is impossible without
tripping a test:

  (a) preamble brace shadowing — the TB-89 captured shape
      (post-train judge response with ``{50/150, 150/50}`` in the
      recommendations section before the final ``{"status": "pass", ...}``
      verdict). The pre-TB-261 parser returned ``parse_error=
      unescaped_in_string`` for this shape; the util correctly recovers
      the verdict.
  (b) multiple shadowing snippets — preamble holds several ``{...}``
      blocks (sets, code, formulas) before the verdict.
  (c) JSON strings containing internal ``{`` / ``}`` chars — string
      escapes don't fool the brace boundary.
  (d) JSON escape sequences (``\\"``, ``\\\\``) inside string values —
      stdlib ``raw_decode`` handles these by construction.
  (e) no JSON object found → returns ``None``.
  (f) multiple top-level JSON objects → returns the **rightmost**.

The TB-89 captured response is the integration check — a contract test
against an actual production failure to anchor the fix to the operator's
incident. Its location is supplied at runtime via the
``AP2_TB89_CAPTURED_RESPONSE`` env var rather than a hard-coded absolute
path (TB-415: a baked ``/Users/<sandbox-user>/repos/...`` path ships in
``ap2.tests`` and leaks the sandbox operator's local checkout root —
``ap2.tests`` is a declared package). When the env var is unset (the
default for any clean checkout) the integration check skips and the
synthetic brace-shadowing cases above cover the same shape. See the
module docstring on ``ap2/json_extract.py`` for the algorithm + rationale.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from ap2.json_extract import extract_rightmost_json_object


# --------------------------------------------------------------------------
# (a) Preamble brace shadowing — the TB-89 trigger
# --------------------------------------------------------------------------


def test_preamble_brace_shadowing_tb89_shape():
    """Judge response holds literal braces in the prose preamble before
    the verdict JSON at the end. Pre-TB-261 the slice-from-first-brace
    extractor captured the preamble's ``{`` and threw ``json.loads``
    at a mostly-prose blob; the util walks rightmost-first and finds
    the actual verdict.
    """
    response = (
        "Looking at the insight file:\n"
        "- Line 43-49: Reading section contains the verdict summary\n"
        "- Lines 51-55: enumerate all three verdicts:\n"
        "  - curriculum_contributing -> follow-up split-ratio sweep "
        "({50/150, 150/50}) before any cloud commit\n"
        "  - curriculum_not_contributing -> queue TB-90's RFT/STaR\n"
        "  - curriculum_broken -> operator decision\n\n"
        '{"status": "pass", "rationale": "all three verdicts enumerated"}'
    )

    out = extract_rightmost_json_object(response)
    assert out is not None
    obj, start, end = out
    assert obj == {
        "status": "pass",
        "rationale": "all three verdicts enumerated",
    }
    # The returned slice should reproduce the verdict, not the preamble.
    assert response[start:end].startswith('{"status"')
    # The verdict sits at the very end — trailing content is empty.
    assert response[end:] == ""


# --------------------------------------------------------------------------
# (b) Multiple shadowing snippets in the preamble
# --------------------------------------------------------------------------


def test_multiple_shadowing_snippets_in_preamble():
    """Preamble holds several non-JSON ``{...}`` blocks before the
    verdict. Each shadowing snippet is itself a `{`-led substring that
    isn't valid JSON; the util skips past all of them and lands on the
    rightmost block that actually parses.
    """
    response = (
        "Analysis:\n"
        "- Parameter sweep covers {batch=8, lr=1e-4} and {batch=16, lr=5e-5}.\n"
        "- The set notation {A, B, C} enumerates three options.\n"
        "- Code reads `obj = {key: value for key in xs}`.\n"
        '{"verdict": "pass", "reasoning": "all checks ok"}'
    )

    out = extract_rightmost_json_object(response)
    assert out is not None
    obj, _, _ = out
    assert obj == {"verdict": "pass", "reasoning": "all checks ok"}


# --------------------------------------------------------------------------
# (c) JSON strings containing internal `{` / `}` chars
# --------------------------------------------------------------------------


def test_json_string_with_internal_braces():
    """A JSON string value containing ``{`` and ``}`` chars is correctly
    bounded by stdlib ``raw_decode``'s string-state tracking — the util
    inherits that for free."""
    response = (
        'prose preamble {set notation} '
        '{"reasoning": "the code matches the pattern {x, y, z}", '
        '"status": "pass"}'
    )

    out = extract_rightmost_json_object(response)
    assert out is not None
    obj, _, _ = out
    assert obj["status"] == "pass"
    assert obj["reasoning"] == "the code matches the pattern {x, y, z}"


# --------------------------------------------------------------------------
# (d) JSON escape sequences (`\"`, `\\`) inside string values
# --------------------------------------------------------------------------


def test_json_escape_sequences_in_string_values():
    """Backslash-escaped quotes and backslashes inside the verdict's
    string values parse correctly. A hand-rolled brace counter would
    have to re-implement string-escape state tracking to handle this;
    stdlib ``raw_decode`` does it natively."""
    # Build the raw JSON literal explicitly so the Python string-escape
    # layer doesn't obscure what the LLM-response bytes actually are.
    json_blob = (
        '{"status": "pass", '
        '"rationale": "file has \\"quoted\\" word and \\\\ backslash"}'
    )
    response = "Prose with {set, notation} preamble.\n" + json_blob

    out = extract_rightmost_json_object(response)
    assert out is not None
    obj, _, _ = out
    assert obj["status"] == "pass"
    assert obj["rationale"] == 'file has "quoted" word and \\ backslash'


# --------------------------------------------------------------------------
# (e) No JSON object found → returns None
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "label, response",
    [
        ("empty", ""),
        ("prose only", "I cannot evaluate this bullet."),
        ("only opening brace", "Here is the start { but no end"),
        ("malformed json mid-string", '{"status": "pass", "rationale": "trunc'),
        ("top-level list, no object", "[1, 2, 3, 4]"),
        ("top-level scalar", "42"),
    ],
)
def test_no_json_object_returns_none(label, response):
    """Truly-unparseable / non-object-shaped responses fall through to
    ``None`` so each call site's existing "no JSON object" error path
    fires unchanged. This is the backward-compat guarantee — pre-
    TB-261 these all hit the call sites' fall-back too."""
    assert extract_rightmost_json_object(response) is None


# --------------------------------------------------------------------------
# (f) Multiple top-level JSON objects → returns rightmost
# --------------------------------------------------------------------------


def test_multiple_top_level_objects_returns_rightmost():
    """When the response contains multiple parseable top-level JSON
    objects, the util returns the LAST one — matching the judge /
    janitor / validator-judge prompt contract that the verdict sits
    at the end of the response."""
    response = (
        '{"earlier": "object", "status": "this is not the verdict"}\n'
        "Reasoning prose in the middle.\n"
        '{"status": "pass", "rationale": "final verdict"}'
    )

    out = extract_rightmost_json_object(response)
    assert out is not None
    obj, _, _ = out
    assert obj == {"status": "pass", "rationale": "final verdict"}


def test_multiple_objects_with_trailing_prose_returns_rightmost():
    """Trailing prose after the rightmost object doesn't shift the
    selection. ``end_offset`` still bounds the verdict's JSON, so each
    call site can detect the trailing prose via ``response[end:]``."""
    response = (
        '{"earlier": "object"}\n'
        '{"status": "pass", "rationale": "ok"}\n'
        "Hope this helps!"
    )

    out = extract_rightmost_json_object(response)
    assert out is not None
    obj, _, end = out
    assert obj == {"status": "pass", "rationale": "ok"}
    assert response[end:].strip() == "Hope this helps!"


# --------------------------------------------------------------------------
# Half-open `end` offset contract
# --------------------------------------------------------------------------


def test_end_offset_is_half_open():
    """``response[start:end]`` reproduces the parsed substring; ``end``
    points one past the closing ``}``. Documenting the convention so
    each call site can substitute ``response[end:]`` for the pre-TB-261
    ``response[end + 1:]`` trailing-prose check cleanly."""
    response = (
        'preamble {x, y} '
        '{"status": "pass"}\n'
        "trailing"
    )

    out = extract_rightmost_json_object(response)
    assert out is not None
    obj, start, end = out
    assert obj == {"status": "pass"}
    assert response[start:end] == '{"status": "pass"}'
    # End points right past the closing brace, so end is the index of
    # the newline character.
    assert response[end] == "\n"


# --------------------------------------------------------------------------
# Integration check: real TB-89 captured response on disk
# --------------------------------------------------------------------------


# TB-415: sandbox-neutral source for the captured-response path. The file
# lives in a downstream operator's debug dir, which is environment-specific;
# point at it via `AP2_TB89_CAPTURED_RESPONSE=<path>` rather than baking the
# sandbox operator's local `/Users/<sandbox-user>/repos/...` checkout root
# into a shipped test module. Unset (the clean-checkout default) → `None` →
# the integration check skips and the synthetic cases above carry coverage.
_TB89_CAPTURED_RESPONSE_ENV = os.environ.get("AP2_TB89_CAPTURED_RESPONSE", "").strip()
_TB89_CAPTURED_RESPONSE = (
    Path(_TB89_CAPTURED_RESPONSE_ENV) if _TB89_CAPTURED_RESPONSE_ENV else None
)


@pytest.mark.skipif(
    _TB89_CAPTURED_RESPONSE is None or not _TB89_CAPTURED_RESPONSE.exists(),
    reason=(
        "TB-89 captured response file not provided. Set "
        "AP2_TB89_CAPTURED_RESPONSE=<path> to run this integration check "
        "against the literal incident response; the unit cases above cover "
        "the same brace-shadowing shape synthetically."
    ),
)
def test_tb89_captured_response_parses_as_pass():
    """Integration check against the literal LLM response that triggered
    the original incident. The verdict is ``status=pass``; pre-TB-261
    the parser returned ``parse_error=unescaped_in_string`` and
    ``status=unverified``."""
    raw = _TB89_CAPTURED_RESPONSE.read_text()

    out = extract_rightmost_json_object(raw)
    assert out is not None
    obj, _, _ = out
    assert obj.get("status") == "pass"
    assert "grpo-curriculum" in obj.get("rationale", "")

"""Centralized LLM-response JSON extraction via stdlib ``raw_decode`` (TB-261).

Why this module exists
----------------------
LLM agents (prose judge, janitor judge, validator-judge dep-coherence) all
return responses where the final JSON verdict may be preceded by prose
preamble. Pre-TB-261, four call sites in this codebase reached for the
same hand-rolled extraction pattern to recover the verdict — slicing
from the first ``{`` index (via ``str.find``) to the last ``}`` index
(via ``str.rfind``) and feeding the slice to ``json.loads``.

The bug: that approach is **unbalanced** — it captures everything between
the FIRST ``{`` in the response and the LAST ``}``. When the preamble holds literal
braces (set notation ``{50/150, 150/50}``, code-block examples, parameter
sweeps), the slice contains free-form prose and ``json.loads`` raises a
``JSONDecodeError``. The judge's actual verdict at the END of the response
is hidden — the parser only sees the prose preamble. Concrete cost:
post-train TB-89 burned three full ``unfreeze → retry`` cycles plus a
manual operator unfreeze on exactly this shape (judge response contained
``{50/150, 150/50}`` in the recommendations section before the
``{"status": "pass", ...}`` verdict). The repro lived in a downstream
project's ``.cc-autopilot/bug-reports/`` notes for this parser shape.

The fix is to extract the **rightmost top-level JSON object**, not "first
``{`` to last ``}``". The judge / janitor / validator-judge prompts all
require the verdict to sit at the end of the response (per the TB-236
``trailing_prose_after_json`` distinction — trailing prose is tolerated
post-verdict, but the verdict itself comes last). So scanning candidate
``{`` positions from right to left, and using the stdlib JSON parser to
attempt a parse from each, finds the verdict regardless of how many
braces the preamble holds.

Why ``raw_decode`` over a hand-rolled brace-depth scanner
---------------------------------------------------------
``json.JSONDecoder().raw_decode(text, idx)`` is the stdlib's own JSON
parser, exposed as a function that parses a JSON value starting at
``idx`` and returns ``(obj, end)`` where ``end`` is the offset
immediately after the consumed value. All the string-escape subtleties
that a hand-rolled brace counter would need to re-implement —
double-quoted strings containing ``{``/``}``, ``\\"`` and ``\\\\``
escapes, Unicode escapes — are handled by the same code path that
``json.loads`` uses, so by construction they're correct.

A hand-rolled brace-depth scanner (the shape the original bug report
proposed) would re-derive string-state tracking from scratch, which is
exactly the kind of subtle parser code that breeds the next bug. Stdlib
is strictly better fit: zero deps, idiomatic Python, battle-tested.

Algorithm
---------
Walk every ``{`` position in ``text`` from rightmost to leftmost,
attempting ``raw_decode(text, pos)`` at each. First success wins — it's
the rightmost top-level JSON object by construction, because any object
that contains it would have a ``{`` at an earlier position which we'd
visit later in the rightmost-first walk. O(n × k) where k is the number
of ``{`` chars in ``text``; k is typically 1-5 in LLM responses so this
is effectively O(n).

Non-dict top-level values
-------------------------
Only top-level JSON **objects** (``{...}``) are returned. The util's
contract is "find a dict-shaped verdict"; lists, scalars, and strings
at the top level are intentionally ignored. The callers all expect a
dict and have their own taxonomy for "valid JSON, wrong shape" (e.g.
``ap2/tools.py``'s ``non_dict`` category) — so returning ``None`` on a
non-dict candidate keeps the boundary clean. In practice the algorithm
walks ``{`` positions only, so a top-level list ``[1, 2, 3]`` returns
``None`` without ever calling ``raw_decode`` (no ``{`` to walk from).

Fallback semantics
------------------
If no candidate position yields a parseable JSON object, the util
returns ``None``. Each call site translates ``None`` back to its
existing "no JSON object" error path so the observable behavior on
truly-malformed responses is unchanged — only the set of responses
that parse correctly widens.
"""
from __future__ import annotations

import json


# Module-level decoder instance — ``json.JSONDecoder()`` is cheap to
# construct but every call site re-using one instance keeps the per-
# extraction overhead at "one ``raw_decode`` call, no construction".
_DECODER = json.JSONDecoder()


def extract_rightmost_json_object(
    text: str,
) -> tuple[dict, int, int] | None:
    """Return ``(obj, start, end)`` for the rightmost top-level JSON object
    in ``text``, or ``None`` if no parseable JSON object is found.

    ``start`` is the index of the opening ``{`` in ``text``; ``end`` is
    the offset immediately after the closing ``}`` (matching
    ``json.JSONDecoder.raw_decode``'s half-open convention).
    ``text[start:end]`` reproduces the parsed JSON substring;
    ``text[end:]`` is whatever trailing content followed the verdict.

    Only top-level JSON **objects** are recognized; lists, scalars, and
    strings at the top level return ``None``. The judge / janitor /
    validator-judge prompts in this codebase all pin a dict verdict, and
    each call site has its own "valid JSON, wrong shape" category for
    non-dict responses — so the util keeps the boundary clean by only
    matching object-shaped values.

    Algorithm: enumerate every ``{`` position in ``text`` from rightmost
    to leftmost; try ``raw_decode(text, pos)`` at each; first
    object-typed success wins (it's the rightmost top-level object by
    construction). See the module docstring for the rationale.
    """
    if not text:
        return None
    # Collect every `{` index from rightmost to leftmost. Using
    # `str.rfind` with a shrinking upper bound keeps the scan linear in
    # the response length, vs. scanning the whole string for each
    # position. The list is bounded by the number of literal `{` chars
    # in the response, typically 1-5 for LLM verdicts.
    candidates: list[int] = []
    i = text.rfind("{")
    while i != -1:
        candidates.append(i)
        i = text.rfind("{", 0, i)
    for pos in candidates:
        try:
            obj, end = _DECODER.raw_decode(text, pos)
        except json.JSONDecodeError:
            # Not a valid JSON value at this `{` (or starts a value but
            # truncates mid-string / mid-key). Keep walking leftward —
            # an earlier candidate may still parse cleanly.
            continue
        if isinstance(obj, dict):
            return obj, pos, end
        # `raw_decode` from a `{` position will always return a dict in
        # strict JSON (since `{` only opens objects). This branch is
        # defensive against future JSON-superset variants and stays
        # consistent with the "only top-level objects" contract.
    return None

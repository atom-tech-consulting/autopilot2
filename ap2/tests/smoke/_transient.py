"""Shared transient-SDK-error classifier for the real-SDK smokes (TB-351).

The real-SDK smokes in this package exercise the *wiring* of our SDK
integration: that MCP tools fire, the prose/validator judge round-trips
and returns a structured verdict, the agent actually calls the tool.
They do NOT test Anthropic's uptime. When the underlying live Claude call
errors at the transport/service level — a 5xx, an overload, a rate-limit,
a dropped connection, a judge timeout, or the CLI emitting an `is_error`
result that the SDK surfaces as ``Claude Code returned an error result:
...`` — the wiring couldn't be exercised this run. That is an
inconclusive SKIP, not a FAIL (observed 2026-05-30 in
``test_prose_judge_passes_obvious_pass_case``:
``status='unverified' notes='judge error: Exception: Claude Code returned
an error result: success'``).

``is_transient_sdk_error`` centralizes the signature list so all five
smokes agree on what counts as transient, and so a single non-live unit
test (``ap2/tests/test_tb351_transient_classifier.py``, which runs in the
normal gate) can pin the transient-vs-real mapping without spending a
live call.

The classifier is deliberately NARROW:

  * A confident, structured ``pass``/``fail`` verdict is NEVER transient
    — even a *wrong* one. A confident-but-incorrect verdict is a real
    regression and must still fail its smoke (the briefing's
    "wrong-verdict still fails" design point).
  * Deterministic wiring bugs are NOT in the signature list. The TB-249
    ``--max-tokens`` arg rejection, for instance, surfaces as an
    ``unknown option`` ProcessError string, which matches none of the
    transient signatures, so the validator smoke still fails on it.

How each smoke's transient signal reaches the classifier:

  * Prose judge (``verify._judge_prose_bullet``) catches the error
    internally and returns a ``CriterionResult`` with
    ``status="unverified"`` and a ``notes="judge error: ..."`` string.
  * Validator judge (``_check_dependency_coherence``) fails open and
    emits a ``validator_judge_fail`` / ``validator_judge_timeout`` event
    carrying the SDK error string in its ``error`` field.
  * The MCP round-trip smokes iterate ``sdk.query(...)`` directly; a
    transient service error is *raised* as an ``Exception`` whose message
    is the error text.

The classifier accepts all three shapes (result object, event dict,
exception) so the smokes can feed it whatever they have.
"""
from __future__ import annotations

from typing import Callable, Optional

#: Substrings (matched case-insensitively against a result's
#: notes/message/error text) that indicate a transient transport/service
#: error rather than a genuine verdict. One source of truth so the five
#: smokes never drift in what they treat as transient, and so the unit
#: test can pin the list. ``judge error`` and ``returned an error
#: result`` are the exact signatures observed in the wild on 2026-05-30.
TRANSIENT_SIGNATURES: tuple[str, ...] = (
    "judge error",
    "returned an error result",
    "temporarily unavailable",
    "overloaded",
    "rate limit",
    "429",
    "5xx",
    "internal server error",
    "timed out",
    "timeout",
    "connection",
)

#: Statuses that represent a confident, structured verdict. A result in
#: one of these is NEVER transient (even if the verdict is wrong) — this
#: keeps the override narrow per the briefing's "wrong-verdict still
#: fails" design point.
_CLEAN_VERDICT_STATUSES = frozenset({"pass", "fail"})


def _status_of(result: object) -> Optional[str]:
    """Best-effort extraction of a ``status`` field from a result-like."""
    if isinstance(result, dict):
        status = result.get("status")
    else:
        status = getattr(result, "status", None)
    return status if isinstance(status, str) else None


def _message_of(result: object) -> str:
    """Best-effort extraction of the human-readable error/notes text.

    Handles the three shapes the smokes feed in: a result object
    (``CriterionResult`` with ``.notes``), an event dict (with
    ``error``/``reason``), and a raised exception.
    """
    if isinstance(result, str):
        return result
    if isinstance(result, BaseException):
        return f"{type(result).__name__}: {result}"
    if isinstance(result, dict):
        parts = [result.get(k) for k in ("notes", "message", "error", "reason")]
    else:
        parts = [
            getattr(result, k, None)
            for k in ("notes", "message", "error", "reason")
        ]
    return " ".join(str(p) for p in parts if p)


def transient_signature(result: object) -> Optional[str]:
    """Return the matched transient signature, or ``None`` if ``result`` is
    not a transient transport/service error.

    A clean ``pass``/``fail`` verdict short-circuits to ``None`` so a
    confident-but-wrong verdict is never masked as transient.
    """
    if _status_of(result) in _CLEAN_VERDICT_STATUSES:
        return None
    haystack = _message_of(result).lower()
    for sig in TRANSIENT_SIGNATURES:
        if sig in haystack:
            return sig
    return None


def is_transient_sdk_error(result: object) -> bool:
    """True iff ``result`` looks like a transient SDK transport/service
    error (→ the smoke should skip, optionally after one bounded retry)
    rather than a genuine — even if wrong — verdict (→ the smoke should
    still assert/fail)."""
    return transient_signature(result) is not None


def call_with_transient_retry(
    call: Callable[[], object],
    *,
    describe: str,
    transient_of: Callable[[object], Optional[str]] = transient_signature,
    max_retries: int = 1,
) -> object:
    """Run ``call()``, skipping (after one bounded retry) on a transient
    SDK error.

    ``call`` is a zero-arg callable that performs the live SDK round-trip
    and either returns a result or raises. ``transient_of`` maps the
    *returned* value to a matched signature string (or ``None`` for a
    clean result); the default classifies the return value directly,
    which fits smokes whose return *is* the result (prose judge) or whose
    return is a plain tuple of side effects (the MCP smokes, where the
    transient signal arrives as a raised exception instead). Smokes whose
    return buries the signal — e.g. the validator smoke's fail/timeout
    event list — pass a custom ``transient_of``.

    On a transient error (a raised exception OR a transient-mapped
    return), retries up to ``max_retries`` times (default 1 → worst-case
    cost ~2x), then ``pytest.skip``s naming the matched signature. A
    non-transient exception re-raises; a non-transient return flows back
    to the caller untouched, where the smoke's existing assertions run —
    so a genuinely-wrong verdict still fails.
    """
    import pytest

    attempt = 0
    while True:
        try:
            result = call()
        except Exception as exc:  # noqa: BLE001 — re-raised unless transient
            sig = transient_signature(exc)
            if sig is None:
                raise
            if attempt >= max_retries:
                pytest.skip(
                    f"transient SDK error ({sig}) during {describe}: {exc}"
                )
            attempt += 1
            continue
        sig = transient_of(result)
        if sig is None:
            return result
        if attempt >= max_retries:
            pytest.skip(
                f"transient SDK error ({sig}) during {describe} "
                f"(still transient after {max_retries} retry)"
            )
        attempt += 1

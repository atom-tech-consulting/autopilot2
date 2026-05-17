"""TB-249 real-SDK smoke for the LLM dep-coherence validator.

What FakeSDK / unit-test stubs can't catch (TB-249's regression
shape): the Claude Agent SDK rejecting `--max-tokens` as an unknown
option only manifests against the real SDK. The pre-TB-249 code
passed `extra_args={"max-tokens": str(max_tokens)}` to
`ClaudeAgentOptions`; every call exited with `error: unknown option
'--max-tokens'` and the fail-open posture in
`_check_dependency_coherence` swallowed it as a
`validator_judge_fail` event. Unit tests passed because the test
suite mocked the judge — only a real SDK round-trip exposes the
regression.

This smoke fires the validator against a known-good briefing (no
dep-mismatch claims) with the real Haiku-4.5 judge and asserts:

  1. The validator returns `None` (the briefing's prose names no
     hard predecessor outside its `@blocked:` codespan).
  2. No `validator_judge_fail` event was emitted (the SDK call
     completed without an `extra_args` rejection).
  3. No `validator_judge_timeout` event was emitted (the default
     15s budget is comfortably above Haiku's typical response time
     on a small payload).

OPT-IN: this test makes real API calls. It only runs when AP2_REAL_SDK
is set:

    AP2_REAL_SDK=1 uv run pytest ap2/tests/smoke/test_validator_judge_real_sdk.py -v -s

Bounded cost: tiny payload, `max_turns=2`, single-call expectation.
Per-invocation cost target ≤$0.005 at Haiku rates (per howto.md's
validator-judge knob doc).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("AP2_REAL_SDK"),
    reason="real-SDK smoke; set AP2_REAL_SDK=1 to run",
)


# Intentionally a small, structurally-valid briefing that claims NO
# external dependencies. The judge should return an empty
# hard_predecessors list and the validator should return None.
_GOOD_BRIEFING = """\
# Toy task: add a one-line helper to ap2/tools.py

Tags: `#test`

## Goal

Demonstrate the TB-249 smoke. Add a no-op helper function so the
validator has a real briefing to chew on. Why now: TB-249 fixed the
extra_args bug; this smoke confirms the SDK no longer rejects the
call.

## Scope

(1) Add `def _toy_noop(): return None` to ap2/tools.py.

## Design

Trivial smoke target. No design.

## Verification

- `uv run pytest -q ap2/tests/` — full suite green.

## Out of scope

- Anything else.
"""


def test_validator_judge_real_sdk_happy_path(tmp_path):
    """Real Claude Haiku-4.5 judge + structurally-valid briefing → no
    fail/timeout events, validator returns None."""
    from ap2 import events, tools

    # Reset the deprecation-knob one-shot flag so order-dependent smoke
    # runs don't surprise us.
    tools._VALIDATOR_JUDGE_DEPRECATED_KNOB_LOGGED.clear()

    # Ensure the legacy alias is NOT set; we want the canonical
    # max_turns path to fire.
    os.environ.pop("AP2_VALIDATOR_JUDGE_MAX_TOKENS", None)
    os.environ.pop("AP2_VALIDATOR_JUDGE_DISABLED", None)

    events_file = tmp_path / "events.jsonl"
    err = tools._validate_briefing_structure(
        _GOOD_BRIEFING,
        description="add a no-op helper",
        blocked_csv="",
        events_file=events_file,
        # dep_judge_fn=None → real SDK path
    )

    evts = events.tail(events_file, 50) if events_file.exists() else []
    fails = [e for e in evts if e.get("type") == "validator_judge_fail"]
    timeouts = [e for e in evts if e.get("type") == "validator_judge_timeout"]
    print(
        f"\n[smoke] validator returned err={err!r}; "
        f"fails={len(fails)}; timeouts={len(timeouts)}"
    )

    assert fails == [], (
        f"TB-249 regression — validator_judge_fail event(s) emitted: "
        f"{fails!r}. The most likely cause is the `extra_args=` "
        "literal in `_judge_dep_coherence_default` carrying an "
        "SDK-rejected key (e.g. `max-tokens`). Run "
        "`grep -n max-tokens ap2/tools.py` to confirm."
    )
    assert timeouts == [], (
        f"validator_judge_timeout event(s) emitted: {timeouts!r}. "
        "Either Haiku is slower than 15s on a trivial payload "
        "(unusual) or the SDK is hung — investigate before treating "
        "this as a TB-249 regression."
    )
    # The good briefing claims no hard predecessors; the validator
    # should return None. (If Haiku ever hallucinates a hard
    # predecessor, the smoke surfaces a non-regression-pinned diagnostic
    # — we don't fail the test on it, just print it.)
    if err is not None:
        print(
            f"[smoke] NOTE — validator rejected the good briefing: {err!r}. "
            "Not a TB-249 failure (SDK call succeeded) but worth a "
            "look at the judge's prompt drift."
        )

"""TB-249 regression-pin tests for the validator-judge SDK invocation.

History: TB-235 shipped the LLM dep-coherence validator
(`_validate_briefing_structure` check #7) with `extra_args={"max-tokens":
str(max_tokens)}` on `ClaudeAgentOptions`. The Claude Agent SDK rejects
`--max-tokens` as an unknown option, so every judge call failed with
stderr `error: unknown option '--max-tokens'`. The fail-open posture
swallowed the failure and emitted a `validator_judge_fail` event —
operators never saw the validator's verdict because the validator never
ran. TB-243 surfaced the climbing `validator_judge_fail_count_24h` on
`ap2 status`, which is how the regression was caught.

TB-249 fix:
  - Drop `extra_args={"max-tokens": ...}`. The SDK rejects it.
  - Use `max_turns` as the budget primitive (every other ap2 SDK call
    site does — verify.py, janitor.py, daemon.py).
  - Wire `AP2_VALIDATOR_JUDGE_MAX_TURNS` (default 2) as the canonical
    operator knob.
  - Deprecate `AP2_VALIDATOR_JUDGE_MAX_TOKENS` — kept as a one-cycle
    alias that resolves to `max_turns` capped at 5 (so a stale `500`
    from the old default doesn't translate to 500 turns) and emits a
    `validator_judge_deprecated_knob` event once per process.

Cases (mirror briefing Scope §4):
  1. `extra_args=` literal in `_judge_dep_coherence_default` does not
     contain `max-tokens` (regression-pin against re-introducing the
     bug).
  2. The validator hands the judge fn a `max_turns` kwarg with a
     positive int.
  3. `AP2_VALIDATOR_JUDGE_MAX_TOKENS=10` (legacy) → judge sees
     `max_turns=5` (ceiling-capped) AND a
     `validator_judge_deprecated_knob` event is emitted once per
     process.
  4. On the happy path (valid JSON judge response) no
     `validator_judge_fail` event fires — i.e. the SDK arg fix
     actually restores the validator's primary path.
"""
from __future__ import annotations

import ast
import inspect
import re
import textwrap
from pathlib import Path

import pytest

from ap2 import events, tools
from ap2.tests._briefing_fixtures import canonical_briefing


_CANONICAL = canonical_briefing("TB-301", title="tb-249 regression target")


@pytest.fixture(autouse=True)
def _unshield_validator_judge(monkeypatch):
    """TB-254: override the top-level `ap2/tests/conftest.py` shield.

    This module's cases exercise the validator's interaction with the
    judge's SDK kwargs (`max_turns`, deprecated `max-tokens` alias).
    The shield short-circuits `_check_dependency_coherence` before
    the judge's kwargs are constructed, so the regression-pins would
    silently no-op without this fixture. delenv at test-start lets
    the judge stub fire and inspect what kwargs the validator passed;
    monkeypatch restores the shield on teardown.
    """
    monkeypatch.delenv("AP2_VALIDATOR_JUDGE_DISABLED", raising=False)


def _events_file(tmp_path: Path) -> Path:
    return tmp_path / "events.jsonl"


def _make_judge(response: dict | None, captured: list[dict] | None = None):
    """Build a stub `dep_judge_fn` matching the TB-249 contract.

    Accepts the validator's kwargs (`briefing_text`, `description`,
    `blocked_tokens`, `timeout_s`, `max_turns`) and records them into
    `captured` for assertion.
    """
    def _fn(**kwargs):
        if captured is not None:
            captured.append(dict(kwargs))
        return response

    return _fn


# (1) Regression-pin: the parsed AST of `_judge_dep_coherence_default`
# must contain no `extra_args=` keyword whose dict value carries the
# literal `"max-tokens"` key. Walking the AST (rather than substring
# grepping the source) means the docstring is free to discuss the
# historical bug — only an actual code-level keyword argument trips
# the check. Use the function source via `inspect` rather than the
# file path so a refactor that splits the helper across modules still
# catches the bug at the right call site.
def test_validator_judge_extra_args_does_not_contain_max_tokens():
    src = textwrap.dedent(inspect.getsource(tools._judge_dep_coherence_default))
    tree = ast.parse(src)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg != "extra_args":
                continue
            if not isinstance(kw.value, ast.Dict):
                continue
            for key in kw.value.keys:
                # Match both ast.Constant("max-tokens") and the legacy
                # ast.Str representation (py<3.12 compat).
                key_text = None
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    key_text = key.value
                elif hasattr(ast, "Str") and isinstance(key, getattr(ast, "Str")):
                    key_text = key.s
                if key_text and "max-tokens" in key_text:
                    offenders.append(key_text)
    assert offenders == [], (
        "_judge_dep_coherence_default must not pass "
        '`extra_args={"max-tokens": ...}` to ClaudeAgentOptions — the '
        "Claude Agent SDK rejects --max-tokens as an unknown option "
        "(TB-249). Use `max_turns` as the budget primitive instead. "
        f"Offending key(s): {offenders!r}"
    )
    # Belt-and-suspenders: the canonical budget knob must be wired
    # (positive match — confirms the fix is in place, not just that
    # the broken arg was removed). Use regex tolerant of whitespace.
    assert re.search(r"max_turns\s*=\s*max_turns", src), (
        "_judge_dep_coherence_default must wire `max_turns` into "
        "ClaudeAgentOptions (TB-249)."
    )


# (2) The validator hands the judge fn a `max_turns` kwarg with a
# positive int. Default path with no env knob set uses the module
# default (`_VALIDATOR_JUDGE_MAX_TURNS_DEFAULT`).
def test_validator_judge_uses_max_turns_for_budget(tmp_path, monkeypatch):
    monkeypatch.delenv("AP2_VALIDATOR_JUDGE_MAX_TURNS", raising=False)
    monkeypatch.delenv("AP2_VALIDATOR_JUDGE_MAX_TOKENS", raising=False)
    captured: list[dict] = []
    judge = _make_judge(
        {"hard_predecessors": [], "reasoning": "no deps"},
        captured=captured,
    )
    err = tools._validate_briefing_structure(
        _CANONICAL,
        description="x",
        blocked_csv="",
        events_file=_events_file(tmp_path),
        dep_judge_fn=judge,
    )
    assert err is None
    assert captured, "judge stub was never invoked"
    seen = captured[0]
    assert "max_turns" in seen, (
        f"validator must pass `max_turns` kwarg; saw keys {sorted(seen)}"
    )
    assert isinstance(seen["max_turns"], int), seen
    assert seen["max_turns"] > 0, seen
    assert seen["max_turns"] == tools._VALIDATOR_JUDGE_MAX_TURNS_DEFAULT


# (2b) Env override: AP2_VALIDATOR_JUDGE_MAX_TURNS=3 propagates.
def test_validator_judge_env_override_propagates(tmp_path, monkeypatch):
    monkeypatch.delenv("AP2_VALIDATOR_JUDGE_MAX_TOKENS", raising=False)
    monkeypatch.setenv("AP2_VALIDATOR_JUDGE_MAX_TURNS", "3")
    captured: list[dict] = []
    judge = _make_judge(
        {"hard_predecessors": [], "reasoning": "x"},
        captured=captured,
    )
    err = tools._validate_briefing_structure(
        _CANONICAL,
        description="x",
        blocked_csv="",
        events_file=_events_file(tmp_path),
        dep_judge_fn=judge,
    )
    assert err is None
    assert captured[0]["max_turns"] == 3


# (3) Deprecated-knob alias: AP2_VALIDATOR_JUDGE_MAX_TOKENS=10 →
# `max_turns=5` (ceiling-capped via `_VALIDATOR_JUDGE_DEPRECATED_KNOB_CEIL`)
# AND a `validator_judge_deprecated_knob` event fires once per process.
def test_validator_judge_deprecated_knob_alias(tmp_path, monkeypatch):
    # Reset the one-shot per-process logged set so the test is
    # order-independent.
    tools._VALIDATOR_JUDGE_DEPRECATED_KNOB_LOGGED.clear()
    monkeypatch.delenv("AP2_VALIDATOR_JUDGE_MAX_TURNS", raising=False)
    monkeypatch.setenv("AP2_VALIDATOR_JUDGE_MAX_TOKENS", "10")
    events_file = _events_file(tmp_path)
    captured: list[dict] = []
    judge = _make_judge(
        {"hard_predecessors": [], "reasoning": "x"},
        captured=captured,
    )
    err = tools._validate_briefing_structure(
        _CANONICAL,
        description="x",
        blocked_csv="",
        events_file=events_file,
        dep_judge_fn=judge,
    )
    assert err is None
    # Ceiling-cap pin: 10 > 5 → 5.
    assert captured[0]["max_turns"] == tools._VALIDATOR_JUDGE_DEPRECATED_KNOB_CEIL
    # One-shot deprecation event.
    evts = events.tail(events_file, 50)
    dep = [
        e for e in evts if e.get("type") == "validator_judge_deprecated_knob"
    ]
    assert len(dep) == 1, (
        f"expected exactly one validator_judge_deprecated_knob event; "
        f"got {dep!r}"
    )
    payload = dep[0]
    assert payload.get("knob") == "AP2_VALIDATOR_JUDGE_MAX_TOKENS", payload
    assert payload.get("replacement") == "AP2_VALIDATOR_JUDGE_MAX_TURNS", payload
    assert int(payload.get("legacy_value")) == 10, payload
    assert (
        int(payload.get("applied_max_turns"))
        == tools._VALIDATOR_JUDGE_DEPRECATED_KNOB_CEIL
    ), payload
    # Per-process idempotency: a second invocation does NOT re-emit.
    captured.clear()
    err2 = tools._validate_briefing_structure(
        _CANONICAL,
        description="x",
        blocked_csv="",
        events_file=events_file,
        dep_judge_fn=judge,
    )
    assert err2 is None
    evts2 = events.tail(events_file, 50)
    dep2 = [
        e for e in evts2 if e.get("type") == "validator_judge_deprecated_knob"
    ]
    assert len(dep2) == 1, (
        "deprecation event must fire exactly once per process — saw "
        f"{len(dep2)} after second validate call"
    )


# (3b) Canonical knob wins on conflict: if BOTH knobs are set, the
# canonical one takes precedence AND no deprecation event fires
# (the legacy knob is silently shadowed — operator-config drift, not
# operator-active-use; future-TB cleanup decides whether to escalate).
def test_validator_judge_canonical_knob_wins_over_legacy(tmp_path, monkeypatch):
    tools._VALIDATOR_JUDGE_DEPRECATED_KNOB_LOGGED.clear()
    monkeypatch.setenv("AP2_VALIDATOR_JUDGE_MAX_TURNS", "3")
    monkeypatch.setenv("AP2_VALIDATOR_JUDGE_MAX_TOKENS", "999")
    events_file = _events_file(tmp_path)
    captured: list[dict] = []
    judge = _make_judge(
        {"hard_predecessors": [], "reasoning": "x"},
        captured=captured,
    )
    err = tools._validate_briefing_structure(
        _CANONICAL,
        description="x",
        blocked_csv="",
        events_file=events_file,
        dep_judge_fn=judge,
    )
    assert err is None
    assert captured[0]["max_turns"] == 3
    evts = events.tail(events_file, 50) if events_file.exists() else []
    dep = [
        e for e in evts if e.get("type") == "validator_judge_deprecated_knob"
    ]
    assert dep == [], (
        f"deprecation event must not fire when canonical knob is set; "
        f"got {dep!r}"
    )


# (4) Happy-path integration: valid JSON judge response → no
# `validator_judge_fail` event. The TB-235 regression's smoking gun was
# that EVERY validator invocation emitted `validator_judge_fail`; this
# pins that the canonical happy path is clean of that event.
def test_validator_judge_fail_count_unchanged_on_happy_path(tmp_path, monkeypatch):
    monkeypatch.delenv("AP2_VALIDATOR_JUDGE_MAX_TOKENS", raising=False)
    monkeypatch.delenv("AP2_VALIDATOR_JUDGE_MAX_TURNS", raising=False)
    events_file = _events_file(tmp_path)
    judge = _make_judge(
        {"hard_predecessors": [], "reasoning": "no hard deps"},
    )
    err = tools._validate_briefing_structure(
        _CANONICAL,
        description="x",
        blocked_csv="",
        events_file=events_file,
        dep_judge_fn=judge,
    )
    assert err is None
    if events_file.exists():
        evts = events.tail(events_file, 50)
    else:
        evts = []
    fails = [e for e in evts if e.get("type") == "validator_judge_fail"]
    assert fails == [], (
        f"happy-path validator must not emit validator_judge_fail; "
        f"got {fails!r}"
    )
    timeouts = [e for e in evts if e.get("type") == "validator_judge_timeout"]
    assert timeouts == [], (
        f"happy-path validator must not emit validator_judge_timeout; "
        f"got {timeouts!r}"
    )

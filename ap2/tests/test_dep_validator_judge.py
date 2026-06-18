"""Regression-pin tests for the TB-235 LLM-driven dependency-coherence
validator (`_validate_briefing_structure` check #7).

Every case here mocks the SDK round-trip — `dep_judge_fn` is the
injection seam — so the tests run deterministically without touching
Anthropic's API. The optional `AP2_REAL_SDK=1` smoke under
`ap2/tests/smoke/` exercises the real Haiku judge against TB-220's
historical briefing; that one is the live-API canary, this module is
the unit floor for the validator's decision logic.

Cases (mirror the Scope §7 contract from the briefing):
  (a) judge identifies a hard predecessor MATCHING the task's
      `@blocked:` codespan → pass.
  (b) judge identifies a hard predecessor NOT in `@blocked:` →
      reject with a specific error message naming the missing TB-N
      and the judge's reasoning verbatim.
  (c) judge identifies an empty hard-predecessor list → pass.
  (d) judge returns malformed JSON (non-dict response) → log
      `validator_judge_fail` event + pass (fail-open).
  (e) judge timeout → log `validator_judge_timeout` event + pass
      (fail-open).
  (f) `AP2_VALIDATOR_JUDGE_DISABLED=1` is set → check #7 skipped
      entirely, no judge call made, no event emitted.
  (g) the reject error message includes both the missing TB-N AND the
      judge's reasoning verbatim (the operator's diagnostic).

A two-axis check also pins that check #7 runs AFTER all six
deterministic checks (i.e. a structurally-malformed briefing rejects
before the judge ever sees it — the judge never wastes tokens on
noise). Together with the seven cases above this covers the full
Scope §7 contract.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ap2 import events, tools
from ap2.tests._briefing_fixtures import (
    briefing_missing,
    canonical_briefing,
)


_CANONICAL = canonical_briefing("TB-300", title="dep-judge target")


@pytest.fixture(autouse=True)
def _unshield_validator_judge(monkeypatch):
    """Override the top-level `ap2/tests/conftest.py` shield (TB-254).

    The shield sets `AP2_VALIDATOR_JUDGE_DISABLED=1` by default for the
    unit-test session so the >18 tests that exercise `add_*` paths
    don't dispatch real Haiku-4.5 SDK calls. This module is the
    intentional-judge-exercising regression-pin for TB-235 check #7,
    so it MUST run with the shield off — the `dep_judge_fn` stubs
    only matter if `_check_dependency_coherence` doesn't short-circuit
    on the env var first. Test (f) below
    (`test_dep_judge_disabled_skips_check`) re-sets the env var
    locally to pin the off-switch path; this fixture's `delenv` is
    safely undone by that test's `monkeypatch.setenv` in monkeypatch's
    LIFO stack.
    """
    monkeypatch.delenv("AP2_COMPONENTS_VALIDATOR_JUDGE_DISABLED", raising=False)


def _events_file(tmp_path: Path) -> Path:
    """Return a per-test events file path. No daemon, no Config — we
    just need a writable path so `_check_dependency_coherence`'s
    fail-open emit branch can append.
    """
    return tmp_path / "events.jsonl"


def _make_judge(response: dict | None = None, *, raise_timeout: bool = False,
                raise_other: bool = False, captured: list | None = None):
    """Build a stub `dep_judge_fn` for injection into the validator.

    `response` is the dict the judge returns (caller decides shape:
    `{"hard_predecessors": [...], "reasoning": "..."}` is the
    canonical happy path; passing a non-dict tests the malformed-JSON
    branch).

    `raise_timeout=True` causes the stub to raise
    `tools._DepJudgeTimeout` so the timeout branch fires.
    `raise_other=True` causes a generic RuntimeError — exercises the
    `validator_judge_fail` emit + fail-open path.

    `captured` is an optional list the stub appends a per-call dict
    of its received kwargs into, so the test can assert the validator
    fed the judge the right payload (briefing + description +
    blocked_tokens).
    """
    def _fn(**kwargs):
        if captured is not None:
            captured.append(dict(kwargs))
        if raise_timeout:
            raise tools._DepJudgeTimeout("stubbed timeout")
        if raise_other:
            raise RuntimeError("stubbed sdk failure")
        return response

    return _fn


# (a) Hard predecessor present in @blocked → pass.
def test_dep_judge_pass_when_declared(tmp_path):
    captured: list[dict] = []
    judge = _make_judge(
        {"hard_predecessors": ["TB-217"], "reasoning": "needs ap2/_shared.py"},
        captured=captured,
    )
    err = tools._validate_briefing_structure(
        _CANONICAL,
        description="Add foo helper",
        blocked_csv="TB-217",
        events_file=_events_file(tmp_path),
        dep_judge_fn=judge,
    )
    assert err is None
    assert captured, "judge stub was never called"
    assert captured[0]["blocked_tokens"] == ["TB-217"]
    assert captured[0]["description"] == "Add foo helper"
    assert captured[0]["briefing_text"] == _CANONICAL


# (b) Hard predecessor NOT in @blocked → reject with specific message.
def test_dep_judge_reject_when_missing(tmp_path):
    judge = _make_judge(
        {
            "hard_predecessors": ["TB-217"],
            "reasoning": (
                "the briefing references ap2/_shared.py as a precondition, "
                "which is created by TB-217"
            ),
        },
    )
    err = tools._validate_briefing_structure(
        _CANONICAL,
        description="Add foo helper",
        blocked_csv="",  # nothing declared
        events_file=_events_file(tmp_path),
        dep_judge_fn=judge,
    )
    assert err is not None
    assert "TB-217" in err
    assert "briefing structure invalid" in err
    assert "hard predecessor" in err
    # Operator-facing fix instructions:
    assert "@blocked:TB-217" in err


# (c) Empty hard-predecessor list → pass.
def test_dep_judge_pass_when_empty_list(tmp_path):
    judge = _make_judge({"hard_predecessors": [], "reasoning": "no deps"})
    err = tools._validate_briefing_structure(
        _CANONICAL,
        description="Add foo helper",
        blocked_csv="",
        events_file=_events_file(tmp_path),
        dep_judge_fn=judge,
    )
    assert err is None


# (d) Malformed JSON / non-dict response → log + pass (fail-open).
def test_dep_judge_malformed_response_fails_open(tmp_path):
    events_file = _events_file(tmp_path)
    # Stub returns None (mirrors the default SDK helper's behavior on
    # JSON-parse failure — the SDK helper itself returns None when it
    # can't extract a JSON object from the model reply).
    judge = _make_judge(None)
    err = tools._validate_briefing_structure(
        _CANONICAL,
        description="Add foo helper",
        blocked_csv="",
        events_file=events_file,
        dep_judge_fn=judge,
    )
    # Fail-open: malformed judge response must NOT block the briefing.
    assert err is None
    # And the operator gets a `validator_judge_fail` event so a
    # climbing skip rate is observable.
    evts = events.tail(events_file, 50)
    fails = [e for e in evts if e.get("type") == "validator_judge_fail"]
    assert len(fails) == 1
    assert "non-dict" in fails[0].get("error", "")


# (e) Timeout → log + pass (fail-open).
def test_dep_judge_timeout_fails_open(tmp_path, monkeypatch):
    events_file = _events_file(tmp_path)
    monkeypatch.setenv("AP2_COMPONENTS_VALIDATOR_JUDGE_TIMEOUT_S", "3")
    judge = _make_judge(None, raise_timeout=True)
    err = tools._validate_briefing_structure(
        _CANONICAL,
        description="Add foo helper",
        blocked_csv="",
        events_file=events_file,
        dep_judge_fn=judge,
    )
    assert err is None
    evts = events.tail(events_file, 50)
    timeouts = [e for e in evts if e.get("type") == "validator_judge_timeout"]
    assert len(timeouts) == 1
    assert float(timeouts[0]["timeout_s"]) == 3.0


# (f) AP2_VALIDATOR_JUDGE_DISABLED=1 → check #7 skipped entirely.
def test_dep_judge_disabled_skips_check(tmp_path, monkeypatch):
    events_file = _events_file(tmp_path)
    monkeypatch.setenv("AP2_COMPONENTS_VALIDATOR_JUDGE_DISABLED", "1")
    # The judge stub should NEVER be called; pin that via a sentinel
    # exception — if it fires, the test fails with a clear trace.
    def _explode(**_kwargs):
        raise AssertionError(
            "AP2_VALIDATOR_JUDGE_DISABLED=1 should bypass the judge"
        )

    err = tools._validate_briefing_structure(
        _CANONICAL,
        description="Add foo helper",
        blocked_csv="",
        events_file=events_file,
        dep_judge_fn=_explode,
    )
    assert err is None
    # No event should have been emitted (the disable is a clean
    # skip, not a fail-open).
    assert not events_file.exists() or events_file.read_text() == ""


# (g) Reject error includes BOTH the missing TB-N AND the judge's
# reasoning verbatim (operator's diagnostic).
def test_dep_judge_reject_message_includes_reasoning_and_id(tmp_path):
    reasoning = (
        "the briefing's `## Scope` references `ap2/foo.py` as a "
        "precondition, which is created by TB-217"
    )
    judge = _make_judge(
        {"hard_predecessors": ["TB-217"], "reasoning": reasoning},
    )
    err = tools._validate_briefing_structure(
        _CANONICAL,
        description="Add foo helper",
        blocked_csv="TB-999",  # something declared, but not TB-217
        events_file=_events_file(tmp_path),
        dep_judge_fn=judge,
    )
    assert err is not None
    assert "TB-217" in err
    assert reasoning in err
    # Mention the operator's two fix paths verbatim — keeps the
    # phrasing pinned so a future copy-edit doesn't silently drop
    # one of them.
    assert "Either add @blocked:TB-217" in err
    assert "rephrase" in err


# Axis check: check #7 runs AFTER the six deterministic checks (a
# structurally-malformed briefing rejects before the judge is ever
# asked). Cheaper, and the operator gets the more specific error.
def test_dep_judge_skipped_when_deterministic_check_fails(tmp_path):
    captured: list[dict] = []
    judge = _make_judge(
        {"hard_predecessors": ["TB-217"], "reasoning": "x"},
        captured=captured,
    )
    # Drop the `## Verification` section so check #2 (parseable
    # Verification) rejects first.
    bad = briefing_missing("TB-301", drop="Verification")
    err = tools._validate_briefing_structure(
        bad,
        description="",
        blocked_csv="",
        events_file=_events_file(tmp_path),
        dep_judge_fn=judge,
    )
    assert err is not None
    assert "Verification" in err
    # The judge was never called — the deterministic gate fired first.
    assert captured == [], (
        "judge should not be invoked on structurally-malformed input; "
        f"got {captured!r}"
    )


# Axis check: case-insensitive token match. An author who writes
# `@blocked:tb-217` (lowercase) should not be rejected when the judge
# names `TB-217` — the validator normalizes case both ways.
def test_dep_judge_case_insensitive_token_match(tmp_path):
    judge = _make_judge(
        {"hard_predecessors": ["TB-217"], "reasoning": "x"},
    )
    err = tools._validate_briefing_structure(
        _CANONICAL,
        description="x",
        blocked_csv="tb-217",
        events_file=_events_file(tmp_path),
        dep_judge_fn=judge,
    )
    assert err is None


# Axis check: the judge ignores non-TB-shaped tokens in the
# `hard_predecessors` list. A judge that hallucinates a non-TB-N
# string (e.g. "some-file.py") must NOT trigger a false reject.
def test_dep_judge_ignores_non_tb_n_tokens(tmp_path):
    judge = _make_judge(
        {"hard_predecessors": ["ap2/_shared.py", ""], "reasoning": "x"},
    )
    err = tools._validate_briefing_structure(
        _CANONICAL,
        description="x",
        blocked_csv="",
        events_file=_events_file(tmp_path),
        dep_judge_fn=judge,
    )
    assert err is None


# Axis check: a generic SDK failure (non-timeout exception) emits
# `validator_judge_fail` and fails open. Distinct event-type from
# the timeout branch so an operator can spot which infra hiccup is
# dominating.
def test_dep_judge_generic_failure_emits_fail_event(tmp_path):
    events_file = _events_file(tmp_path)
    judge = _make_judge(None, raise_other=True)
    err = tools._validate_briefing_structure(
        _CANONICAL,
        description="x",
        blocked_csv="",
        events_file=events_file,
        dep_judge_fn=judge,
    )
    assert err is None
    evts = events.tail(events_file, 50)
    fails = [e for e in evts if e.get("type") == "validator_judge_fail"]
    assert len(fails) == 1
    assert "RuntimeError" in fails[0]["error"]
    assert "stubbed sdk failure" in fails[0]["error"]


# Axis check: when neither `events_file` nor `dep_judge_fn` is
# supplied (existing call sites in `ap2/tests/test_tools.py` that
# only exercise the deterministic checks), check #7 is bypassed —
# the validator stays backward-compatible with the >30 historical
# call sites.
def test_dep_judge_skipped_when_no_events_file_or_judge():
    err = tools._validate_briefing_structure(
        _CANONICAL,
        description="this briefing claims TB-217 must already be on disk",
        blocked_csv="",
    )
    # The text deliberately mentions TB-217 as a predecessor; without
    # an `events_file` / `dep_judge_fn` opt-in, check #7 never fires
    # so the briefing passes. Mirrors how `test_tools.py`'s legacy
    # validator-unit tests stay green.
    assert err is None


# Env-knob smoke: AP2_VALIDATOR_JUDGE_TIMEOUT_S parses correctly
# (default 15, override via env). Pin the propagation path.
def test_dep_judge_timeout_env_knob_parses(tmp_path, monkeypatch):
    monkeypatch.setenv("AP2_COMPONENTS_VALIDATOR_JUDGE_TIMEOUT_S", "42")
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
    assert captured[0]["timeout_s"] == 42.0


# Env-knob smoke: AP2_VALIDATOR_JUDGE_MAX_TURNS parses correctly
# (TB-249: replaced AP2_VALIDATOR_JUDGE_MAX_TOKENS — the legacy knob
# now resolves into `max_turns` via a deprecated-alias path covered by
# `test_tb_validator_judge_sdk_args.py`).
def test_dep_judge_max_turns_env_knob_parses(tmp_path, monkeypatch):
    monkeypatch.delenv("AP2_COMPONENTS_VALIDATOR_JUDGE_MAX_TOKENS", raising=False)
    monkeypatch.setenv("AP2_COMPONENTS_VALIDATOR_JUDGE_MAX_TURNS", "4")
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
    assert captured[0]["max_turns"] == 4


# Env-knob default smoke: with neither env var set, the validator
# falls back to the module-level defaults (15s timeout, 2 max turns —
# TB-249 migrated from max_tokens to max_turns, per the SDK's native
# budget primitive). Pin the defaults so a future tweak to the
# constants trips this test (forcing the env-knob docs to update in
# lockstep).
def test_dep_judge_env_knob_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv("AP2_COMPONENTS_VALIDATOR_JUDGE_TIMEOUT_S", raising=False)
    monkeypatch.delenv("AP2_COMPONENTS_VALIDATOR_JUDGE_MAX_TOKENS", raising=False)
    monkeypatch.delenv("AP2_COMPONENTS_VALIDATOR_JUDGE_MAX_TURNS", raising=False)
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
    assert captured[0]["timeout_s"] == tools._VALIDATOR_JUDGE_TIMEOUT_S_DEFAULT
    assert captured[0]["max_turns"] == tools._VALIDATOR_JUDGE_MAX_TURNS_DEFAULT


# TB-419: the validator judge no longer hard-codes a Claude model. It is a
# cost-sensitive sub-call, so it resolves the LIGHT tier of whichever adapter
# backs the `validator_judge` kind. Pin that the judge targets the resolved
# adapter's light tier (never the heavy/opus tier) so an accidental swap to the
# heavy tier — which would blow the per-call cost target — trips a focused test.
def test_dep_judge_targets_resolved_adapter_light_tier(tmp_path, monkeypatch):
    from ap2.adapters import ClaudeCodeAdapter, CodexAdapter
    from ap2.briefing_validators import _validator_judge_model
    from ap2.config import Config
    from ap2.init import init_project

    for name in list(__import__("os").environ):
        if name.startswith("AP2_"):
            monkeypatch.delenv(name, raising=False)
    init_project(tmp_path)
    cfg = Config.load(tmp_path)

    # cfg-less seam → the default Claude adapter's light tier (sonnet, NOT opus).
    assert _validator_judge_model(None) == ClaudeCodeAdapter().default_model_light
    assert "opus" not in _validator_judge_model(None).lower()

    # Claude-routed judge → Claude light tier.
    monkeypatch.setenv("AP2_AGENT_BACKEND_VALIDATOR_JUDGE", "claude")
    assert _validator_judge_model(cfg) == ClaudeCodeAdapter().default_model_light

    # Codex-routed judge → Codex light tier, NOT a Claude id (the leak the
    # adapter tier avoids out of the box).
    monkeypatch.setenv("AP2_AGENT_BACKEND_VALIDATOR_JUDGE", "codex")
    resolved = _validator_judge_model(cfg)
    assert resolved == CodexAdapter().default_model_light
    assert not resolved.startswith("claude")

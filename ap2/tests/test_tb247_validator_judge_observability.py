"""TB-247: validator-judge parse-failure observability + prompt-tightening.

The dependency-coherence validator judge (`ap2.tools.
_judge_dep_coherence_default`, invoked by `_check_dependency_coherence`)
gates new task briefings on hard-predecessor coherence — any TB-N
referenced as a precondition must appear in the task's `@blocked:`
codespan.

Pre-TB-247, when the judge's final message couldn't be parsed into a
``{"hard_predecessors": [...], "reasoning": "..."}`` dict, the function:

  1. Returned ``None`` on four failure branches (empty text / no
     braces / JSONDecodeError / non-dict).
  2. Triggered the dispatcher's catch-all ``validator_judge_fail`` event
     with only ``error="non-dict judge response"`` — no raw response,
     no per-failure categorization.

Within <4h of TB-243 shipping the count surface for these failures
(2026-05-16T23:59Z → 2026-05-17T02:33Z), the very first two ideation
cycles both hit the catch-all — 2/2 wild failure rate against a
critical fail-open gate under live auto-approve. The operator saw the
count climb but had zero diagnostic data to act on.

TB-247 transplants TB-236's prose-judge fix (commit ``f32374f``) onto
the validator judge — byte-for-byte pattern parity, not invention:

  - **Prevention.** Tightened prompt with explicit OUTPUT CONTRACT,
    inline example, ≤200-character ``reasoning`` cap, no markdown
    fences / preamble / trailing prose.
  - **Observability — dump.** On parse failure, the FULL raw last-
    assistant-text is written to
    ``.cc-autopilot/debug/<UTC-ts>-validator-judge-response.txt``.
    Successful parses leave nothing on disk.
  - **Observability — categorization.** ``validator_judge_fail`` events
    now carry ``parse_error`` (one of ``ap2.tools._DEP_JUDGE_PARSE_ERRORS``:
    ``empty_text`` / ``no_braces`` / ``json_decode`` / ``non_dict`` /
    ``sdk_exception``) and ``debug_path`` (path to the dump file, when
    the dump landed).

Tests below pin all six briefing-§Scope behaviors:
  (a) malformed-text path → dump file lands, content is full raw text,
      event carries ``debug_path`` + ``parse_error="json_decode"``.
  (b) non-dict-JSON path (e.g. ``[1, 2, 3]``) → dump fires with
      ``parse_error="non_dict"``.
  (c) no-braces path (prose-only response) → dump fires with
      ``parse_error="no_braces"``.
  (d) successful-parse path → NO dump file written.
  (e) OSError on dump write swallows cleanly — judge still returns
      None-data outcome, no crash propagation.
  (f) prompt-text regression pin: the new "JSON object only" + "200
      characters" directives + inline example are present in
      ``_judge_dep_coherence_default``'s source.

Plus end-to-end pins on the event-payload enrichment via
``_check_dependency_coherence`` for both the parse-failure outcome
path and the SDK-exception path.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ap2 import events, tools
from ap2.tests._briefing_fixtures import canonical_briefing


_CANONICAL = canonical_briefing("TB-300", title="dep-judge target")


@pytest.fixture(autouse=True)
def _unshield_validator_judge(monkeypatch):
    """TB-254: override the top-level `ap2/tests/conftest.py` shield.

    The shield sets `AP2_VALIDATOR_JUDGE_DISABLED=1` for the whole
    unit-test session so most tests don't accidentally trigger real
    Haiku-4.5 SDK calls. This module's end-to-end cases call
    `_validate_briefing_structure` with a `dep_judge_fn` stub and
    expect the judge stub to fire (so its parse-failure outcomes
    surface in `validator_judge_fail` event payloads); the shield
    would short-circuit `_check_dependency_coherence` before the
    stub ever runs. delenv at test-start lets the stub fire, and
    monkeypatch restores the shield on teardown so the shield's
    cross-module guarantee is preserved.
    """
    monkeypatch.delenv("AP2_COMPONENTS_VALIDATOR_JUDGE_DISABLED", raising=False)


# ---------------------------------------------------------------------------
# (a) – (d): pure parse + dump function
# ---------------------------------------------------------------------------


def test_parse_dump_on_malformed_text_json_decode(tmp_path):
    """Malformed JSON (JSONDecodeError) → full raw text dumped + outcome
    carries ``parse_error='json_decode'`` and a populated ``dump_path``.

    The dump file's content must be the FULL raw response byte-for-byte
    — NOT the ``start..end+1`` substring, NOT truncated to 200 chars
    (the pre-TB-247 ``notes`` cap that motivated the original TB-236
    fix). 800-char rationale proves we capture beyond any preview cap.
    """
    events_file = tmp_path / "events.jsonl"
    long_reasoning = "x" * 800
    malformed = (
        '{"hard_predecessors": ["TB-1"], "reasoning": "'
        + long_reasoning
        + 'BAD"QUOTE"}'
    )
    outcome = tools._parse_dep_judge_response(
        malformed, events_file=events_file,
    )
    assert outcome.data is None
    assert outcome.parse_error == "json_decode"
    assert outcome.dump_path is not None
    assert outcome.dump_path.exists()
    # FULL raw text, byte-for-byte.
    assert outcome.dump_path.read_text() == malformed
    assert len(outcome.dump_path.read_text()) > 800


def test_parse_dump_on_non_dict_json(tmp_path):
    """Valid JSON that's not a dict (e.g. list) → dump fires with
    ``parse_error='non_dict'``.

    This is the ACTUAL wild failure shape observed 2026-05-17T00:29Z
    and 02:33Z (``error="non-dict judge response"`` in the
    ``validator_judge_fail`` payloads). Pre-TB-247 the operator had no
    way to know whether the underlying response was a list, a string,
    a null, or what — only the catch-all label fired.
    """
    events_file = tmp_path / "events.jsonl"
    text = "[1, 2, 3]"
    outcome = tools._parse_dep_judge_response(text, events_file=events_file)
    assert outcome.data is None
    assert outcome.parse_error == "non_dict"
    assert outcome.dump_path is not None
    assert outcome.dump_path.read_text() == text


def test_parse_dump_on_no_braces(tmp_path):
    """Prose-only response (no ``{`` / ``}``) → dump fires with
    ``parse_error='no_braces'``.

    Distinct from ``json_decode`` because no JSON parse was even
    attempted — the response had no object-delimiters to anchor the
    extraction. Common when the model returns a refusal sentence or
    an apology paragraph instead of the contracted JSON object.
    """
    events_file = tmp_path / "events.jsonl"
    text = "I cannot validate this briefing right now — please retry."
    outcome = tools._parse_dep_judge_response(text, events_file=events_file)
    assert outcome.data is None
    assert outcome.parse_error == "no_braces"
    assert outcome.dump_path is not None
    assert outcome.dump_path.read_text() == text


def test_parse_dump_on_empty_text(tmp_path):
    """Empty text (SDK returned no last-assistant-text) →
    ``parse_error='empty_text'``, dump still fires with empty contents.

    Writing the empty dump is deliberate: the operator wants to see
    the empty-response signature on disk too (proves the categorization
    is correct, not a misclassification of garbage as empty).
    """
    events_file = tmp_path / "events.jsonl"
    outcome = tools._parse_dep_judge_response("", events_file=events_file)
    assert outcome.data is None
    assert outcome.parse_error == "empty_text"
    assert outcome.dump_path is not None
    assert outcome.dump_path.read_text() == ""


def test_no_dump_on_successful_parse(tmp_path):
    """Well-formed dict response → no dump file on disk, ``parse_error``
    is None, ``data`` carries the parsed dict.

    Dumps are opt-in on failure only — steady-state successful judging
    accumulates many calls per day and a dump per call would bloat the
    debug dir with useless noise (mirrors TB-236's same policy).
    """
    events_file = tmp_path / "events.jsonl"
    text = '{"hard_predecessors": ["TB-217"], "reasoning": "ok"}'
    outcome = tools._parse_dep_judge_response(text, events_file=events_file)
    assert outcome.data == {
        "hard_predecessors": ["TB-217"], "reasoning": "ok",
    }
    assert outcome.parse_error is None
    assert outcome.dump_path is None
    # Debug dir is allowed to not exist on success.
    debug_dir = events_file.parent / "debug"
    if debug_dir.exists():
        assert list(debug_dir.iterdir()) == [], list(debug_dir.iterdir())


def test_dump_filename_shape(tmp_path):
    """Dump filename matches the TB-247 convention:
    ``<UTC-ts>-validator-judge-response.txt`` where ``<UTC-ts>`` is
    ``%Y%m%dT%H%M%SZ`` (mirrors TB-236's verify-side dump format).
    """
    events_file = tmp_path / "events.jsonl"
    outcome = tools._parse_dep_judge_response(
        "no braces", events_file=events_file,
    )
    assert outcome.dump_path is not None
    name = outcome.dump_path.name
    assert name.endswith("-validator-judge-response.txt")
    # `<UTC-ts>` is 16 chars: `YYYYMMDDTHHMMSSZ`.
    prefix = name[: -len("-validator-judge-response.txt")]
    assert len(prefix) == 16, prefix
    assert prefix[8] == "T" and prefix[-1] == "Z", prefix
    # And lands in the events-file-sibling `debug/` dir.
    assert outcome.dump_path.parent == events_file.parent / "debug"


# ---------------------------------------------------------------------------
# (e): OSError on dump write swallows cleanly
# ---------------------------------------------------------------------------


def test_oserror_on_dump_write_swallows_cleanly(tmp_path, monkeypatch):
    """OSError on dump write (full disk, permission denied, etc.) MUST
    NOT propagate out of ``_parse_dep_judge_response``.

    The outcome still carries ``parse_error`` (so the dispatcher's
    fail-open path still emits ``validator_judge_fail`` with the
    category) but ``dump_path`` is None because the diagnostic write
    failed. Mirrors TB-236's best-effort write pattern in
    ``ap2/verify.py::_judge_prose_bullet``.
    """
    events_file = tmp_path / "events.jsonl"
    real_write_text = Path.write_text

    def _explode(self, *a, **kw):  # noqa: ANN001
        if "validator-judge-response" in self.name:
            raise OSError("simulated disk full")
        return real_write_text(self, *a, **kw)

    monkeypatch.setattr(Path, "write_text", _explode)

    outcome = tools._parse_dep_judge_response(
        "garbage with no braces", events_file=events_file,
    )
    assert outcome.data is None
    # Parse-error category still set (categorization is independent of
    # the dump write).
    assert outcome.parse_error == "no_braces"
    # Dump write failed → path swallowed.
    assert outcome.dump_path is None


def test_oserror_on_debug_dir_mkdir_swallows_cleanly(tmp_path, monkeypatch):
    """OSError on debug-dir mkdir (parent dir read-only, etc.) MUST NOT
    propagate either — same best-effort posture for the dir-create
    step as for the write step.
    """
    events_file = tmp_path / "events.jsonl"
    real_mkdir = Path.mkdir

    def _explode(self, *a, **kw):  # noqa: ANN001
        if self.name == "debug":
            raise OSError("simulated permission denied")
        return real_mkdir(self, *a, **kw)

    monkeypatch.setattr(Path, "mkdir", _explode)

    outcome = tools._parse_dep_judge_response(
        "[1, 2, 3]", events_file=events_file,
    )
    assert outcome.data is None
    assert outcome.parse_error == "non_dict"
    assert outcome.dump_path is None


# ---------------------------------------------------------------------------
# (f): prompt-text regression pin
# ---------------------------------------------------------------------------


def test_prompt_contains_json_object_only_directive():
    """Pin the TB-247 "JSON object only" directive so a future copy-edit
    doesn't silently drop the contract that prevents the parse failures
    TB-243 surfaced. The verification bullet ``grep -nE "JSON object
    only" ap2/tools.py`` pins the same string at the source level — this
    test asserts the string actually lives inside the judge's prompt
    builder (not just somewhere in the module)."""
    import inspect

    src = inspect.getsource(tools._judge_dep_coherence_default)
    assert "JSON object only" in src, (
        "TB-247 prompt-tightening: 'JSON object only' directive missing "
        "from _judge_dep_coherence_default"
    )


def test_prompt_contains_200_character_cap():
    """Pin the ≤200-character reasoning cap. The shorter the reasoning,
    the smaller the surface area for JSON-escape bugs — TB-236's
    canonical root cause for the prose judge."""
    import inspect

    src = inspect.getsource(tools._judge_dep_coherence_default)
    assert "200 characters" in src, (
        "TB-247 prompt-tightening: '200 characters' rationale cap missing "
        "from _judge_dep_coherence_default"
    )


def test_prompt_contains_inline_example():
    """The inline example demonstrates the exact response shape. Pin
    the canonical example fragment so a refactor can't drop it without
    a deliberate decision."""
    import inspect

    src = inspect.getsource(tools._judge_dep_coherence_default)
    # The example uses concrete TB-N + reasoning shape.
    assert "hard_predecessors" in src
    assert "reasoning" in src
    # And the example specifically uses a concrete TB-N token.
    assert "TB-217" in src, (
        "TB-247 prompt-tightening: inline example with concrete TB-N "
        "is missing from the prompt builder"
    )


def test_prompt_forbids_markdown_fences():
    """Pin the explicit ban on markdown code fences — the actual wild
    failure mode for many models in the wild is to wrap JSON in
    ```json``` fences, which the simple ``find('{')`` / ``rfind('}')``
    extractor handles, but the OUTPUT CONTRACT should still forbid it
    explicitly so the model sees a clear directive."""
    import inspect

    src = inspect.getsource(tools._judge_dep_coherence_default)
    # The literal directive ('No markdown code fences') is present.
    assert "markdown code fences" in src or "markdown fences" in src, src


# ---------------------------------------------------------------------------
# Parse-error category enum completeness
# ---------------------------------------------------------------------------


def test_parse_error_categories_complete():
    """Every category named in the briefing is present in the module's
    enum so events.jsonl readers can rely on the closed set when
    filtering / aggregating."""
    assert "empty_text" in tools._DEP_JUDGE_PARSE_ERRORS
    assert "no_braces" in tools._DEP_JUDGE_PARSE_ERRORS
    assert "json_decode" in tools._DEP_JUDGE_PARSE_ERRORS
    assert "non_dict" in tools._DEP_JUDGE_PARSE_ERRORS
    assert "sdk_exception" in tools._DEP_JUDGE_PARSE_ERRORS


# ---------------------------------------------------------------------------
# End-to-end: event payload enrichment via `_check_dependency_coherence`
# ---------------------------------------------------------------------------


def _events_file(tmp_path: Path) -> Path:
    return tmp_path / "events.jsonl"


def _make_outcome_judge(outcome: tools._DepJudgeOutcome):
    """Stub `dep_judge_fn` that returns a pre-built `_DepJudgeOutcome`
    — exercises the post-TB-247 return shape end-to-end without
    needing the real SDK."""
    def _fn(**_kwargs):
        return outcome
    return _fn


def test_validator_judge_fail_event_carries_debug_path_and_parse_error(
    tmp_path,
):
    """When the judge returns a parse-failure outcome with a
    ``dump_path`` and ``parse_error``, the ``validator_judge_fail`` event
    carries BOTH ``debug_path`` and ``parse_error`` fields — the
    operator's actionable diagnostic surface added in this task.
    """
    events_file = _events_file(tmp_path)
    fake_dump = (
        events_file.parent / "debug"
        / "20260517T000000Z-validator-judge-response.txt"
    )
    fake_dump.parent.mkdir(parents=True, exist_ok=True)
    fake_dump.write_text("the full raw judge response goes here")

    judge = _make_outcome_judge(
        tools._DepJudgeOutcome(
            data=None,
            parse_error="json_decode",
            dump_path=fake_dump,
        ),
    )

    err = tools._validate_briefing_structure(
        _CANONICAL,
        description="x",
        blocked_csv="",
        events_file=events_file,
        dep_judge_fn=judge,
    )
    # Fail-open: parse failure must NOT block the briefing.
    assert err is None

    evts = events.tail(events_file, 50)
    fails = [e for e in evts if e.get("type") == "validator_judge_fail"]
    assert len(fails) == 1, fails
    e = fails[0]
    assert e.get("parse_error") == "json_decode"
    assert e.get("debug_path") == str(fake_dump)
    # Existing "non-dict" error string still present — TB-243's count
    # surface keys off the event type (not the error string), but a
    # human grepping logs for "non-dict" expects to keep finding hits.
    assert "non-dict" in e.get("error", "")


def test_validator_judge_fail_event_omits_debug_path_when_dump_swallowed(
    tmp_path,
):
    """When the outcome carries ``parse_error`` but ``dump_path`` is None
    (e.g. the OSError-swallow path), the event still carries
    ``parse_error`` but ``debug_path`` is absent — no synthetic empty-
    string field bloat."""
    events_file = _events_file(tmp_path)
    judge = _make_outcome_judge(
        tools._DepJudgeOutcome(
            data=None,
            parse_error="non_dict",
            dump_path=None,
        ),
    )

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
    assert len(fails) == 1, fails
    assert fails[0].get("parse_error") == "non_dict"
    assert "debug_path" not in fails[0]


def test_validator_judge_fail_event_marks_sdk_exception(tmp_path):
    """SDK-exception branch → ``parse_error='sdk_exception'`` on the
    emitted event. No ``debug_path`` (no raw text was ever produced
    for this branch — the exception fired before any response came
    back)."""
    events_file = _events_file(tmp_path)

    def _explode(**_kwargs):
        raise RuntimeError("simulated SDK failure")

    err = tools._validate_briefing_structure(
        _CANONICAL,
        description="x",
        blocked_csv="",
        events_file=events_file,
        dep_judge_fn=_explode,
    )
    assert err is None  # fail-open

    evts = events.tail(events_file, 50)
    fails = [e for e in evts if e.get("type") == "validator_judge_fail"]
    assert len(fails) == 1, fails
    e = fails[0]
    assert e.get("parse_error") == "sdk_exception"
    assert "RuntimeError" in e.get("error", "")
    assert "debug_path" not in e


def test_legacy_dict_stub_still_works(tmp_path):
    """Pre-TB-247 test stubs that return plain ``dict | None`` (not
    ``_DepJudgeOutcome``) and don't accept the new ``events_file`` kwarg
    must keep working unchanged. Back-compat for the
    ``test_dep_validator_judge`` module — pre-TB-247 fixtures stay
    green without edits."""
    events_file = _events_file(tmp_path)

    def _legacy(*, briefing_text, description, blocked_tokens,
                timeout_s, max_turns):  # noqa: ARG001
        # Old signature: no `events_file` kwarg; returns dict directly.
        return {"hard_predecessors": [], "reasoning": "no deps"}

    err = tools._validate_briefing_structure(
        _CANONICAL,
        description="x",
        blocked_csv="",
        events_file=events_file,
        dep_judge_fn=_legacy,
    )
    assert err is None
    # No fail-open event for a clean parse.
    evts = events.tail(events_file, 50) if events_file.exists() else []
    assert not any(
        e.get("type") == "validator_judge_fail" for e in evts
    )


def test_legacy_dict_stub_none_return_still_fails_open(tmp_path):
    """Legacy stub returning ``None`` (no diagnostic info) still fires
    the ``validator_judge_fail`` event with the catch-all error string
    — no ``parse_error`` / ``debug_path`` since the legacy stub couldn't
    provide them. Mirrors pre-TB-247 behavior for back-compat."""
    events_file = _events_file(tmp_path)

    def _legacy(*, briefing_text, description, blocked_tokens,
                timeout_s, max_turns):  # noqa: ARG001
        return None

    err = tools._validate_briefing_structure(
        _CANONICAL,
        description="x",
        blocked_csv="",
        events_file=events_file,
        dep_judge_fn=_legacy,
    )
    assert err is None  # fail-open
    evts = events.tail(events_file, 50)
    fails = [e for e in evts if e.get("type") == "validator_judge_fail"]
    assert len(fails) == 1, fails
    assert "non-dict" in fails[0].get("error", "")
    # Legacy stub provided no diagnostic data → these fields absent.
    assert "parse_error" not in fails[0]
    assert "debug_path" not in fails[0]


def test_outcome_judge_pass_path_unchanged(tmp_path):
    """Happy path: outcome carries a parsed dict → validator runs the
    normal dep-coherence check, no fail-open event. Pin that the
    outcome wrapping doesn't accidentally fall through to the
    fail-open branch when ``data`` is a valid dict."""
    events_file = _events_file(tmp_path)
    judge = _make_outcome_judge(
        tools._DepJudgeOutcome(
            data={"hard_predecessors": [], "reasoning": "no deps"},
            parse_error=None,
            dump_path=None,
        ),
    )

    err = tools._validate_briefing_structure(
        _CANONICAL,
        description="x",
        blocked_csv="",
        events_file=events_file,
        dep_judge_fn=judge,
    )
    assert err is None
    evts = events.tail(events_file, 50) if events_file.exists() else []
    assert not any(
        e.get("type") == "validator_judge_fail" for e in evts
    )


def test_outcome_judge_reject_path_includes_reasoning(tmp_path):
    """Happy reject path via outcome shape: outcome carries a dict with a
    missing hard predecessor → validator rejects with the operator-
    facing error string. Confirms the outcome unpacking preserves the
    pre-TB-247 reject behavior."""
    events_file = _events_file(tmp_path)
    judge = _make_outcome_judge(
        tools._DepJudgeOutcome(
            data={
                "hard_predecessors": ["TB-217"],
                "reasoning": "the briefing imports ap2/_shared.py",
            },
            parse_error=None,
            dump_path=None,
        ),
    )

    err = tools._validate_briefing_structure(
        _CANONICAL,
        description="x",
        blocked_csv="",
        events_file=events_file,
        dep_judge_fn=judge,
    )
    assert err is not None
    assert "TB-217" in err
    assert "@blocked:TB-217" in err

"""TB-236: prose-judge parse-failure observability + prompt-tightening tests.

The prose judge (``ap2.verify._judge_prose_bullet``) is responsible for
turning a free-form acceptance bullet into a pass/fail verdict via an SDK
call against the cumulative task diff + working tree. Pre-TB-236, when
the SDK's final message couldn't be parsed into a
``{"status": ..., "rationale": ...}`` envelope, the verifier:

  1. Recorded the bullet as ``unverified`` (soft-pass — the daemon treats
     ``verification_partial`` as Complete under ``AP2_AUTO_APPROVE=1``).
  2. Truncated the offending response to 200 chars and stuffed it into
     the event's ``notes`` field.

Under auto-approve that combination silently skipped prose bullets with
no diagnostic surface. TB-228 bullet 7 (2026-05-15T18:30:53Z) was the
observed instance that motivated this TB. TB-231 proposed retrying the
SDK call — operator rejected the shape (doubles cost without telling us
WHY the parse failed). TB-236 ships the root-cause replacement:

  - **Prevention**: the prompt now caps the rationale at 200 characters
    and is explicit that the FINAL message must be JSON-only (no markdown
    fences, no preamble, no trailing prose). Intermediate Read/Grep tool
    calls stay legal — only the last message is constrained.
  - **Observability — dump**: on parse failure, the FULL raw last-
    assistant-text is written to
    ``.cc-autopilot/debug/<run_ts>-<task>-judge-bullet<idx>-response.txt``.
    Successful parses leave nothing on disk.
  - **Observability — categorization**: on parse failure, the
    ``judge_call`` event carries a ``parse_error`` field tagged with one
    of ``ap2.verify.PARSE_ERROR_CATEGORIES`` so the operator can pattern-
    detect across many failures without opening every dump.
  - **Observability — length signals**: every ``judge_call`` event
    carries ``response_length`` (chars), and successful parses also
    carry ``rationale_length`` — lets operators track whether prompt-
    tightening is actually shortening rationales over time.

Tests below pin all six behaviors. The stub SDK mirrors the shape used
in ``ap2/tests/e2e/test_verify_per_task.py``'s TB-157 judge-event tests
— one assistant text envelope + (optionally) a ResultMessage carrying
usage / cost. The judge's own SDK loop in ``_judge_prose_bullet`` is what
we're exercising; the parser ``_parse_judge_response`` is exercised
indirectly through it (its outcomes drive the event fields under test).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ap2 import events
from ap2.verify import (
    PARSE_ERROR_CATEGORIES,
    VerifyBullet,
    _categorize_parse_error,
    _judge_prose_bullet,
)


# --------------------------------------------------------------------------
# Stub SDK helpers
# --------------------------------------------------------------------------


class _StubSDK:
    """Stub mirror of ``claude_agent_sdk``'s surface used by the judge.

    Yields one assistant-text envelope with ``self.text`` and (when
    ``with_result=True``) a ResultMessage carrying usage / cost so the
    judge_call event's TB-157 fields populate too. The text is whatever
    the caller wires up — well-formed JSON for the success cases, the
    various malformed shapes for the failure cases.
    """

    def __init__(self, text: str, *, with_result: bool = True) -> None:
        self.text = text
        self.with_result = with_result
        # Capture the prompt so tests can assert on prompt content
        # without monkeypatching the SDK shape (e.g. the rationale-
        # length constraint test).
        self.captured_prompt: str | None = None

    class ClaudeAgentOptions:
        def __init__(self, **kw):
            self.kw = kw

    def query(self, *, prompt, options):  # noqa: ARG002
        self.captured_prompt = prompt

        async def _gen():
            yield SimpleNamespace(content=[SimpleNamespace(text=self.text)])
            if self.with_result:
                yield SimpleNamespace(
                    content=None,
                    model="claude-opus-4-7",
                    num_turns=1,
                    total_cost_usd=0.001,
                    stop_reason="end_turn",
                    usage={
                        "input_tokens": 100,
                        "output_tokens": 20,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                    },
                )

        return _gen()


def _run_judge(
    stub: _StubSDK,
    *,
    project_root: Path,
    events_file: Path | None,
    task_id: str = "TB-236",
    bullet_idx: int = 0,
) -> object:
    """Drive ``_judge_prose_bullet`` synchronously for the tests."""
    bullet = VerifyBullet(kind="prose", text="The diff adds foo.py")
    return asyncio.run(
        _judge_prose_bullet(
            bullet,
            project_root=project_root,
            sdk=stub,
            diff_text="diff --git a/foo.py b/foo.py\n+def foo(): ...\n",
            events_file=events_file,
            task_id=task_id,
            bullet_idx=bullet_idx,
        )
    )


def _judge_event(events_file: Path) -> dict:
    rows = events.tail(events_file, n=10)
    judge_rows = [r for r in rows if r.get("type") == "judge_call"]
    assert len(judge_rows) == 1, judge_rows
    return judge_rows[0]


def _dump_dir(events_file: Path) -> Path:
    return events_file.parent / "debug"


# --------------------------------------------------------------------------
# Scope §2: dump file on parse failure
# --------------------------------------------------------------------------


def test_response_dumped_on_parse_failure(tmp_path):
    """Malformed-JSON response → debug file with FULL raw last-message text.

    The verifier's pre-TB-236 ``notes`` truncated to 200 chars, which made
    it impossible to tell whether the failure was an unescaped quote,
    trailing prose, truncation, or something else. The dump captures the
    full text so the operator can diagnose.
    """
    events_file = tmp_path / "events.jsonl"
    # Long enough to PROVE we capture beyond the 200-char `notes` cap.
    long_rationale = "x" * 800
    malformed = (
        '{"status": "pass", "rationale": "' + long_rationale + 'BAD"QUOTE"}'
    )
    stub = _StubSDK(malformed)

    res = _run_judge(stub, project_root=tmp_path, events_file=events_file)
    assert res.status == "unverified"

    debug_dir = _dump_dir(events_file)
    dumps = sorted(debug_dir.glob("*-judge-bullet*-response.txt"))
    assert len(dumps) == 1, dumps
    content = dumps[0].read_text()
    # FULL response captured — not just the start[:end+1] substring, and
    # not truncated to 200 chars.
    assert content == malformed
    assert len(content) > 800


def test_no_dump_on_successful_parse(tmp_path):
    """Successful judge parse → nothing written to .cc-autopilot/debug/.

    The dump path is opt-in on failure only — successful judge calls
    accumulate at >100/day under steady state and a dump per call would
    bloat the debug dir with useless noise.
    """
    events_file = tmp_path / "events.jsonl"
    stub = _StubSDK('{"status": "pass", "rationale": "ok"}')

    res = _run_judge(stub, project_root=tmp_path, events_file=events_file)
    assert res.status == "pass"

    debug_dir = _dump_dir(events_file)
    if debug_dir.exists():
        assert list(debug_dir.iterdir()) == [], list(debug_dir.iterdir())


# --------------------------------------------------------------------------
# Scope §2: judge_call event carries dump path
# --------------------------------------------------------------------------


def test_judge_call_event_carries_dump_path_on_failure(tmp_path):
    """The dump file's path is referenced from the ``judge_call`` event so
    log readers can locate the dump without scanning the debug dir."""
    events_file = tmp_path / "events.jsonl"
    stub = _StubSDK('garbage that is not json at all')

    _run_judge(stub, project_root=tmp_path, events_file=events_file)

    e = _judge_event(events_file)
    assert "judge_response_dump" in e, e
    assert Path(e["judge_response_dump"]).exists()
    assert Path(e["judge_response_dump"]).read_text() == "garbage that is not json at all"


def test_no_dump_path_on_event_when_parse_succeeds(tmp_path):
    """Successful parse → ``judge_response_dump`` field absent (don't bloat
    events.jsonl with empty fields per Scope §2)."""
    events_file = tmp_path / "events.jsonl"
    stub = _StubSDK('{"status": "fail", "rationale": "no foo.py"}')

    _run_judge(stub, project_root=tmp_path, events_file=events_file)

    e = _judge_event(events_file)
    assert "judge_response_dump" not in e, e


# --------------------------------------------------------------------------
# Scope §3: parse_error categorization
# --------------------------------------------------------------------------


# Each row: (label, response_text, expected_category).
_PARSE_ERROR_CASES = [
    (
        "no_json_object_empty",
        "",
        "no_json_object",
    ),
    (
        "no_json_object_prose_only",
        "I cannot evaluate this bullet.",
        "no_json_object",
    ),
    (
        "trailing_prose_after_json",
        '{"status": "pass", "rationale": "ok"}\nHope this helps!',
        "trailing_prose_after_json",
    ),
    (
        "unescaped_quote_in_string",
        '{"status": "pass", "rationale": "the file has "quoted" text"}',
        "unescaped_in_string",
    ),
    (
        "json_truncated_midstring",
        '{"status": "pass", "rationale": "the file exists at',
        "json_truncated",
    ),
]


@pytest.mark.parametrize(
    "label, response, expected",
    _PARSE_ERROR_CASES,
    ids=[c[0] for c in _PARSE_ERROR_CASES],
)
def test_categorize_parse_error_pure(label, response, expected):
    """Pure-function check on the categorization helper — independent of
    the SDK-loop wiring so the heuristic table is regression-pinned."""
    assert _categorize_parse_error(response) == expected
    assert expected in PARSE_ERROR_CATEGORIES


@pytest.mark.parametrize(
    "label, response, expected",
    _PARSE_ERROR_CASES,
    ids=[c[0] for c in _PARSE_ERROR_CASES],
)
def test_parse_error_categorized(tmp_path, label, response, expected):
    """End-to-end: each malformed shape lands the right category on the
    ``judge_call`` event."""
    events_file = tmp_path / "events.jsonl"
    stub = _StubSDK(response)

    _run_judge(stub, project_root=tmp_path, events_file=events_file)

    e = _judge_event(events_file)
    assert e.get("parse_error") == expected, e


def test_parse_error_other_catchall_in_categories():
    """The catch-all enum value exists so any future failure shape that
    doesn't match the four specific heuristics still classifies cleanly."""
    assert "parse_error_other" in PARSE_ERROR_CATEGORIES


# --------------------------------------------------------------------------
# Scope §4: length signals on every judge_call
# --------------------------------------------------------------------------


def test_response_length_recorded_on_all_calls(tmp_path):
    """Both success and failure paths populate ``response_length``."""
    # Success path
    events_file_ok = tmp_path / "events_ok.jsonl"
    good_text = '{"status": "pass", "rationale": "ok"}'
    _run_judge(
        _StubSDK(good_text),
        project_root=tmp_path,
        events_file=events_file_ok,
    )
    e_ok = _judge_event(events_file_ok)
    assert e_ok.get("response_length") == len(good_text)
    # Successful parse must also carry rationale_length (Scope §4).
    assert e_ok.get("rationale_length") == len("ok")

    # Failure path
    events_file_bad = tmp_path / "events_bad.jsonl"
    bad_text = "not json"
    _run_judge(
        _StubSDK(bad_text),
        project_root=tmp_path,
        events_file=events_file_bad,
    )
    e_bad = _judge_event(events_file_bad)
    assert e_bad.get("response_length") == len(bad_text)
    # Failure path does NOT carry rationale_length — there's no
    # rationale to measure.
    assert "rationale_length" not in e_bad, e_bad


# --------------------------------------------------------------------------
# Scope §1: strict-JSON prompt-tightening constraints
# --------------------------------------------------------------------------


def test_strict_prompt_includes_rationale_length_constraint(tmp_path):
    """The system/user prompt sent to the judge must spell out both the
    ≤200 character rationale cap (prevention against escape bugs and cost)
    AND the 'JSON object only' final-message constraint (prevents prose-
    wrapped responses). Both literal strings are pinned so a future
    prompt rewrite doesn't silently drop the constraints."""
    events_file = tmp_path / "events.jsonl"
    stub = _StubSDK('{"status": "pass", "rationale": "ok"}')

    _run_judge(stub, project_root=tmp_path, events_file=events_file)

    prompt = stub.captured_prompt or ""
    assert "200 characters" in prompt, prompt
    assert "JSON object only" in prompt, prompt
    # Example JSON shape is also part of the contract (Scope §1).
    assert '{"status": "pass", "rationale": "X exists per L42"}' in prompt


# --------------------------------------------------------------------------
# Cross-cutting: failure path still emits a judge_call event
# (regression pin — the dump-write must not short-circuit the event)
# --------------------------------------------------------------------------


def test_judge_call_event_still_emits_on_parse_failure(tmp_path):
    """A parse failure dumps the response AND emits the ``judge_call``
    event — the dump is additive, not a replacement."""
    events_file = tmp_path / "events.jsonl"
    stub = _StubSDK("no json here")

    _run_judge(stub, project_root=tmp_path, events_file=events_file)

    e = _judge_event(events_file)
    assert e["verdict"] == "unverified"
    assert e["task"] == "TB-236"
    assert e["bullet_idx"] == 0
    # Length signal present on the failure path too.
    assert e.get("response_length") == len("no json here")


def test_dump_filename_shape(tmp_path):
    """Dump filename follows the documented convention:
    ``<run_ts>-<task>-judge-bullet<idx>-response.txt``. Operators grep
    the debug dir on this convention; a drift would silently break
    diagnostic workflow."""
    events_file = tmp_path / "events.jsonl"
    stub = _StubSDK("not json")

    _run_judge(
        stub,
        project_root=tmp_path,
        events_file=events_file,
        task_id="TB-999",
        bullet_idx=7,
    )

    dumps = list(_dump_dir(events_file).glob("*"))
    assert len(dumps) == 1, dumps
    name = dumps[0].name
    assert name.endswith("-TB-999-judge-bullet7-response.txt"), name
    # Run-ts prefix is a UTC-millisecond-free compact timestamp shaped
    # like 20260515T191234Z (matches `_prep_debug_dumps` convention).
    ts_part = name.split("-TB-999-")[0]
    assert len(ts_part) == 16, ts_part  # YYYYMMDDTHHMMSSZ
    assert ts_part.endswith("Z"), ts_part

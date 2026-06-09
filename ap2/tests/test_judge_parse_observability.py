"""TB-236 + TB-385: prose-judge parse-failure observability + prompt-tightening.

The prose judge (``ap2.verify._judge_prose_bullet``, relocated to the
``verifier_judge`` component) turns a free-form acceptance bullet into a
pass/fail verdict via an SDK call against the cumulative task diff + working
tree. When the SDK's final message can't be parsed into a
``{"status": ..., "rationale": ...}`` envelope, the verifier records the
bullet as ``unverified`` (soft-pass) and — for diagnosis — dumps the FULL
raw last-assistant-text to
``.cc-autopilot/debug/<run_ts>-<task>-judge-bullet<idx>-response.txt``.

TB-236 originally ALSO rode the parse-failure categorization
(``parse_error``) and length signals (``response_length`` /
``rationale_length``) on a per-bullet ``judge_call`` event. **TB-385
removed that event**: the per-task verifier's per-bullet ``judge_call``
events were folded into the daemon's single terminal ``task_verify`` event
(the per-bullet verdict lives in ``task_verify.bullets[]``), so the judge
no longer streams a ``judge_call`` per bullet. What survives unchanged is:

  - **Prevention**: the prompt caps the rationale at 200 characters and is
    explicit that the FINAL message must be JSON-only (no markdown fences,
    no preamble, no trailing prose).
  - **Observability — dump**: on parse failure, the FULL raw last-
    assistant-text is written to the per-bullet debug file. Successful
    parses leave nothing on disk.
  - **Categorization (pure function)**: ``_categorize_parse_error`` still
    classifies a malformed response into one of ``PARSE_ERROR_CATEGORIES``;
    it's exercised directly here (the event that used to carry it is gone).

Tests below pin those surviving behaviors PLUS the TB-385 contract that NO
``judge_call`` event is emitted by the prose judge anymore.
"""
from __future__ import annotations

import asyncio
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
    ``with_result=True``) a ResultMessage carrying usage / cost. The text is
    whatever the caller wires up — well-formed JSON for the success cases,
    the various malformed shapes for the failure cases.
    """

    def __init__(self, text: str, *, with_result: bool = True) -> None:
        self.text = text
        self.with_result = with_result
        # Capture the prompt so tests can assert on prompt content without
        # monkeypatching the SDK shape (e.g. the rationale-length test).
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


def _judge_call_rows(events_file: Path) -> list[dict]:
    """TB-385: the prose judge no longer emits `judge_call` — this should
    always be empty. Helper kept so the regression pins read clearly."""
    rows = events.tail(events_file, n=10)
    return [r for r in rows if r.get("type") == "judge_call"]


def _dump_dir(events_file: Path) -> Path:
    return events_file.parent / "debug"


# --------------------------------------------------------------------------
# TB-385: the prose judge emits NO `judge_call` event (folded into task_verify)
# --------------------------------------------------------------------------


def test_no_judge_call_event_emitted_on_success(tmp_path):
    """A successful prose-judge call emits NO `judge_call` event — the
    per-bullet verdict is folded into the daemon's terminal `task_verify`
    event instead (TB-385)."""
    events_file = tmp_path / "events.jsonl"
    stub = _StubSDK('{"status": "pass", "rationale": "ok"}')

    res = _run_judge(stub, project_root=tmp_path, events_file=events_file)
    assert res.status == "pass"
    assert _judge_call_rows(events_file) == []


def test_no_judge_call_event_emitted_on_parse_failure(tmp_path):
    """A parse failure still records the bullet as `unverified` and writes
    the diagnostic dump, but emits NO `judge_call` event (TB-385)."""
    events_file = tmp_path / "events.jsonl"
    stub = _StubSDK("no json here")

    res = _run_judge(stub, project_root=tmp_path, events_file=events_file)
    assert res.status == "unverified"
    assert _judge_call_rows(events_file) == []
    # The on-disk dump is the surviving diagnostic surface.
    dumps = list(_dump_dir(events_file).glob("*-judge-bullet*-response.txt"))
    assert len(dumps) == 1, dumps


# --------------------------------------------------------------------------
# Dump file on parse failure (TB-236 — surface retained under TB-385)
# --------------------------------------------------------------------------


def test_response_dumped_on_parse_failure(tmp_path):
    """Malformed-JSON response → debug file with FULL raw last-message text.

    The verifier's ``notes`` truncate to 200 chars, which made it impossible
    to tell whether the failure was an unescaped quote, trailing prose,
    truncation, or something else. The dump captures the full text so the
    operator can diagnose.
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


# --------------------------------------------------------------------------
# parse_error categorization (pure function — the surviving categorization
# surface now that the `judge_call` event is gone)
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
def test_malformed_response_dumps_with_no_judge_call(tmp_path, label, response, expected):
    """End-to-end: each `parse_error`-classified shape writes a diagnostic
    dump capturing the FULL raw response, and emits NO `judge_call` event
    (TB-385). The category itself is asserted by the pure-function test
    above — TB-385 dropped the per-bullet event that used to carry it, so
    the on-disk dump is the operator's diagnostic surface.

    Note `trailing_prose_after_json` parses to a usable `pass` verdict
    (the trailing commentary is recoverable) yet still flags a
    `parse_error`, so a dump is written; the other shapes land the bullet
    `unverified`. The dump invariant holds across all of them, so it's the
    shared assertion here."""
    events_file = tmp_path / "events.jsonl"
    stub = _StubSDK(response)

    _run_judge(stub, project_root=tmp_path, events_file=events_file)
    assert _judge_call_rows(events_file) == []
    # Every `parse_error`-classified shape (including the empty response,
    # which dumps an empty file) writes exactly one dump carrying the FULL
    # raw response text.
    dumps = list(_dump_dir(events_file).glob("*-judge-bullet*-response.txt"))
    assert len(dumps) == 1, (dumps, expected)
    assert dumps[0].read_text() == response


def test_parse_error_other_catchall_in_categories():
    """The catch-all enum value exists so any future failure shape that
    doesn't match the four specific heuristics still classifies cleanly."""
    assert "parse_error_other" in PARSE_ERROR_CATEGORIES


# --------------------------------------------------------------------------
# Scope §1: strict-JSON prompt-tightening constraints
# --------------------------------------------------------------------------


def test_strict_prompt_includes_rationale_length_constraint(tmp_path):
    """The prompt sent to the judge must spell out both the ≤200 character
    rationale cap (prevention against escape bugs and cost) AND the 'JSON
    object only' final-message constraint (prevents prose-wrapped responses).
    Both literal strings are pinned so a future prompt rewrite doesn't
    silently drop the constraints."""
    events_file = tmp_path / "events.jsonl"
    stub = _StubSDK('{"status": "pass", "rationale": "ok"}')

    _run_judge(stub, project_root=tmp_path, events_file=events_file)

    prompt = stub.captured_prompt or ""
    assert "200 characters" in prompt, prompt
    assert "JSON object only" in prompt, prompt
    # Example JSON shape is also part of the contract (Scope §1).
    assert '{"status": "pass", "rationale": "X exists per L42"}' in prompt

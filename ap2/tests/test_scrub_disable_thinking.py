"""TB-294: regression pins for the ideation_state scrub thinking-disabled fix
and the typed-exception / audit-event surface around it.

Why this module exists separately from
``test_scrub_exhaustion_language.py`` (TB-284): the TB-284 module pins
the post-write-filter design intent (what the scrub deletes, the
fail-safe-by-preserving-the-file behavior on the integration layer,
the model-env knob). This module pins the TB-294 fixes:

  * ``thinking={"type": "disabled"}`` is wired into the
    ``ClaudeAgentOptions(...)`` call inside ``_run_scrub``.
    Production root-cause was Haiku 4.5's adaptive-thinking
    auto-engagement on the per-sentence classification prompt,
    producing a 22439-character internal reasoning trace that pushed
    total latency past the 60s ``_SCRUB_TIMEOUT_S`` budget on every
    cycle. A future SDK-options refactor that drops the kwarg trips
    this test, not a production cycle.

  * ``scrub_exhaustion_language`` raises typed exceptions on the
    three failure modes (``ScrubTimeoutError`` / ``ScrubSDKError`` /
    ``ScrubEmptyOutputError``) so the caller can distinguish them and
    emit the ``ideation_state_scrub_error`` audit event with an
    accurate ``reason`` field. Pre-TB-294 the function swallowed
    every failure silently and returned the input unchanged — broken
    scrub looked identical to a clean-input no-op on the events
    stream.

  * ``_maybe_scrub_ideation_state`` catches each typed exception and
    emits ``ideation_state_scrub_error`` with the matching
    ``reason`` (``timeout`` / ``sdk_error`` / ``empty_output``) +
    ``duration_s`` + ``error`` payload fields. The original
    ``ideation_state.md`` content is NOT overwritten in any
    exception path (TB-284 file-layer fail-safe preserved).

  * On a successful no-op (input == scrubbed output), NO audit event
    fires — the steady-state happy path stays silent so the events
    stream isn't drowned in scrub noise.
"""
from __future__ import annotations

import inspect
import types
from pathlib import Path

import pytest

from ap2 import events as events_module
from ap2 import ideation, ideation_scrub
from ap2.config import Config
from ap2.ideation_scrub import (
    ScrubEmptyOutputError,
    ScrubError,
    ScrubSDKError,
    ScrubTimeoutError,
    scrub_exhaustion_language,
)


# ---------------------------------------------------------------------------
# Fake-SDK fixtures. Mirror the shape used in
# ``test_scrub_exhaustion_language.py`` so both modules exercise the same
# seam — recording stub on ``ClaudeAgentOptions``, async-generator stub on
# ``query`` with optional configurable failure modes.


def _make_fake_sdk(response: str, *, calls: list | None = None):
    """Recording fake SDK: captures every ``ClaudeAgentOptions(...)`` call.

    ``calls`` (optional) is appended-to on each invocation with
    ``{"prompt": ..., "options": ...}`` so a test can inspect the
    kwargs the production code passed.
    """
    call_log: list = calls if calls is not None else []

    class _Options:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    async def _query(*, prompt, options):
        call_log.append({"prompt": prompt, "options": options})

        class _Part:
            text = response

        class _Msg:
            content = [_Part()]

        yield _Msg()

    return types.SimpleNamespace(ClaudeAgentOptions=_Options, query=_query)


def _make_failing_sdk(exc: Exception):
    """Fake SDK whose ``query`` raises ``exc`` on first iteration.

    Used to exercise the typed-exception path in
    ``scrub_exhaustion_language``: any non-timeout exception raised
    inside the SDK call is re-raised as ``ScrubSDKError``.
    """

    class _Options:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    async def _query(*, prompt, options):
        raise exc
        yield  # pragma: no cover — unreachable; marks _query as a generator

    return types.SimpleNamespace(ClaudeAgentOptions=_Options, query=_query)


def _make_cfg(tmp_path: Path) -> Config:
    """Minimal `Config` rooted at `tmp_path` with `.cc-autopilot/` ensured."""
    (tmp_path / "TASKS.md").write_text(
        "# Tasks\n\n## Active\n\n## Ready\n\n## Backlog\n\n## Complete\n\n## Frozen\n"
    )
    (tmp_path / "CLAUDE.md").write_text(
        "## Autopilot\n\n- Task list: `TASKS.md`\n- Next task ID: TB-100\n"
    )
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()
    return cfg


# ---------------------------------------------------------------------------
# (1) `_run_scrub` passes `thinking={"type": "disabled"}` into
# `ClaudeAgentOptions`.


def test_run_scrub_passes_thinking_disabled_into_options():
    """Recording-stub assertion: ``thinking={"type": "disabled"}`` is wired.

    The fake SDK's ``ClaudeAgentOptions`` records every kwarg it was
    constructed with; the assertion pins the exact dict shape so a
    refactor that passes ``thinking="disabled"`` (string form) or
    forgets the kwarg entirely trips this test.
    """
    calls: list = []
    sdk = _make_fake_sdk("scrubbed output\n", calls=calls)
    scrub_exhaustion_language("some markdown\n", sdk=sdk)
    assert len(calls) == 1
    opts = calls[0]["options"]
    assert "thinking" in opts.kwargs, (
        "`thinking` kwarg missing from ClaudeAgentOptions — TB-294 fix "
        "regressed. Re-add `thinking={\"type\": \"disabled\"}` to the "
        "options dict inside `_run_scrub._ask`."
    )
    assert opts.kwargs["thinking"] == {"type": "disabled"}, (
        "Wrong `thinking` shape — TB-294 requires the canonical SDK "
        "config-object form `{\"type\": \"disabled\"}`."
    )


def test_run_scrub_source_mentions_thinking_disabled():
    """Source-level pin: ``thinking`` + ``disabled`` literal substrings present.

    Defense-in-depth against a refactor that routes the kwarg through
    an intermediate dict or factory function: the briefing's
    verification bullet
    (``inspect.getsource(_run_scrub)`` containing both strings)
    is pinned here so a refactor that breaks the bullet trips a unit
    test first.
    """
    src = inspect.getsource(ideation_scrub._run_scrub)
    assert "thinking" in src, (
        "`thinking` substring missing from `_run_scrub` source — TB-294 fix "
        "regressed."
    )
    assert "disabled" in src, (
        "`disabled` substring missing from `_run_scrub` source — TB-294 fix "
        "regressed."
    )


# ---------------------------------------------------------------------------
# (2) Typed exceptions on each failure mode.


def test_scrub_raises_timeout_error_on_inner_timeout(monkeypatch):
    """``TimeoutError`` from inside ``_run_scrub`` → ``ScrubTimeoutError``.

    Pre-TB-294 the timeout was swallowed and the input was returned
    unchanged silently. The new contract surfaces a typed exception
    so ``_maybe_scrub_ideation_state`` can emit the
    ``ideation_state_scrub_error reason=timeout`` audit event.
    """
    def _boom(*, sdk, prompt, model):
        raise TimeoutError("scrub worker exceeded 65s")
    monkeypatch.setattr(ideation_scrub, "_run_scrub", _boom)

    with pytest.raises(ScrubTimeoutError) as excinfo:
        scrub_exhaustion_language("some text\n", sdk=object())
    # The original TimeoutError message is preserved through the
    # wrapped exception so the audit event's `error` field can carry
    # the worker-grace message verbatim for operator triage.
    assert "65s" in str(excinfo.value)


def test_scrub_raises_sdk_error_on_generic_exception():
    """Any non-timeout exception → ``ScrubSDKError`` wrapping the type name.

    The wrapped message carries ``<ExceptionType>: <message>`` so the
    audit event's ``error`` payload field discriminates between
    e.g. ``ConnectionError`` and ``ValueError`` at operator-triage time.
    """
    sdk = _make_failing_sdk(ConnectionError("upstream refused"))
    with pytest.raises(ScrubSDKError) as excinfo:
        scrub_exhaustion_language("some text\n", sdk=sdk)
    assert "ConnectionError" in str(excinfo.value)
    assert "upstream refused" in str(excinfo.value)


def test_scrub_raises_empty_output_error_on_blank_sdk_response():
    """SDK returned whitespace-only output → ``ScrubEmptyOutputError``.

    The caller layer preserves the original file on disk; the typed
    exception lets the audit event fire with ``reason=empty_output``.
    """
    sdk = _make_fake_sdk("   \n\n   ")
    with pytest.raises(ScrubEmptyOutputError):
        scrub_exhaustion_language("some text\n", sdk=sdk)


def test_scrub_error_hierarchy_is_subclass_of_scrub_error():
    """All three typed scrub exceptions inherit from ``ScrubError``.

    Lets future code catch the whole family via the base class
    without enumerating each subclass.
    """
    assert issubclass(ScrubTimeoutError, ScrubError)
    assert issubclass(ScrubSDKError, ScrubError)
    assert issubclass(ScrubEmptyOutputError, ScrubError)


# ---------------------------------------------------------------------------
# (3) `_maybe_scrub_ideation_state` catches each typed exception and emits
# `ideation_state_scrub_error` with the right `reason` field. The original
# `ideation_state.md` content is NOT overwritten in any exception path.


def _stub_scrub_raises(monkeypatch, exc: Exception) -> None:
    """Replace ``ideation_scrub.scrub_exhaustion_language`` to raise ``exc``.

    The integration test calls
    ``ideation._maybe_scrub_ideation_state``, which imports
    ``ideation_scrub`` lazily inside the function body — the
    monkeypatch must be on the ``ideation_scrub`` module attribute so
    the lazy import resolves to the stubbed callable.
    """
    def _raises(text, *, sdk, cfg=None):
        raise exc
    monkeypatch.setattr(ideation_scrub, "scrub_exhaustion_language", _raises)


def test_maybe_scrub_emits_timeout_event_on_scrub_timeout_error(tmp_path, monkeypatch):
    """``ScrubTimeoutError`` → ``ideation_state_scrub_error reason=timeout``."""
    cfg = _make_cfg(tmp_path)
    target = cfg.project_root / ".cc-autopilot" / "ideation_state.md"
    original = (
        "## Current focus assessment\n\n"
        "- **Focus A**\n"
        "  - Progress so far: TB-1 landed.\n"
    )
    target.write_text(original)
    _stub_scrub_raises(monkeypatch, ScrubTimeoutError("scrub worker exceeded 65s"))

    ideation._maybe_scrub_ideation_state(cfg, sdk=object())

    # File preserved on disk — fail-safe layer intact.
    assert target.read_text() == original

    err_evt = _last_event_of_type(cfg, "ideation_state_scrub_error")
    assert err_evt["reason"] == "timeout"
    assert "65s" in err_evt["error"]
    assert "duration_s" in err_evt
    assert isinstance(err_evt["duration_s"], (int, float))
    # The success event MUST NOT fire on the error path.
    assert _last_event_of_type_or_none(cfg, "ideation_state_scrubbed") is None


def test_maybe_scrub_emits_sdk_error_event_on_scrub_sdk_error(tmp_path, monkeypatch):
    """``ScrubSDKError`` → ``ideation_state_scrub_error reason=sdk_error``."""
    cfg = _make_cfg(tmp_path)
    target = cfg.project_root / ".cc-autopilot" / "ideation_state.md"
    original = "## Current focus assessment\n\n- **Focus A**\n  - Gaps: x.\n"
    target.write_text(original)
    _stub_scrub_raises(
        monkeypatch,
        ScrubSDKError("ConnectionError: upstream refused"),
    )

    ideation._maybe_scrub_ideation_state(cfg, sdk=object())

    assert target.read_text() == original
    err_evt = _last_event_of_type(cfg, "ideation_state_scrub_error")
    assert err_evt["reason"] == "sdk_error"
    assert "ConnectionError" in err_evt["error"]
    assert "duration_s" in err_evt
    assert _last_event_of_type_or_none(cfg, "ideation_state_scrubbed") is None


def test_maybe_scrub_emits_empty_output_event_on_scrub_empty_output_error(
    tmp_path, monkeypatch,
):
    """``ScrubEmptyOutputError`` → ``ideation_state_scrub_error reason=empty_output``."""
    cfg = _make_cfg(tmp_path)
    target = cfg.project_root / ".cc-autopilot" / "ideation_state.md"
    original = "## Current focus assessment\n\n- **Focus A**\n  - Gaps: x.\n"
    target.write_text(original)
    _stub_scrub_raises(
        monkeypatch,
        ScrubEmptyOutputError("SDK returned empty / whitespace-only output"),
    )

    ideation._maybe_scrub_ideation_state(cfg, sdk=object())

    assert target.read_text() == original
    err_evt = _last_event_of_type(cfg, "ideation_state_scrub_error")
    assert err_evt["reason"] == "empty_output"
    assert "duration_s" in err_evt
    assert _last_event_of_type_or_none(cfg, "ideation_state_scrubbed") is None


# ---------------------------------------------------------------------------
# (4) Steady-state happy-path preservation: on a successful no-op (input ==
# scrubbed output), NO audit event fires — the events stream stays quiet on
# clean cycles.


def test_maybe_scrub_no_error_event_on_clean_input_noop(tmp_path):
    """Clean input → no `ideation_state_scrub_error`, no `ideation_state_scrubbed`.

    Steady-state happy path. The fake SDK returns the input verbatim
    (simulating a well-prompted Haiku that found nothing to delete),
    and neither the success event nor the error event should fire.
    """
    cfg = _make_cfg(tmp_path)
    target = cfg.project_root / ".cc-autopilot" / "ideation_state.md"
    clean = (
        "## Current focus assessment\n\n"
        "- **Focus A**\n"
        "  - Progress so far: TB-1 landed.\n"
        "  - Gaps: TB-3 still pending.\n"
    )
    target.write_text(clean)
    sdk = _make_fake_sdk(clean)

    ideation._maybe_scrub_ideation_state(cfg, sdk)

    assert target.read_text() == clean
    kinds = [e["type"] for e in events_module.tail(cfg.events_file, 20)]
    assert "ideation_state_scrub_error" not in kinds
    assert "ideation_state_scrubbed" not in kinds


def test_maybe_scrub_emits_success_event_on_real_scrub_diff(tmp_path):
    """Real diff path still emits `ideation_state_scrubbed`, not the error event.

    Sanity-check the TB-294 wiring didn't break the TB-284 success
    path: a real scrub diff continues to fire
    ``ideation_state_scrubbed removed_chars=<N>`` with no error event
    leaking in.
    """
    cfg = _make_cfg(tmp_path)
    target = cfg.project_root / ".cc-autopilot" / "ideation_state.md"
    original = (
        "## Current focus assessment\n\n"
        "- **Focus A**\n"
        "  - This focus is essentially done.\n"
        "  - Gaps: none.\n"
    )
    scrubbed = (
        "## Current focus assessment\n\n"
        "- **Focus A**\n"
        "  - Gaps: none.\n"
    )
    target.write_text(original)
    sdk = _make_fake_sdk(scrubbed)

    ideation._maybe_scrub_ideation_state(cfg, sdk)

    assert target.read_text() == scrubbed
    kinds = [e["type"] for e in events_module.tail(cfg.events_file, 20)]
    assert "ideation_state_scrubbed" in kinds
    assert "ideation_state_scrub_error" not in kinds


# ---------------------------------------------------------------------------
# Test helpers.


def _last_event_of_type(cfg: Config, type_: str) -> dict:
    """Return the most-recent event of ``type_`` (raises if none found)."""
    for evt in reversed(events_module.tail(cfg.events_file, 50)):
        if evt.get("type") == type_:
            return evt
    raise AssertionError(f"no event of type {type_!r} in events.jsonl")


def _last_event_of_type_or_none(cfg: Config, type_: str) -> dict | None:
    """Return the most-recent event of ``type_`` or ``None`` if absent."""
    for evt in reversed(events_module.tail(cfg.events_file, 50)):
        if evt.get("type") == type_:
            return evt
    return None

"""TB-284: post-write scrub strips exhaustion language from ideation_state.md.

Pins:

- ``scrub_exhaustion_language(text, *, sdk)`` returns the SDK's
  scrubbed text on success, the input verbatim on a clean file, and
  the input unchanged on any SDK error (fail-safe).
- The integration helper ``ideation._maybe_scrub_ideation_state``
  reads the file the agent just wrote, runs the scrub, overwrites
  only when the scrubbed text differs, and emits
  ``ideation_state_scrubbed removed_chars=<N>`` on a successful
  modification.
- The ``focus_exhausted`` self-skip predicate in
  ``ap2/ideation.py`` is fully gone — the only ``ideation_skipped``
  reason ``_maybe_ideate`` emits today is ``roadmap_complete``;
  there is no longer any code path inside ``_maybe_ideate`` that
  emits ``reason=focus_exhausted``.
- The ``AP2_IDEATION_SCRUB_MODEL`` env knob overrides
  ``DEFAULT_SCRUB_MODEL`` and threads through into the
  ``ClaudeAgentOptions(model=...)`` call.
"""
from __future__ import annotations

import asyncio
import time
import types
from pathlib import Path

import pytest

from ap2 import events as events_module
from ap2 import ideation, ideation_scrub
from ap2.board import Board
from ap2.config import Config
from ap2.cron import save_state
from ap2.ideation_scrub import (
    DEFAULT_SCRUB_MODEL,
    scrub_exhaustion_language,
)


# ---------------------------------------------------------------------------
# Fake-SDK fixtures.


def _make_fake_sdk(response: str, *, calls: list | None = None):
    """Build a module-like fake matching the ``claude_agent_sdk`` shape.

    Exposes ``ClaudeAgentOptions`` (records its kwargs) and ``query``
    (async generator yielding one message whose final ``content[0].text``
    is ``response``). ``calls`` (optional) is appended-to on each
    invocation so a test can inspect prompt + options after the fact.
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
    """Build a fake SDK whose ``query`` raises ``exc`` on first iteration.

    Exercises the fail-safe path — ``scrub_exhaustion_language`` must
    swallow the exception and return the input unchanged.
    """

    class _Options:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    async def _query(*, prompt, options):
        raise exc
        yield  # pragma: no cover — unreachable, marks _query as a generator

    return types.SimpleNamespace(ClaudeAgentOptions=_Options, query=_query)


# ---------------------------------------------------------------------------
# `scrub_exhaustion_language` unit tests.


def test_scrub_with_exhaustion_sentences_returns_sdk_response():
    """Seeded state with exhaustion sentences → scrubbed output drops them.

    The fake SDK returns a pre-canned scrubbed string; the function's
    job is to plumb it back to the caller. The test asserts the
    response is propagated verbatim and that the original verdict
    sentences are absent from it.
    """
    original = (
        "## Current focus assessment\n\n"
        "- **Focus A**\n"
        "  - Progress so far: TB-1 landed, TB-2 landed.\n"
        "  - This focus is essentially done. All gaps are covered.\n"
        "  - Gaps: none remaining.\n"
    )
    scrubbed_response = (
        "## Current focus assessment\n\n"
        "- **Focus A**\n"
        "  - Progress so far: TB-1 landed, TB-2 landed.\n"
        "  - Gaps: none remaining.\n"
    )
    sdk = _make_fake_sdk(scrubbed_response)
    out = scrub_exhaustion_language(original, sdk=sdk)
    assert out == scrubbed_response
    assert "essentially done" not in out
    assert "- Progress so far:" in out  # structure preserved
    assert "- **Focus A**" in out


def test_scrub_clean_state_is_byte_identical_noop():
    """Clean state → scrubbed output is byte-identical (no-op).

    The fake SDK returns the same text it was given (simulating a
    well-prompted Haiku that returns the input verbatim when no
    sentence matches the delete criteria).
    """
    clean = (
        "## Current focus assessment\n\n"
        "- **Focus A**\n"
        "  - Progress so far: TB-1 landed.\n"
        "  - Gaps: TB-3 still pending.\n"
    )
    sdk = _make_fake_sdk(clean)
    out = scrub_exhaustion_language(clean, sdk=sdk)
    assert out == clean


def test_scrub_llm_error_returns_input_unchanged():
    """LLM-error path → returns input unchanged (fail-safe).

    Structure (axis breadcrumbs, proposed-task lists, factual
    observations) is more valuable to keep than verdict sentences are
    to remove on any single cycle.
    """
    original = "## Current focus assessment\n\nany content here."
    sdk = _make_failing_sdk(RuntimeError("network blew up"))
    out = scrub_exhaustion_language(original, sdk=sdk)
    assert out == original


def test_scrub_empty_input_returns_empty_without_calling_sdk():
    """Empty / whitespace-only input returns unchanged with NO SDK call.

    Saves a roundtrip on the first-ever ideation cycle where
    ideation_state.md may not exist or be empty.
    """
    calls: list = []
    sdk = _make_fake_sdk("anything", calls=calls)
    for empty in ("", "   ", "\n\n", "\t\n  \n"):
        out = scrub_exhaustion_language(empty, sdk=sdk)
        assert out == empty
    assert calls == [], "SDK should not have been called on empty input"


def test_scrub_empty_sdk_response_returns_input_unchanged():
    """If the SDK returns an empty / whitespace response, fall back to input.

    A model that returned nothing usable would otherwise zero the
    state file — worst possible outcome. Preserve the original.
    """
    original = "## Current focus assessment\n\n- **Focus A**\n  - Gaps: x.\n"
    sdk = _make_fake_sdk("   \n\n  ")
    out = scrub_exhaustion_language(original, sdk=sdk)
    assert out == original


def test_scrub_threads_default_model_into_options():
    """Default model `claude-haiku-4-5-20251001` flows into ClaudeAgentOptions."""
    calls: list = []
    sdk = _make_fake_sdk("scrubbed", calls=calls)
    scrub_exhaustion_language("some text\n", sdk=sdk)
    assert len(calls) == 1
    opts = calls[0]["options"]
    assert opts.kwargs["model"] == DEFAULT_SCRUB_MODEL


def test_scrub_model_env_knob_overrides_default(monkeypatch):
    """``AP2_IDEATION_SCRUB_MODEL`` overrides the default model.

    Operator override semantics: empty / whitespace knob falls back to
    the module default (matches the parsing style of every other ap2
    model / effort knob).
    """
    monkeypatch.setenv("AP2_IDEATION_SCRUB_MODEL", "claude-some-other-model")
    calls: list = []
    sdk = _make_fake_sdk("scrubbed", calls=calls)
    scrub_exhaustion_language("some text\n", sdk=sdk)
    assert calls[0]["options"].kwargs["model"] == "claude-some-other-model"


def test_scrub_model_env_knob_empty_falls_back_to_default(monkeypatch):
    """An empty / whitespace `AP2_IDEATION_SCRUB_MODEL` falls back to the default."""
    monkeypatch.setenv("AP2_IDEATION_SCRUB_MODEL", "   ")
    calls: list = []
    sdk = _make_fake_sdk("scrubbed", calls=calls)
    scrub_exhaustion_language("some text\n", sdk=sdk)
    assert calls[0]["options"].kwargs["model"] == DEFAULT_SCRUB_MODEL


# ---------------------------------------------------------------------------
# Integration: `ideation._maybe_scrub_ideation_state` end-to-end.


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


def test_maybe_scrub_emits_event_when_scrub_modifies_file(tmp_path):
    """Integration: scrub modifies the file → event fires + file is rewritten.

    Wires the scrub into the post-write path the same way
    ``_run_ideation`` does and asserts on (a) the file's new content
    matches the SDK's response, (b) the
    ``ideation_state_scrubbed`` event fires with
    ``removed_chars > 0``.
    """
    cfg = _make_cfg(tmp_path)
    target = cfg.project_root / ".cc-autopilot" / "ideation_state.md"
    original = (
        "## Current focus assessment\n\n"
        "- **Focus A**\n"
        "  - Progress so far: TB-1 landed.\n"
        "  - This focus is essentially done.\n"
        "  - Gaps: none.\n"
    )
    target.write_text(original)
    scrubbed = (
        "## Current focus assessment\n\n"
        "- **Focus A**\n"
        "  - Progress so far: TB-1 landed.\n"
        "  - Gaps: none.\n"
    )
    sdk = _make_fake_sdk(scrubbed)

    ideation._maybe_scrub_ideation_state(cfg, sdk)

    assert target.read_text() == scrubbed
    kinds = [e["type"] for e in events_module.tail(cfg.events_file, 20)]
    assert "ideation_state_scrubbed" in kinds
    scrub_evt = next(
        e for e in events_module.tail(cfg.events_file, 20)
        if e["type"] == "ideation_state_scrubbed"
    )
    assert scrub_evt["removed_chars"] == len(original) - len(scrubbed)
    assert scrub_evt["removed_chars"] > 0


def test_maybe_scrub_no_event_when_scrub_is_noop(tmp_path):
    """Integration: scrub returns input unchanged → no event, file unchanged."""
    cfg = _make_cfg(tmp_path)
    target = cfg.project_root / ".cc-autopilot" / "ideation_state.md"
    clean = (
        "## Current focus assessment\n\n"
        "- **Focus A**\n"
        "  - Progress so far: TB-1 landed.\n"
        "  - Gaps: TB-3 still pending.\n"
    )
    target.write_text(clean)
    sdk = _make_fake_sdk(clean)  # returns the same text → no diff

    ideation._maybe_scrub_ideation_state(cfg, sdk)

    assert target.read_text() == clean
    kinds = [e["type"] for e in events_module.tail(cfg.events_file, 20)]
    assert "ideation_state_scrubbed" not in kinds


def test_maybe_scrub_no_event_when_sdk_errors(tmp_path):
    """Integration: SDK error → scrub returns input unchanged, no event."""
    cfg = _make_cfg(tmp_path)
    target = cfg.project_root / ".cc-autopilot" / "ideation_state.md"
    original = "## Current focus assessment\n\n- **Focus A**\n  - Gaps: x.\n"
    target.write_text(original)
    sdk = _make_failing_sdk(RuntimeError("model unavailable"))

    ideation._maybe_scrub_ideation_state(cfg, sdk)

    assert target.read_text() == original
    kinds = [e["type"] for e in events_module.tail(cfg.events_file, 20)]
    assert "ideation_state_scrubbed" not in kinds


def test_maybe_scrub_missing_file_is_silent_noop(tmp_path):
    """File missing (first-ever cycle / agent skipped the write) → silent noop."""
    cfg = _make_cfg(tmp_path)
    # No file created.
    sdk = _make_fake_sdk("would never be returned")
    ideation._maybe_scrub_ideation_state(cfg, sdk)  # must not raise

    kinds = [e["type"] for e in events_module.tail(cfg.events_file, 20)]
    assert "ideation_state_scrubbed" not in kinds


def test_maybe_scrub_empty_file_is_silent_noop(tmp_path):
    """Empty / whitespace-only file → silent noop (don't invoke the SDK)."""
    cfg = _make_cfg(tmp_path)
    target = cfg.project_root / ".cc-autopilot" / "ideation_state.md"
    target.write_text("   \n\n")
    calls: list = []
    sdk = _make_fake_sdk("anything", calls=calls)

    ideation._maybe_scrub_ideation_state(cfg, sdk)

    assert calls == [], "SDK should not have been called on an empty file"
    kinds = [e["type"] for e in events_module.tail(cfg.events_file, 20)]
    assert "ideation_state_scrubbed" not in kinds


# ---------------------------------------------------------------------------
# `focus_exhausted` skip predicate is fully gone.


def test_focus_exhausted_predicate_no_longer_in_ideation_source():
    """TB-284: the `focus_exhausted` substring is gone from ``ap2/ideation.py``.

    Direct source-grep asserts the deleted predicate plus its
    ``reason="focus_exhausted"`` event emission can never resurface
    silently — same shape as the briefing's
    ``! grep -q 'reason=focus_exhausted\\|focus_exhausted' ap2/ideation.py``
    verification bullet, but kept as a test so a future refactor that
    re-adds the substring under any name trips a unit-test failure
    before the docs-drift / shell-grep gates see it.
    """
    src = Path(ideation.__file__).read_text()
    assert "focus_exhausted" not in src, (
        "`focus_exhausted` substring resurfaced in ap2/ideation.py — TB-284 "
        "deleted the self-skip predicate and the empty-cycles focus-advance "
        "heuristic (TB-283) is now the authority on exhaustion. Don't re-"
        "introduce."
    )


def test_maybe_ideate_does_not_emit_reason_focus_exhausted(tmp_path, monkeypatch):
    """End-to-end: `_maybe_ideate` no longer reaches a ``focus_exhausted`` skip.

    Seeds an ``ideation_state.md`` with the legacy
    ``Status: exhausted-needs-operator`` value for every focus item,
    runs ``_maybe_ideate`` with the stubbed control agent, and
    asserts the resulting events carry NO
    ``ideation_skipped reason=focus_exhausted`` entry. Pre-TB-284 this
    seed would have tripped the deleted gate.
    """
    # Project skeleton.
    (tmp_path / "TASKS.md").write_text(
        "# Tasks\n\n## Active\n\n## Ready\n\n## Backlog\n\n## Complete\n\n## Frozen\n"
    )
    (tmp_path / "CLAUDE.md").write_text(
        "## Autopilot\n\n- Task list: `TASKS.md`\n- Next task ID: TB-100\n"
    )
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()

    # Project ideation prompt override (avoids depending on the load-bearing
    # default prompt body for this test).
    override = cfg.project_root / ".cc-autopilot" / "ideation_prompt.md"
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text("Test ideation prompt.\n")

    # Seed an ideation_state.md whose every focus item self-reports
    # `exhausted-needs-operator` — the legacy gate condition.
    state = cfg.project_root / ".cc-autopilot" / "ideation_state.md"
    state.write_text(
        "## Current focus assessment\n\n"
        "- **Focus A**\n"
        "  - Progress so far: x\n"
        "  - Gaps: x\n"
        "  - Status: `exhausted-needs-operator`\n"
        "  - Reasoning: x\n\n"
        "- **Focus B**\n"
        "  - Progress so far: x\n"
        "  - Gaps: x\n"
        "  - Status: `exhausted-needs-operator`\n"
        "  - Reasoning: x\n"
    )

    monkeypatch.setenv("AP2_IDEATION_COOLDOWN_S", "0")
    monkeypatch.delenv("AP2_IDEATION_DISABLED", raising=False)
    save_state(cfg.cron_state_file, {"ideation": time.time() - 10000})

    # Stub the SDK call so `_run_ideation` doesn't hit the network.
    async def fake_run_control_agent(
        cfg, sdk, mcp_server, *, label, prompt, allowed_tools, max_turns,
    ):
        return (False, None, "", Path("/tmp/fake-prompt-dump"))

    from ap2 import daemon as _daemon
    monkeypatch.setattr(_daemon, "_run_control_agent", fake_run_control_agent)
    monkeypatch.setattr(_daemon, "_snapshot_state_paths", lambda c: {})
    monkeypatch.setattr(_daemon, "_changed_state_paths", lambda a, b: [])
    monkeypatch.setattr(_daemon, "_commit_state_files", lambda *a, **k: None)

    # Stub the scrub helper so it doesn't try to invoke an SDK during this
    # gate-shape test (the scrub itself is exercised by the integration tests
    # above; this test pins gate behavior).
    monkeypatch.setattr(
        ideation, "_maybe_scrub_ideation_state", lambda cfg, sdk: None
    )

    asyncio.run(ideation._maybe_ideate(cfg, sdk=None, mcp_server=None))

    skips = [
        e for e in events_module.tail(cfg.events_file, 50)
        if e["type"] == "ideation_skipped"
        and "focus_exhausted" in str(e.get("reason", ""))
    ]
    assert skips == [], (
        "TB-284 deleted the `focus_exhausted` skip predicate — "
        "`_maybe_ideate` should NOT emit `ideation_skipped reason="
        "focus_exhausted` regardless of the prior cycle's "
        "ideation_state.md content."
    )
    # The gate is gone → ideation should have actually run (the
    # ideation_empty_board entry-marker fires inside `_run_ideation`).
    kinds = [e["type"] for e in events_module.tail(cfg.events_file, 50)]
    assert "ideation_empty_board" in kinds


# ---------------------------------------------------------------------------
# Defensive: scrub is idempotent — running it twice on the SAME input gives
# the SAME output (the model is the source of idempotency, but the wrapper
# must not introduce any spurious mutation between consecutive calls).


def test_scrub_is_idempotent_when_sdk_returns_input_verbatim():
    """An already-clean file scrubbed twice yields the same byte sequence."""
    clean = (
        "## Current focus assessment\n\n"
        "- **Focus A**\n"
        "  - Progress so far: TB-1 landed.\n"
        "  - Gaps: TB-3 still pending.\n"
    )
    sdk = _make_fake_sdk(clean)
    once = scrub_exhaustion_language(clean, sdk=sdk)
    twice = scrub_exhaustion_language(once, sdk=sdk)
    assert once == clean
    assert twice == clean

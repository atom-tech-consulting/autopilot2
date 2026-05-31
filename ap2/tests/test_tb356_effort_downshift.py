"""TB-356: dynamic effort step-down on the thinking-block-immutability retry.

Pins the graceful-degradation path that runs the first attempt at full
`AP2_AGENT_EFFORT` and steps a task's effort down one tier on the automatic
retry ONLY when the run failed with the bundled-CLI thinking-block-immutability
400 (`... thinking or redacted_thinking blocks in the latest assistant message
cannot be modified`). Other failure classes retry at unchanged effort.

Five cleavages mirror the briefing's test list:
  (a) the classifier (`_is_thinking_block_corruption`) is True on a
      stream/last_messages carrying the 400 text and False on a generic
      failure / a verification-style failure / a bare "cannot be modified".
  (b) the `_step_down_effort` ladder: level 0→xhigh, 1→high, 2→medium,
      3+→low (floored).
  (c) a thinking-block-corruption failure bumps the per-task downshift level
      (and emits `effort_downshift`) while a non-matching failure does not.
  (d) a successful run resets the level (via the extended `reset_attempt`).
  (e) with the kill switch set, dispatch effort stays at base AND a fresh
      thinking-block failure neither bumps the level nor emits the event.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from ap2 import events, retry
from ap2.board import Board
from ap2.config import Config
from ap2.daemon import (
    _is_thinking_block_corruption,
    _resolve_task_effort,
    _step_down_effort,
    run_task,
)

# Representative 400 body text from a real thinking-block-immutability failure
# (TB-353, 2026-05-30). Carries all three discriminators the classifier keys
# on: "cannot be modified" + "thinking"/"redacted_thinking" + "blocks in the
# latest assistant message".
_TB400 = (
    "API Error: 400 messages.1.content.13: thinking or redacted_thinking "
    "blocks in the latest assistant message cannot be modified"
)


# ---------------------------------------------------------------------------
# (b) `_step_down_effort` ladder.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "level,expected",
    [(0, "xhigh"), (1, "high"), (2, "medium"), (3, "low"), (4, "low"), (9, "low")],
)
def test_step_down_effort_ladder_from_xhigh(level, expected):
    assert _step_down_effort("xhigh", level) == expected


def test_step_down_effort_floors_from_lower_base():
    """A non-top base still walks the same ladder and floors at `low`."""
    assert _step_down_effort("high", 0) == "high"
    assert _step_down_effort("high", 1) == "medium"
    assert _step_down_effort("high", 2) == "low"
    assert _step_down_effort("high", 5) == "low"
    assert _step_down_effort("medium", 1) == "low"


def test_step_down_effort_unknown_base_unchanged():
    """A base not on the ladder (`max`, `""`) is returned unchanged — there's
    no known safe step-down path, so we don't guess."""
    assert _step_down_effort("max", 3) == "max"
    assert _step_down_effort("", 2) == ""
    assert _step_down_effort("xhigh", 0) == "xhigh"  # level 0 is a no-op too


# ---------------------------------------------------------------------------
# (a) classifier `_is_thinking_block_corruption`.
# ---------------------------------------------------------------------------


def test_classifier_true_on_stream_carrying_400():
    stream_log = [
        {"seq": 0, "type": "AssistantMessage", "text_preview": "working on it"},
        {"seq": 1, "type": "ResultMessage", "result": _TB400},
    ]
    assert _is_thinking_block_corruption(stream_log) is True


def test_classifier_true_on_error_string():
    assert _is_thinking_block_corruption(_TB400) is True


def test_classifier_true_on_redacted_variant():
    blob = (
        "thinking or redacted_thinking blocks in the latest assistant "
        "message cannot be modified"
    )
    assert _is_thinking_block_corruption(blob) is True


def test_classifier_false_on_generic_task_error():
    # The opaque surface error ap2 stamps for this class does NOT itself carry
    # the signature — only the stream tail does. A run whose stream lacks the
    # signature must not classify.
    assert (
        _is_thinking_block_corruption(
            "Exception: Claude Code returned an error result: success"
        )
        is False
    )
    assert _is_thinking_block_corruption([{"seq": 0, "result": "RuntimeError: boom"}]) is False


def test_classifier_false_on_verification_failure():
    stream_log = [
        {"seq": 0, "result": "FAILED tests/test_x.py::test_y - assert 1 == 2"},
    ]
    assert _is_thinking_block_corruption(stream_log) is False


def test_classifier_narrow_requires_thinking_token():
    # "cannot be modified" alone (e.g. a filesystem-permission message) must
    # NOT match — the classifier is deliberately narrow.
    assert _is_thinking_block_corruption("the file cannot be modified by this user") is False


# ---------------------------------------------------------------------------
# run_task integration fixtures (modeled on test_daemon_recovery).
# ---------------------------------------------------------------------------


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch) -> Config:
    (tmp_path / "TASKS.md").write_text(
        "# Tasks\n\n"
        "## Active\n\n"
        "## Ready\n\n"
        "- [ ] **TB-5** **Victim** `#x` — Will be run. [→ brief](brief.md)\n\n"
        "## Backlog\n\n## Complete\n\n## Frozen\n"
    )
    (tmp_path / "CLAUDE.md").write_text(
        "## Autopilot\n\n- Task list: `TASKS.md`\n- Next task ID: TB-10\n"
    )
    # Strip env that would otherwise leak into effort resolution / verify.
    monkeypatch.delenv("AP2_VERIFY_CMD", raising=False)
    monkeypatch.delenv("AP2_AGENT_EFFORT", raising=False)
    monkeypatch.delenv("AP2_CORE_AGENT_EFFORT", raising=False)
    monkeypatch.delenv("AP2_THINKING_BLOCK_EFFORT_DROP_DISABLED", raising=False)
    monkeypatch.delenv("AP2_CORE_THINKING_BLOCK_EFFORT_DROP_DISABLED", raising=False)
    monkeypatch.setenv("AP2_MAX_RETRIES", "5")  # keep single failures in Backlog
    monkeypatch.setenv("AP2_TASK_TIMEOUT_S", "60")
    cfg_ = Config.load(tmp_path)
    cfg_.ensure_dirs()
    return cfg_


class _FakeMsg:
    def __init__(self, text: str) -> None:
        self.content = [SimpleNamespace(text=text)]


class _FakeToolMsg:
    def __init__(self, name: str, args: dict) -> None:
        self.content = [SimpleNamespace(name=name, input=args, id="tu-1")]


def _make_sdk(behavior):
    class _Options:
        def __init__(self, **kw):
            self.kw = kw

    def _query(prompt, options):  # noqa: ARG001
        return behavior()

    return SimpleNamespace(query=_query, ClaudeAgentOptions=_Options)


def _sdk_yield_then_raise(text: str, exc: BaseException):
    """Yield one message carrying `text` (lands in stream_log), then raise —
    mimics production, where the 400 lives in the stream tail and the surface
    exception is the opaque 'error result: success'."""
    async def gen():
        yield _FakeMsg(text)
        raise exc

    return _make_sdk(gen)


def _sdk_raising(exc: BaseException):
    async def gen():
        if False:  # pragma: no cover - make this an async generator
            yield None
        raise exc

    return _make_sdk(gen)


def _sdk_report(args: dict):
    async def gen():
        yield _FakeToolMsg("report_result", args)

    return _make_sdk(gen)


# ---------------------------------------------------------------------------
# (c) bump-on-thinking-block / no-bump-otherwise.
# ---------------------------------------------------------------------------


def test_thinking_block_failure_bumps_downshift_level(cfg):
    task = Board.load(cfg.tasks_file).get("TB-5")
    sdk = _sdk_yield_then_raise(
        _TB400, RuntimeError("Claude Code returned an error result: success")
    )
    asyncio.run(run_task(cfg, sdk, None, task))

    assert Board.load(cfg.tasks_file).find("TB-5")[0] == "Backlog"
    assert retry.downshift_level(cfg.retry_state_file, "TB-5") == 1

    ds = [
        e for e in events.tail(cfg.events_file, 40)
        if e["type"] == "effort_downshift" and e.get("task") == "TB-5"
    ]
    assert len(ds) == 1, ds
    assert ds[0]["from"] == "xhigh"
    assert ds[0]["to"] == "high"
    assert ds[0]["reason"] == "thinking_block_corruption"
    assert ds[0]["level"] == 1


def test_generic_failure_does_not_bump_downshift_level(cfg):
    task = Board.load(cfg.tasks_file).get("TB-5")
    sdk = _sdk_raising(RuntimeError("boom — unrelated crash"))
    asyncio.run(run_task(cfg, sdk, None, task))

    assert Board.load(cfg.tasks_file).find("TB-5")[0] == "Backlog"
    # Attempt counter bumped (it's a failure) but downshift level untouched.
    assert retry.attempt_count(cfg.retry_state_file, "TB-5") == 1
    assert retry.downshift_level(cfg.retry_state_file, "TB-5") == 0
    assert not any(
        e["type"] == "effort_downshift" for e in events.tail(cfg.events_file, 40)
    )


def test_repeated_thinking_block_failures_walk_effort_down(cfg):
    """Two thinking-block failures walk the resolved dispatch effort
    xhigh → high → medium."""
    assert _resolve_task_effort(cfg, "TB-5") == "xhigh"

    task = Board.load(cfg.tasks_file).get("TB-5")
    sdk = _sdk_yield_then_raise(
        _TB400, RuntimeError("Claude Code returned an error result: success")
    )
    asyncio.run(run_task(cfg, sdk, None, task))
    assert _resolve_task_effort(cfg, "TB-5") == "high"

    from ap2.tools import do_board_edit
    do_board_edit(cfg, {"action": "move_to_ready", "task_id": "TB-5"})
    task2 = Board.load(cfg.tasks_file).get("TB-5")
    asyncio.run(run_task(cfg, sdk, None, task2))
    assert _resolve_task_effort(cfg, "TB-5") == "medium"
    assert retry.downshift_level(cfg.retry_state_file, "TB-5") == 2


# ---------------------------------------------------------------------------
# (d) success resets the level.
# ---------------------------------------------------------------------------


def test_success_resets_downshift_level(cfg):
    retry.bump_downshift(cfg.retry_state_file, "TB-5")
    retry.bump_downshift(cfg.retry_state_file, "TB-5")
    assert retry.downshift_level(cfg.retry_state_file, "TB-5") == 2

    task = Board.load(cfg.tasks_file).get("TB-5")
    sdk = _sdk_report(
        {"status": "complete", "commit": "abc12345", "summary": "did it"}
    )
    asyncio.run(run_task(cfg, sdk, None, task))

    assert Board.load(cfg.tasks_file).find("TB-5")[0] == "Complete"
    assert retry.downshift_level(cfg.retry_state_file, "TB-5") == 0
    assert _resolve_task_effort(cfg, "TB-5") == "xhigh"


def test_reset_attempt_clears_both_counter_and_level(cfg):
    retry.bump_attempt(cfg.retry_state_file, "TB-5")
    retry.bump_downshift(cfg.retry_state_file, "TB-5")
    assert retry.attempt_count(cfg.retry_state_file, "TB-5") == 1
    assert retry.downshift_level(cfg.retry_state_file, "TB-5") == 1

    retry.reset_attempt(cfg.retry_state_file, "TB-5")
    assert retry.attempt_count(cfg.retry_state_file, "TB-5") == 0
    assert retry.downshift_level(cfg.retry_state_file, "TB-5") == 0


# ---------------------------------------------------------------------------
# (e) kill switch.
# ---------------------------------------------------------------------------


def test_kill_switch_keeps_effort_at_base_and_skips_bump(cfg, monkeypatch):
    monkeypatch.setenv("AP2_THINKING_BLOCK_EFFORT_DROP_DISABLED", "1")

    # A pre-existing downshift level (e.g. from before the switch was set) is
    # ignored at dispatch — effort resolves to base.
    retry.bump_downshift(cfg.retry_state_file, "TB-5")
    retry.bump_downshift(cfg.retry_state_file, "TB-5")
    assert _resolve_task_effort(cfg, "TB-5") == "xhigh"

    # A fresh thinking-block failure neither bumps the level nor emits the
    # downshift event while the switch is set.
    task = Board.load(cfg.tasks_file).get("TB-5")
    sdk = _sdk_yield_then_raise(
        _TB400, RuntimeError("Claude Code returned an error result: success")
    )
    asyncio.run(run_task(cfg, sdk, None, task))

    assert retry.downshift_level(cfg.retry_state_file, "TB-5") == 2  # unchanged
    assert not any(
        e["type"] == "effort_downshift" for e in events.tail(cfg.events_file, 40)
    )

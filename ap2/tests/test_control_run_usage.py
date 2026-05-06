"""TB-166: persist control-agent token usage + stream/messages dumps.

Pre-TB-166, `_run_control_agent` (the SDK plumbing for ideation, cron
jobs, MM handler) only dumped the prompt and discarded every SDK
envelope via `async for _ in sdk.query(...): pass`. Token cost,
per-message detail, and the trailing ResultMessage were unrecoverable
post-fact for control-agent runs — even though `run_task` got the same
instrumentation in TB-165.

This file pins:

1. The shared helper now writes `<run_id>.stream.jsonl` +
   `<run_id>.messages.jsonl` (parity with `run_task`) and emits a
   `control_run_usage` event on every terminal path
   (success / timeout / error).
2. The label-specific events (`ideation_timeout`, `ideation_error`,
   `cron_timeout`, etc.) still fire from the caller — `control_run_usage`
   is purely additive.
3. The `run_id` field round-trips to the debug-dump filename prefix so
   an operator can `ls .cc-autopilot/debug/<run_id>.*` after grepping
   the event.
4. All three call sites (ideation, status-report, MM handler) emit the
   event with the appropriate `label`.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from ap2 import events
from ap2.config import Config
from ap2.daemon import _run_control_agent


# ---------- fixtures ----------


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    (tmp_path / "TASKS.md").write_text(
        "# Tasks\n\n## Active\n\n## Ready\n\n## Backlog\n\n"
        "## Pipeline Pending\n\n## Complete\n\n## Frozen\n"
    )
    (tmp_path / "CLAUDE.md").write_text(
        "## Autopilot\n\n- Task list: `TASKS.md`\n- Next task ID: TB-10\n"
    )
    import os
    os.environ["AP2_CONTROL_TIMEOUT_S"] = "60"
    cfg_ = Config.load(tmp_path)
    cfg_.ensure_dirs()
    yield cfg_
    os.environ.pop("AP2_CONTROL_TIMEOUT_S", None)


# ---------- fake SDK ----------


def _result_msg(
    *,
    usage: dict | None = None,
    model_usage: dict | None = None,
    total_cost_usd: float = 0.0,
    num_turns: int = 0,
    model: str = "",
    stop_reason: str = "end_turn",
) -> SimpleNamespace:
    """ResultMessage-shaped envelope. Same shape `_summarize_message`
    expects: usage / model_usage / total_cost_usd / num_turns / model /
    stop_reason directly off the message."""
    return SimpleNamespace(
        content=[],
        usage=usage or {},
        model_usage=model_usage or {},
        total_cost_usd=total_cost_usd,
        num_turns=num_turns,
        model=model,
        stop_reason=stop_reason,
    )


def _make_sdk(behavior):
    class _Options:
        def __init__(self, **kw):
            self.kw = kw

    def _query(prompt, options):  # noqa: ARG001
        return behavior()

    return SimpleNamespace(query=_query, ClaudeAgentOptions=_Options)


def _sdk_yielding_result(
    *,
    usage: dict,
    total_cost_usd: float = 0.05,
    num_turns: int = 1,
    model: str = "claude-opus-4-7",
    model_usage: dict | None = None,
):
    async def gen():
        yield _result_msg(
            usage=usage,
            model_usage=model_usage,
            total_cost_usd=total_cost_usd,
            num_turns=num_turns,
            model=model,
        )

    return _make_sdk(gen)


def _sdk_hanging(sleep_s: float = 5.0):
    async def gen():
        await asyncio.sleep(sleep_s)
        yield _result_msg()

    return _make_sdk(gen)


def _sdk_raising(exc: Exception):
    async def gen():
        if False:  # make it a generator
            yield None
        raise exc

    return _make_sdk(gen)


# ---------- direct helper-level tests ----------


def test_control_run_usage_event_emitted_on_successful_complete(cfg):
    """A clean successful control-agent run emits exactly one
    `control_run_usage` event with non-zero token / cost fields drawn
    from the trailing ResultMessage. Pre-TB-166 the helper discarded
    the SDK stream (`async for _ in sdk.query(...): pass`) so token
    cost was unrecoverable."""
    usage = {
        "input_tokens": 12,
        "output_tokens": 34,
        "cache_creation_input_tokens": 56,
        "cache_read_input_tokens": 78,
    }
    sdk = _sdk_yielding_result(
        usage=usage,
        total_cost_usd=0.0987,
        num_turns=3,
        model="claude-opus-4-7",
        model_usage={"claude-opus-4-7": dict(usage)},
    )
    timed_out, error, stderr_tail, prompt_dump = asyncio.run(
        _run_control_agent(
            cfg, sdk, mcp_server=None,
            label="ideation",
            prompt="hello",
            allowed_tools=[],
            max_turns=1,
        )
    )
    assert timed_out is False
    assert error is None
    assert stderr_tail == ""
    assert prompt_dump.exists()

    evts = events.tail(cfg.events_file, 20)
    runs = [e for e in evts if e["type"] == "control_run_usage"]
    assert len(runs) == 1, runs
    e = runs[0]
    assert e["label"] == "ideation"
    assert e["status"] == "complete"
    assert e["usage"]["input_tokens"] == 12
    assert e["usage"]["output_tokens"] == 34
    assert e["usage"]["cache_creation_input_tokens"] == 56
    assert e["usage"]["cache_read_input_tokens"] == 78
    assert e["total_cost_usd"] == pytest.approx(0.0987)
    assert e["num_turns"] == 3
    assert e["model"] == "claude-opus-4-7"
    assert e["model_usage"]["claude-opus-4-7"]["input_tokens"] == 12
    # `note=stream_incomplete` is reserved for crash paths.
    assert "note" not in e
    # `error` / `stderr_tail` are reserved for non-success paths.
    assert "error" not in e
    assert "stderr_tail" not in e


def test_control_run_usage_keeps_dumps_on_success(cfg):
    """TB-166 retention pin: after `_run_control_agent` returns
    successfully, the per-run prompt.md / stream.jsonl /
    messages.jsonl all live on disk so cross-run cost analysis covers
    successful control-agent runs. Pre-TB-166 only the prompt was
    written; stream + messages didn't exist at all."""
    sdk = _sdk_yielding_result(
        usage={"input_tokens": 1, "output_tokens": 1,
               "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
        total_cost_usd=0.001,
    )
    asyncio.run(
        _run_control_agent(
            cfg, sdk, mcp_server=None,
            label="ideation",
            prompt="hi",
            allowed_tools=[],
            max_turns=1,
        )
    )

    debug_dir = cfg.project_root / ".cc-autopilot" / "debug"
    prompt_dumps = list(debug_dir.glob("*ideation.prompt.md"))
    stream_dumps = list(debug_dir.glob("*ideation.stream.jsonl"))
    messages_dumps = list(debug_dir.glob("*ideation.messages.jsonl"))
    assert len(prompt_dumps) == 1
    assert len(stream_dumps) == 1
    assert len(messages_dumps) == 1
    # The stream file should contain at least the ResultMessage envelope.
    stream_text = stream_dumps[0].read_text()
    assert stream_text.strip(), "stream.jsonl should not be empty"
    assert "usage" in stream_text or "total_cost_usd" in stream_text


def test_control_run_usage_run_id_matches_debug_filename_prefix(cfg):
    """The `control_run_usage.run_id` field equals the
    `<compact_ts>-<label>` filename prefix of the debug dumps. An
    operator can grep `events.jsonl` for the event then `ls` the
    matching debug archive without renaming or stripping suffixes."""
    sdk = _sdk_yielding_result(
        usage={"input_tokens": 5, "output_tokens": 2,
               "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
        total_cost_usd=0.002,
    )
    asyncio.run(
        _run_control_agent(
            cfg, sdk, mcp_server=None,
            label="cron-status-report",
            prompt="ok",
            allowed_tools=[],
            max_turns=1,
        )
    )

    evts = events.tail(cfg.events_file, 20)
    runs = [e for e in evts if e["type"] == "control_run_usage"]
    assert len(runs) == 1
    run_id = runs[0]["run_id"]

    debug_dir = cfg.project_root / ".cc-autopilot" / "debug"
    # Each debug file's name is `<run_id>.<suffix>`.
    prompt_dumps = list(debug_dir.glob(f"{run_id}.prompt.md"))
    stream_dumps = list(debug_dir.glob(f"{run_id}.stream.jsonl"))
    messages_dumps = list(debug_dir.glob(f"{run_id}.messages.jsonl"))
    assert len(prompt_dumps) == 1, (run_id, list(debug_dir.iterdir()))
    assert len(stream_dumps) == 1
    assert len(messages_dumps) == 1
    # Shape pin: <YYYYMMDDTHHMMSSZ>-<label>.
    import re
    assert re.match(r"^\d{8}T\d{6}Z-cron-status-report$", run_id), run_id


def test_control_run_usage_event_on_timeout_uses_stream_incomplete_note(cfg):
    """When the SDK times out before any ResultMessage arrives, the
    event still fires with empty usage and `note=stream_incomplete`.
    Cross-run aggregators reading events.jsonl don't silently drop the
    run."""
    cfg.control_timeout_s = 1
    sdk = _sdk_hanging(sleep_s=5.0)
    timed_out, error, stderr_tail, prompt_dump = asyncio.run(
        _run_control_agent(
            cfg, sdk, mcp_server=None,
            label="ideation",
            prompt="hang",
            allowed_tools=[],
            max_turns=1,
        )
    )
    assert timed_out is True
    assert error is None
    # Return-tuple contract: timeout returns the stderr_tail (may be empty
    # if no stderr lines) and the prompt-dump path.
    assert prompt_dump.exists()

    evts = events.tail(cfg.events_file, 20)
    runs = [e for e in evts if e["type"] == "control_run_usage"]
    assert len(runs) == 1, runs
    e = runs[0]
    assert e["label"] == "ideation"
    assert e["status"] == "timeout"
    assert e["usage"] == {}
    assert e["model_usage"] == {}
    assert e["total_cost_usd"] == 0.0
    assert e["num_turns"] == 0
    assert e["model"] == ""
    assert e.get("note") == "stream_incomplete"

    # All three dump paths must exist on disk even on partial / no-stream
    # runs — operator may need to inspect them post-hoc.
    debug_dir = cfg.project_root / ".cc-autopilot" / "debug"
    assert list(debug_dir.glob("*ideation.prompt.md"))
    assert list(debug_dir.glob("*ideation.stream.jsonl"))
    assert list(debug_dir.glob("*ideation.messages.jsonl"))


def test_control_run_usage_event_on_error_carries_error_field(cfg):
    """When the SDK raises an exception inside `query`, the helper
    emits `control_run_usage` with `status="error"` AND `error=<Type>:
    <msg>`. The dumps still survive on disk for forensic inspection."""
    sdk = _sdk_raising(RuntimeError("boom"))
    timed_out, error, stderr_tail, prompt_dump = asyncio.run(
        _run_control_agent(
            cfg, sdk, mcp_server=None,
            label="ideation",
            prompt="explode",
            allowed_tools=[],
            max_turns=1,
        )
    )
    assert timed_out is False
    assert error == "RuntimeError: boom"
    assert prompt_dump.exists()

    evts = events.tail(cfg.events_file, 20)
    runs = [e for e in evts if e["type"] == "control_run_usage"]
    assert len(runs) == 1, runs
    e = runs[0]
    assert e["label"] == "ideation"
    assert e["status"] == "error"
    assert e["error"] == "RuntimeError: boom"
    assert e.get("note") == "stream_incomplete"

    # Dumps still live on disk.
    debug_dir = cfg.project_root / ".cc-autopilot" / "debug"
    assert list(debug_dir.glob("*ideation.prompt.md"))
    assert list(debug_dir.glob("*ideation.stream.jsonl"))
    assert list(debug_dir.glob("*ideation.messages.jsonl"))


# ---------- call-site coverage ----------


def test_control_run_usage_emitted_via_ideation(cfg, monkeypatch):
    """End-to-end through `_run_ideation`: the event lands with
    `label="ideation"` and the existing `ideation_timeout` /
    `ideation_error` events from the caller are unchanged.

    Also pins the additive-event contract: a *successful* ideation
    run does not emit `ideation_timeout` / `ideation_error` (those
    are caller-side label events that fire only on failure), but
    `control_run_usage` ALWAYS fires."""
    from ap2 import ideation as ideation_mod

    # Disable opt-out so `_run_ideation` proceeds normally.
    monkeypatch.delenv("AP2_IDEATION_DISABLED", raising=False)

    # Stub out the state-commit helpers — we don't care about git plumbing
    # here, only the events.
    from ap2 import daemon as _daemon
    monkeypatch.setattr(_daemon, "_snapshot_state_paths", lambda cfg_: {})
    monkeypatch.setattr(_daemon, "_changed_state_paths", lambda pre, post: [])
    monkeypatch.setattr(_daemon, "_commit_state_files", lambda *a, **kw: None)

    # Stub insights regen — it's pre-flight inside `_run_ideation`.
    from ap2 import insights as insights_mod
    monkeypatch.setattr(
        insights_mod, "maybe_regenerate_index", lambda cfg_: None,
    )

    # Stub the prompt builder so we don't depend on the load-bearing prompt.
    monkeypatch.setattr(
        "ap2.prompts.build_control_prompt",
        lambda cfg_, name, body, **_kw: "stub ideation prompt",
    )

    sdk = _sdk_yielding_result(
        usage={"input_tokens": 100, "output_tokens": 50,
               "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
        total_cost_usd=0.123,
        num_turns=2,
    )
    asyncio.run(ideation_mod._run_ideation(cfg, sdk, mcp_server=None, slots=3))

    evts = events.tail(cfg.events_file, 30)
    runs = [e for e in evts if e["type"] == "control_run_usage"]
    assert len(runs) == 1, runs
    assert runs[0]["label"] == "ideation"
    assert runs[0]["status"] == "complete"
    assert runs[0]["total_cost_usd"] == pytest.approx(0.123)
    assert runs[0]["usage"]["input_tokens"] == 100
    # Successful run — no caller-side failure event fired.
    assert not any(e["type"] == "ideation_timeout" for e in evts)
    assert not any(e["type"] == "ideation_error" for e in evts)


def test_ideation_timeout_emits_both_events(cfg, monkeypatch):
    """Timeout path: `ideation_timeout` (caller's pre-existing label event)
    AND `control_run_usage` (the new additive event) BOTH fire. Asserts
    the additive contract — pre-TB-166 events keep working unchanged."""
    from ap2 import ideation as ideation_mod
    from ap2 import daemon as _daemon
    from ap2 import insights as insights_mod

    monkeypatch.delenv("AP2_IDEATION_DISABLED", raising=False)
    monkeypatch.setattr(_daemon, "_snapshot_state_paths", lambda cfg_: {})
    monkeypatch.setattr(_daemon, "_changed_state_paths", lambda pre, post: [])
    monkeypatch.setattr(_daemon, "_commit_state_files", lambda *a, **kw: None)
    monkeypatch.setattr(insights_mod, "maybe_regenerate_index", lambda cfg_: None)
    monkeypatch.setattr(
        "ap2.prompts.build_control_prompt",
        lambda cfg_, name, body, **_kw: "stub",
    )

    cfg.control_timeout_s = 1
    sdk = _sdk_hanging(sleep_s=5.0)
    asyncio.run(ideation_mod._run_ideation(cfg, sdk, mcp_server=None, slots=3))

    evts = events.tail(cfg.events_file, 30)
    timeouts = [e for e in evts if e["type"] == "ideation_timeout"]
    runs = [e for e in evts if e["type"] == "control_run_usage"]
    assert len(timeouts) == 1, "pre-existing ideation_timeout event must fire"
    assert len(runs) == 1, "new control_run_usage event must fire"
    assert runs[0]["label"] == "ideation"
    assert runs[0]["status"] == "timeout"


def test_ideation_error_emits_both_events(cfg, monkeypatch):
    """Error path additive contract through `_run_ideation`: a control-agent
    run that raises an exception inside `sdk.query` triggers BOTH the new
    `control_run_usage` event (status=error, error=<Type>: <msg>, dumps
    survive on disk for forensic inspection) AND the pre-existing
    `ideation_error` label-specific event from the caller. This pins that
    `control_run_usage` is purely additive — pre-TB-166 events keep firing
    unchanged.

    Helper-level coverage of the error path lives in
    `test_control_run_usage_event_on_error_carries_error_field` above; this
    test pins the call-site interaction so a regression in
    `_run_ideation`'s error-branch ordering surfaces here."""
    from ap2 import ideation as ideation_mod
    from ap2 import daemon as _daemon
    from ap2 import insights as insights_mod

    monkeypatch.delenv("AP2_IDEATION_DISABLED", raising=False)
    monkeypatch.setattr(_daemon, "_snapshot_state_paths", lambda cfg_: {})
    monkeypatch.setattr(_daemon, "_changed_state_paths", lambda pre, post: [])
    monkeypatch.setattr(_daemon, "_commit_state_files", lambda *a, **kw: None)
    monkeypatch.setattr(insights_mod, "maybe_regenerate_index", lambda cfg_: None)
    monkeypatch.setattr(
        "ap2.prompts.build_control_prompt",
        lambda cfg_, name, body, **_kw: "stub",
    )

    sdk = _sdk_raising(RuntimeError("kaboom"))
    asyncio.run(ideation_mod._run_ideation(cfg, sdk, mcp_server=None, slots=3))

    evts = events.tail(cfg.events_file, 30)
    errors = [e for e in evts if e["type"] == "ideation_error"]
    runs = [e for e in evts if e["type"] == "control_run_usage"]
    assert len(errors) == 1, "pre-existing ideation_error event must fire"
    assert errors[0]["error"] == "RuntimeError: kaboom"
    assert len(runs) == 1, "new control_run_usage event must fire"
    assert runs[0]["label"] == "ideation"
    assert runs[0]["status"] == "error"
    assert runs[0]["error"] == "RuntimeError: kaboom"
    assert runs[0].get("note") == "stream_incomplete"

    # Dumps still live on disk after the error path through `_run_ideation`.
    debug_dir = cfg.project_root / ".cc-autopilot" / "debug"
    assert list(debug_dir.glob("*ideation.prompt.md"))
    assert list(debug_dir.glob("*ideation.stream.jsonl"))
    assert list(debug_dir.glob("*ideation.messages.jsonl"))


def test_control_run_usage_emitted_via_status_report(cfg, monkeypatch):
    """End-to-end through `run_status_report`: the event lands with
    `label="cron-status-report"`. Pins call-site coverage for the
    cron path (TB-144 chat-trigger uses the same routine)."""
    from ap2 import status_report as sr_mod

    # Seed activity so the skip-gate doesn't fire.
    events.append(cfg.events_file, "cron_complete", job="status-report")
    events.append(
        cfg.events_file, "task_complete", task="TB-1",
        status="complete", commit="abc1234",
    )

    monkeypatch.setattr(
        "ap2.prompts.build_control_prompt",
        lambda cfg_, name, body, **_kw: "stub status-report prompt",
    )

    sdk = _sdk_yielding_result(
        usage={"input_tokens": 200, "output_tokens": 80,
               "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
        total_cost_usd=0.456,
        num_turns=1,
    )
    asyncio.run(
        sr_mod.run_status_report(
            cfg, sdk, mcp_server=None, trigger="cron",
        )
    )

    evts = events.tail(cfg.events_file, 30)
    runs = [e for e in evts if e["type"] == "control_run_usage"]
    assert len(runs) == 1, runs
    assert runs[0]["label"] == "cron-status-report"
    assert runs[0]["status"] == "complete"
    assert runs[0]["usage"]["input_tokens"] == 200
    assert runs[0]["total_cost_usd"] == pytest.approx(0.456)


def test_control_run_usage_emitted_via_mm_handler(cfg, monkeypatch):
    """MM-handler path: `handle_message` now routes through
    `_run_control_agent`, so `control_run_usage` fires per inbound
    chat message with `label="MM-<post-id>"`. The pre-existing
    `mattermost` invocation event still fires from the caller."""
    from ap2 import daemon as _daemon

    monkeypatch.setattr(
        "ap2.prompts.build_mattermost_prompt",
        lambda cfg_, msg: "stub mm prompt",
    )

    msg = {
        "id": "post-abc-123",
        "channel_name": "ap2",
        "user": "alice",
        "thread_id": "post-abc-123",
        "text": "hi",
    }
    sdk = _sdk_yielding_result(
        usage={"input_tokens": 5, "output_tokens": 7,
               "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
        total_cost_usd=0.01,
    )
    asyncio.run(_daemon.handle_message(cfg, sdk, mcp_server=None, msg=msg))

    evts = events.tail(cfg.events_file, 30)
    runs = [e for e in evts if e["type"] == "control_run_usage"]
    assert len(runs) == 1, runs
    assert runs[0]["label"] == "MM-post-abc-123"
    assert runs[0]["status"] == "complete"
    assert runs[0]["usage"]["input_tokens"] == 5

    # Pre-existing `mattermost` invocation event must still fire.
    assert any(e["type"] == "mattermost" for e in evts)

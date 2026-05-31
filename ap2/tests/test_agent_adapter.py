"""TB-353 / goal.md axis 1 — adapter-contract test for the `AgentAdapter`
ABC and the first implementation, `ClaudeCodeAdapter`.

These tests pin axis 1's deliverable: a backend-agnostic interface plus a
Claude implementation that wraps today's `claude_agent_sdk.query()` path
bit-for-bit. They use a STUBBED SDK (no live SDK call) so the round-trip is
hermetic — `ClaudeCodeAdapter(sdk=<stub>)` injects a fake `claude_agent_sdk`
module exposing `ClaudeAgentOptions` + `query`.

Coverage:
  - `ClaudeCodeAdapter` conforms to `AgentAdapter` (isinstance + abstract
    surface implemented); the ABC itself is non-instantiable.
  - A stubbed `AssistantMessage` / `ResultMessage` stream round-trips to a
    normalized `AgentResult` whose `usage` mirrors `_emit_task_run_usage`'s
    derivation.
  - `run()` yields one normalized `AgentEvent` per SDK envelope plus a
    terminal `type="result"` event.
  - `normalize_options` / `register_tools` map the backend-neutral options /
    tools onto the `ClaudeAgentOptions` kwargs the daemon builds today.
  - The `stream_incomplete` fallback (no `ResultMessage`) and the
    error / timeout paths of `run_to_result`.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from ap2.adapters import (
    AgentAdapter,
    AgentResult,
    AgentRunOptions,
    AgentTools,
    AgentUsage,
    ClaudeCodeAdapter,
)
from ap2.adapters.base import AgentEvent, usage_from_summary


# --------------------------------------------------------------------------
# Stub SDK: a fake `claude_agent_sdk` module exposing `ClaudeAgentOptions`
# + `query`. The message classes are named to mirror the real SDK so
# `AgentEvent.type` (driven by `type(msg).__name__`) reads naturally.
# --------------------------------------------------------------------------


class AssistantMessage:
    def __init__(self, content, model="claude-opus-4-7"):
        self.content = content
        self.model = model


class ResultMessage:
    def __init__(
        self,
        *,
        usage,
        model_usage,
        total_cost_usd,
        num_turns,
        result,
        model,
        stop_reason="end_turn",
    ):
        self.usage = usage
        self.model_usage = model_usage
        self.total_cost_usd = total_cost_usd
        self.num_turns = num_turns
        self.result = result
        self.model = model
        self.stop_reason = stop_reason


def _text_block(text):
    return SimpleNamespace(text=text)


def _tool_use_block(name, inp, id="tu_1"):
    return SimpleNamespace(name=name, input=inp, id=id)


class FakeClaudeOptions:
    """Stand-in for `ClaudeAgentOptions`: captures the kwargs it was built
    with so a test can assert the options-normalization mapping."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs


class FakeSDK:
    """Stand-in for the `claude_agent_sdk` module. `query` replays a
    pre-canned envelope list as an async stream; `ClaudeAgentOptions`
    records its kwargs."""

    def __init__(self, envelopes, *, raise_on=None):
        self._envelopes = envelopes
        self._raise_on = raise_on
        self.captured_prompt = None
        self.captured_options = None

    def ClaudeAgentOptions(self, **kwargs):  # noqa: N802 (mirror SDK name)
        return FakeClaudeOptions(**kwargs)

    def query(self, *, prompt, options):
        self.captured_prompt = prompt
        self.captured_options = options
        return self._stream()

    async def _stream(self):
        for i, env in enumerate(self._envelopes):
            if self._raise_on is not None and i == self._raise_on:
                raise RuntimeError("boom")
            yield env


def _default_envelopes():
    return [
        AssistantMessage([_text_block("working on it")]),
        AssistantMessage([_tool_use_block("Bash", {"command": "ls"})]),
        ResultMessage(
            usage={"input_tokens": 100, "output_tokens": 50},
            model_usage={"claude-opus-4-7": {"input_tokens": 100}},
            total_cost_usd=0.0123,
            num_turns=3,
            result="all done",
            model="claude-opus-4-7",
        ),
    ]


def _options():
    return AgentRunOptions(
        model="claude-opus-4-7",
        effort="xhigh",
        max_turns=80,
        cwd="/tmp/proj",
        permission_mode="bypassPermissions",
        setting_sources=["project"],
    )


def _tools():
    return AgentTools(
        allowed=["Bash", "Read"],
        disallowed=["Edit"],
        mcp_servers={"autopilot": object()},
    )


# --------------------------------------------------------------------------
# Conformance
# --------------------------------------------------------------------------


def test_claude_adapter_conforms_to_abc():
    adapter = ClaudeCodeAdapter(sdk=FakeSDK(_default_envelopes()))
    assert isinstance(adapter, AgentAdapter)
    assert adapter.backend == "claude"
    # The abstract surface is implemented (callables present).
    assert callable(adapter.run)
    assert callable(adapter.normalize_options)
    assert callable(adapter.register_tools)
    assert callable(adapter.run_to_result)


def test_abc_is_not_instantiable():
    with pytest.raises(TypeError):
        AgentAdapter()  # type: ignore[abstract]


# --------------------------------------------------------------------------
# Round-trip: stubbed stream -> normalized AgentResult / usage
# --------------------------------------------------------------------------


def test_run_to_result_round_trips_usage():
    adapter = ClaudeCodeAdapter(sdk=FakeSDK(_default_envelopes()))
    result = asyncio.run(
        adapter.run_to_result("do the thing", _tools(), _options())
    )
    assert isinstance(result, AgentResult)
    assert result.status == "complete"
    # Final assistant text: the ResultMessage's `.result` is the last
    # text-bearing envelope, exactly as the daemon's `_extract_text` walk.
    assert result.text == "all done"

    u = result.usage
    assert isinstance(u, AgentUsage)
    assert u.usage == {"input_tokens": 100, "output_tokens": 50}
    assert u.model_usage == {"claude-opus-4-7": {"input_tokens": 100}}
    assert u.total_cost_usd == 0.0123
    assert u.num_turns == 3
    assert u.model == "claude-opus-4-7"
    assert u.note == ""


def test_run_yields_one_event_per_envelope_plus_terminal():
    adapter = ClaudeCodeAdapter(sdk=FakeSDK(_default_envelopes()))

    async def _collect():
        return [ev async for ev in adapter.run("p", _tools(), _options())]

    events = asyncio.run(_collect())
    # 3 stream envelopes + 1 synthesized terminal "result" event.
    assert [e.type for e in events] == [
        "AssistantMessage",
        "AssistantMessage",
        "ResultMessage",
        "result",
    ]
    # Only the terminal event carries a result.
    assert all(e.result is None for e in events[:-1])
    terminal = events[-1]
    assert isinstance(terminal, AgentEvent)
    assert terminal.result is not None
    assert terminal.result.status == "complete"
    assert terminal.result.usage.total_cost_usd == 0.0123
    # The first stream envelope's normalized summary/text mirror the daemon's
    # message-dump helpers.
    assert events[0].text == "working on it"
    assert events[0].summary["text_preview"] == "working on it"
    # The tool_use envelope surfaces the tool call in its summary.
    assert events[1].summary["tool_calls"][0]["name"] == "Bash"


# --------------------------------------------------------------------------
# Options / tools normalization
# --------------------------------------------------------------------------


def test_normalize_options_maps_claude_agent_option_kwargs():
    adapter = ClaudeCodeAdapter(sdk=FakeSDK(_default_envelopes()))
    kwargs = adapter.normalize_options(_options())
    assert kwargs["model"] == "claude-opus-4-7"
    assert kwargs["extra_args"] == {"effort": "xhigh"}
    assert kwargs["max_turns"] == 80
    assert kwargs["cwd"] == "/tmp/proj"
    assert kwargs["permission_mode"] == "bypassPermissions"
    assert kwargs["setting_sources"] == ["project"]
    # timeout_s is owned by run_to_result's wait_for, NOT mapped onto options.
    assert "timeout_s" not in kwargs


def test_register_tools_maps_tool_policy_and_mcp_servers():
    adapter = ClaudeCodeAdapter(sdk=FakeSDK(_default_envelopes()))
    tools = _tools()
    kwargs = adapter.register_tools(tools)
    assert kwargs["allowed_tools"] == ["Bash", "Read"]
    assert kwargs["disallowed_tools"] == ["Edit"]
    assert kwargs["mcp_servers"] is tools.mcp_servers


def test_run_builds_claude_agent_options_from_merged_kwargs():
    sdk = FakeSDK(_default_envelopes())
    adapter = ClaudeCodeAdapter(sdk=sdk)
    asyncio.run(adapter.run_to_result("the prompt", _tools(), _options()))
    assert sdk.captured_prompt == "the prompt"
    # The options object the adapter built wraps the merged option + tool
    # kwargs (the daemon's `ClaudeAgentOptions(...)` shape).
    opt_kwargs = sdk.captured_options.kwargs
    assert opt_kwargs["model"] == "claude-opus-4-7"
    assert opt_kwargs["extra_args"] == {"effort": "xhigh"}
    assert opt_kwargs["allowed_tools"] == ["Bash", "Read"]
    assert opt_kwargs["disallowed_tools"] == ["Edit"]
    assert "autopilot" in opt_kwargs["mcp_servers"]


# --------------------------------------------------------------------------
# Failure / edge paths
# --------------------------------------------------------------------------


def test_stream_incomplete_when_no_result_message():
    """A stream with no ResultMessage yields the `stream_incomplete`
    sentinel, mirroring `_emit_task_run_usage`'s empty-usage fallback."""
    envelopes = [AssistantMessage([_text_block("partial")])]
    adapter = ClaudeCodeAdapter(sdk=FakeSDK(envelopes))
    result = asyncio.run(adapter.run_to_result("p", _tools(), _options()))
    assert result.status == "complete"
    assert result.usage.note == "stream_incomplete"
    assert result.usage.usage == {}
    assert result.usage.total_cost_usd == 0.0
    assert result.usage.num_turns == 0


def test_run_to_result_error_path():
    """An SDK fault mid-stream becomes a `status="error"` result carrying the
    `<Type>: <msg>` string — the daemon's error vocabulary."""
    adapter = ClaudeCodeAdapter(sdk=FakeSDK(_default_envelopes(), raise_on=1))
    result = asyncio.run(adapter.run_to_result("p", _tools(), _options()))
    assert result.status == "error"
    assert result.error is not None
    assert "RuntimeError" in result.error


def test_run_to_result_timeout_path():
    """When `timeout_s` is exceeded the drain is cancelled and a
    `status="timeout"` result is returned (the daemon's `asyncio.wait_for`
    behavior, now owned by the base adapter)."""

    class SlowSDK(FakeSDK):
        async def _stream(self):
            await asyncio.sleep(10)
            yield AssistantMessage([_text_block("never reached")])

    adapter = ClaudeCodeAdapter(sdk=SlowSDK([]))
    opts = _options()
    opts.timeout_s = 0.05
    result = asyncio.run(adapter.run_to_result("p", _tools(), opts))
    assert result.status == "timeout"
    assert result.usage.note == "stream_incomplete"


# --------------------------------------------------------------------------
# usage_from_summary unit coverage (the shared derivation helper)
# --------------------------------------------------------------------------


def test_usage_from_summary_none_is_stream_incomplete():
    u = usage_from_summary(None)
    assert u.note == "stream_incomplete"
    assert u.usage == {}
    assert u.total_cost_usd == 0.0


def test_usage_from_summary_reads_result_envelope_fields():
    summary = {
        "type": "ResultMessage",
        "usage": {"input_tokens": 7},
        "model_usage": {"m": {"input_tokens": 7}},
        "total_cost_usd": 0.5,
        "num_turns": 9,
        "model": "claude-opus-4-7",
    }
    u = usage_from_summary(summary)
    assert u.usage == {"input_tokens": 7}
    assert u.model_usage == {"m": {"input_tokens": 7}}
    assert u.total_cost_usd == 0.5
    assert u.num_turns == 9
    assert u.model == "claude-opus-4-7"
    assert u.note == ""

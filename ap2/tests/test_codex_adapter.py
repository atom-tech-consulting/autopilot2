"""TB-357 / goal.md axis 4 — adapter-contract test for the second backend,
`CodexAdapter`.

These tests pin axis 4's deliverable: a `CodexAdapter` driving OpenAI's
`codex` CLI agent through the SAME `AgentAdapter` contract `ClaudeCodeAdapter`
satisfies. They use a STUBBED codex handle (no live `codex` process) so the
round-trip is hermetic — `CodexAdapter(codex=<stub>)` injects a fake codex SDK
exposing `CodexOptions` + `run_streamed`.

Coverage (mirrors `test_agent_adapter.py`'s Claude suite so the two backends
are exercised against one shared contract):
  - `CodexAdapter` conforms to `AgentAdapter` (isinstance + abstract surface)
    and reports `backend == "codex"`.
  - A stubbed codex thread-event stream round-trips to a normalized
    `AgentResult` whose `usage` carries input/output/cache tokens, cost, turns,
    and model — one shape, no per-backend branching.
  - `run()` yields one normalized `AgentEvent` per codex envelope plus a
    terminal `type="result"` event.
  - `normalize_options` / `register_tools` map the backend-neutral options /
    tools onto the codex CLI's native kwargs.
  - The `stream_incomplete` fallback (no usage-bearing envelope) and the
    error / timeout paths of the base `run_to_result`.
  - The axis-3 tool-registration surface: ap2's real custom tool set, handed to
    `build_tool_server`, is enumerable via `registered_tool_names()` — the same
    set the Claude adapter exposes (axis-7 parity prep).
"""
from __future__ import annotations

import asyncio

import pytest

from ap2.adapters import (
    AgentAdapter,
    AgentOptions,
    AgentResult,
    AgentRunOptions,
    AgentTools,
    AgentUsage,
    CodexAdapter,
)
from ap2.adapters.base import AgentEvent


# --------------------------------------------------------------------------
# Stub codex handle: a fake codex SDK exposing `CodexOptions` + `run_streamed`.
# Envelopes mirror the `codex exec --json` thread-event shape (dict objects
# with a `type`, `item.*` payloads, and a terminal `turn.completed` carrying
# `usage`).
# --------------------------------------------------------------------------


class FakeCodexOptions:
    """Stand-in for the codex native options object: captures the kwargs it was
    built with so a test can assert the options-normalization mapping."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs


class FakeCodex:
    """Stand-in for the codex SDK handle. `run_streamed` replays a pre-canned
    envelope list as an async stream; `CodexOptions` records its kwargs."""

    def __init__(self, envelopes, *, raise_on=None):
        self._envelopes = envelopes
        self._raise_on = raise_on
        self.captured_prompt = None
        self.captured_options = None

    def CodexOptions(self, **kwargs):  # noqa: N802 (mirror SDK name)
        return FakeCodexOptions(**kwargs)

    def run_streamed(self, *, prompt, options):
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
        {"type": "thread.started", "thread_id": "t_1"},
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "working on it"},
        },
        {
            "type": "item.completed",
            "item": {"type": "command_execution", "command": "ls"},
        },
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "all done"},
        },
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 120,
                "cached_input_tokens": 20,
                "output_tokens": 60,
            },
            "total_cost_usd": 0.0456,
            "num_turns": 4,
            "model": "gpt-5-codex",
        },
    ]


def _options():
    return AgentRunOptions(
        model="gpt-5-codex",
        effort="high",
        max_turns=80,
        cwd="/tmp/proj",
        permission_mode="bypassPermissions",
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


def test_codex_adapter_conforms_to_abc():
    adapter = CodexAdapter(codex=FakeCodex(_default_envelopes()))
    assert isinstance(adapter, AgentAdapter)
    assert adapter.backend == "codex"
    # The abstract surface is implemented (callables present).
    assert callable(adapter.run)
    assert callable(adapter.normalize_options)
    assert callable(adapter.register_tools)
    assert callable(adapter.run_to_result)
    # Axis 3: the tool-registration / enumeration surface.
    assert callable(adapter.build_tool_server)
    assert callable(adapter.registered_tool_names)


def test_codex_adapter_constructs_without_a_handle():
    """`CodexAdapter()` with no injected handle constructs without importing
    the codex SDK (the lazy import only fires on `run`), so the daemon /
    contract test can build it freely."""
    adapter = CodexAdapter()
    assert adapter.backend == "codex"
    assert adapter.registered_tool_names() == []


# --------------------------------------------------------------------------
# Round-trip: stubbed codex stream -> normalized AgentResult / usage
# --------------------------------------------------------------------------


def test_run_to_result_round_trips_usage():
    adapter = CodexAdapter(codex=FakeCodex(_default_envelopes()))
    result = asyncio.run(
        adapter.run_to_result("do the thing", _tools(), _options())
    )
    assert isinstance(result, AgentResult)
    assert result.status == "complete"
    # Final assistant text: the last agent_message item's text.
    assert result.text == "all done"

    u = result.usage
    assert isinstance(u, AgentUsage)
    assert u.usage == {
        "input_tokens": 120,
        "output_tokens": 60,
        "cache_read_input_tokens": 20,
    }
    assert u.total_cost_usd == 0.0456
    assert u.num_turns == 4
    assert u.model == "gpt-5-codex"
    assert u.note == ""
    # The normalized token sum the cost guards / `ap2 status` read.
    assert u.combined_tokens == 180


def test_run_yields_one_event_per_envelope_plus_terminal():
    adapter = CodexAdapter(codex=FakeCodex(_default_envelopes()))

    async def _collect():
        return [ev async for ev in adapter.run("p", _tools(), _options())]

    events = asyncio.run(_collect())
    # 6 stream envelopes + 1 synthesized terminal "result" event.
    assert [e.type for e in events] == [
        "thread.started",
        "turn.started",
        "item.completed",
        "item.completed",
        "item.completed",
        "turn.completed",
        "result",
    ]
    # Only the terminal event carries a result.
    assert all(e.result is None for e in events[:-1])
    terminal = events[-1]
    assert isinstance(terminal, AgentEvent)
    assert terminal.result is not None
    assert terminal.result.status == "complete"
    assert terminal.result.usage.total_cost_usd == 0.0456
    # The first agent_message envelope's normalized summary/text.
    assert events[2].text == "working on it"
    assert events[2].summary["text_preview"] == "working on it"
    # The command_execution envelope surfaces the action in its summary.
    assert events[3].summary["tool_calls"][0]["name"] == "command"


# --------------------------------------------------------------------------
# Options / tools normalization
# --------------------------------------------------------------------------


def test_normalize_options_maps_codex_native_kwargs():
    adapter = CodexAdapter(codex=FakeCodex(_default_envelopes()))
    kwargs = adapter.normalize_options(_options())
    assert kwargs["model"] == "gpt-5-codex"
    assert kwargs["reasoning_effort"] == "high"
    assert kwargs["max_turns"] == 80
    assert kwargs["cwd"] == "/tmp/proj"
    assert kwargs["approval_policy"] == "bypassPermissions"
    # timeout_s is owned by run_to_result's wait_for, NOT mapped onto options.
    assert "timeout_s" not in kwargs


def test_normalize_options_threads_extra():
    opts = AgentOptions(model="gpt-5-codex", extra={"sandbox_mode": "workspace-write"})
    kwargs = CodexAdapter().normalize_options(opts)
    assert kwargs["sandbox_mode"] == "workspace-write"


def test_register_tools_maps_tool_policy_and_mcp_servers():
    adapter = CodexAdapter(codex=FakeCodex(_default_envelopes()))
    tools = _tools()
    kwargs = adapter.register_tools(tools)
    assert kwargs["allowed_tools"] == ["Bash", "Read"]
    assert kwargs["disallowed_tools"] == ["Edit"]
    assert kwargs["mcp_servers"] is tools.mcp_servers


def test_run_builds_codex_options_from_merged_kwargs():
    codex = FakeCodex(_default_envelopes())
    adapter = CodexAdapter(codex=codex)
    asyncio.run(adapter.run_to_result("the prompt", _tools(), _options()))
    assert codex.captured_prompt == "the prompt"
    # The options object the adapter built wraps the merged option + tool
    # kwargs (the codex native invocation shape).
    opt_kwargs = codex.captured_options.kwargs
    assert opt_kwargs["model"] == "gpt-5-codex"
    assert opt_kwargs["reasoning_effort"] == "high"
    assert opt_kwargs["allowed_tools"] == ["Bash", "Read"]
    assert opt_kwargs["disallowed_tools"] == ["Edit"]
    assert "autopilot" in opt_kwargs["mcp_servers"]


# --------------------------------------------------------------------------
# Failure / edge paths (base run_to_result, exercised through the codex stream)
# --------------------------------------------------------------------------


def test_stream_incomplete_when_no_usage_envelope():
    """A stream with no terminal `turn.completed` (no usage) yields the
    `stream_incomplete` sentinel, mirroring the Claude path's empty-usage
    fallback."""
    envelopes = [
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "partial"},
        }
    ]
    adapter = CodexAdapter(codex=FakeCodex(envelopes))
    result = asyncio.run(adapter.run_to_result("p", _tools(), _options()))
    assert result.status == "complete"
    assert result.text == "partial"
    assert result.usage.note == "stream_incomplete"
    assert result.usage.usage == {}
    assert result.usage.total_cost_usd == 0.0
    assert result.usage.num_turns == 0


def test_run_to_result_error_path():
    """A codex fault mid-stream becomes a `status="error"` result carrying the
    `<Type>: <msg>` string — the daemon's error vocabulary."""
    adapter = CodexAdapter(codex=FakeCodex(_default_envelopes(), raise_on=2))
    result = asyncio.run(adapter.run_to_result("p", _tools(), _options()))
    assert result.status == "error"
    assert result.error is not None
    assert "RuntimeError" in result.error


def test_run_to_result_timeout_path():
    """When `timeout_s` is exceeded the drain is cancelled and a
    `status="timeout"` result is returned (the base adapter's `asyncio.wait_for`
    behavior, shared across backends)."""

    class SlowCodex(FakeCodex):
        async def _stream(self):
            await asyncio.sleep(10)
            yield {"type": "turn.completed", "usage": {}}

    adapter = CodexAdapter(codex=SlowCodex([]))
    opts = _options()
    opts.timeout_s = 0.05
    result = asyncio.run(adapter.run_to_result("p", _tools(), opts))
    assert result.status == "timeout"
    assert result.usage.note == "stream_incomplete"


# --------------------------------------------------------------------------
# Axis 3 surface: tool-registration / enumeration (axis-7 parity prep)
#
# The same ap2 custom toolset the Claude adapter exposes must register through
# the codex adapter's `build_tool_server` and be enumerable — axis 7 asserts
# both backends register one identical set.
# --------------------------------------------------------------------------


_EXPECTED_AP2_TOOL_SHORT_NAMES = {
    "board_edit",
    "cron_edit",
    "mattermost_reply",
    "mattermost_thread_read",
    "log_event",
    "daemon_control",
    "ideation_state_write",
    "git_log_grep",
    "operator_log_append",
    "operator_queue_append",
    "report_result",
    "cron_propose",
    "status_report_run",
    "pipeline_task_start",
}


def _scaffold_cfg(root):
    """Minimal on-disk project so `build_mcp_server` can build the real
    toolset (mirrors the Claude suite's helper)."""
    from pathlib import Path

    from ap2.config import Config

    root = Path(root)
    (root / "TASKS.md").write_text(
        "# Tasks\n\n## Active\n\n## Ready\n\n## Backlog\n\n"
        "## Complete\n\n## Frozen\n"
    )
    cfg = Config.load(root)
    cfg.ensure_dirs()
    return cfg


def test_registered_tool_names_empty_before_build():
    adapter = CodexAdapter()
    assert adapter.registered_tool_names() == []


def test_build_tool_server_records_short_names_from_tool_set():
    """`build_tool_server` records each tool's `.name` for enumeration and
    returns a codex-native server descriptor — a direct adapter-surface unit
    test, independent of `build_mcp_server`."""
    from types import SimpleNamespace

    alpha = SimpleNamespace(name="alpha")
    beta = SimpleNamespace(name="beta")

    adapter = CodexAdapter()
    server = adapter.build_tool_server([alpha, beta], version="test")
    assert server["name"] == "autopilot"
    assert server["backend"] == "codex"
    assert server["version"] == "test"
    assert adapter.registered_tool_names() == ["alpha", "beta"]
    # Re-registering replaces the recorded set (reflects the latest call).
    adapter.build_tool_server([alpha], server_name="custom", version="test")
    assert adapter.registered_tool_names() == ["alpha"]


def test_build_tool_server_uses_injected_handle_builder():
    """When the injected codex handle exposes a `create_mcp_server(...)`
    builder, `build_tool_server` delegates to it (the production path) while
    still recording the short-names for enumeration."""
    from types import SimpleNamespace

    captured = {}

    def create_mcp_server(*, name, version, tools):
        captured["name"] = name
        captured["version"] = version
        captured["tools"] = tools
        return {"native": True, "name": name}

    codex = SimpleNamespace(create_mcp_server=create_mcp_server)
    adapter = CodexAdapter(codex=codex)
    server = adapter.build_tool_server(
        [SimpleNamespace(name="alpha")], version="v1"
    )
    assert server == {"native": True, "name": "autopilot"}
    assert captured["name"] == "autopilot"
    assert captured["version"] == "v1"
    assert adapter.registered_tool_names() == ["alpha"]


def test_codex_adapter_exposes_ap2_tool_short_names(tmp_path):
    """Axis-3 / axis-7 parity: ap2's real custom tool set, handed to the codex
    adapter's tool-registration surface via `tools.build_mcp_server`, is
    enumerated as exactly the expected short-names — the same set the Claude
    adapter exposes."""
    import ap2.tools as tools

    adapter = CodexAdapter()
    server = tools.build_mcp_server(_scaffold_cfg(tmp_path), adapter=adapter)

    # The adapter returned its backend-native MCP-server descriptor.
    assert isinstance(server, dict)
    assert server.get("name") == "autopilot"
    assert server.get("backend") == "codex"

    # And the adapter enumerates the full ap2 toolset through its surface —
    # no tool dropped or renamed relative to the Claude adapter.
    assert set(adapter.registered_tool_names()) == _EXPECTED_AP2_TOOL_SHORT_NAMES

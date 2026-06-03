"""TB-357 / goal.md axis 4 — adapter-contract test for the second backend,
`CodexAdapter` (TB-372: repointed onto OpenAI's real `openai_codex` SDK).

These tests pin axis 4's deliverable: a `CodexAdapter` driving OpenAI's Codex
agent through the SAME `AgentAdapter` contract `ClaudeCodeAdapter` satisfies.
They use a STUBBED codex handle (no live `codex` process) so the round-trip is
hermetic — `CodexAdapter(codex=<stub>)` injects a fake `openai_codex` module
mirroring the REAL client surface: `AsyncCodex().thread_start(...)` →
`thread.turn(prompt)` → `turn.stream()` replaying a canned `Notification` list,
plus the `ApprovalMode` / `Sandbox` enums the adapter coerces options onto.
Constructing the stub against the real shape (not a fabricated options/stream
API) is deliberate: a regression to invented symbols fails this suite.

Coverage (mirrors `test_agent_adapter.py`'s Claude suite so the two backends
are exercised against one shared contract):
  - `CodexAdapter` conforms to `AgentAdapter` (isinstance + abstract surface)
    and reports `backend == "codex"`.
  - A stubbed codex turn stream round-trips to a normalized `AgentResult` whose
    `usage` carries input/output/cache tokens — one shape, no per-backend
    branching (codex reports no per-run cost/turn/model, so those default).
  - `run()` yields one normalized `AgentEvent` per codex notification plus a
    terminal `type="result"` event.
  - `normalize_options` / `register_tools` map the backend-neutral options /
    tools onto the codex native kwargs; `run()` drives `thread_start` + `turn`.
  - The `stream_incomplete` fallback (no usage notification), the failed-turn
    error path, and the error / timeout paths of the base `run_to_result`.
  - `load_codex_sdk` resolves the real `openai_codex` module through
    `sys.modules` (the injected-handle seam).
  - The axis-3 tool-registration surface: ap2's real custom tool set, handed to
    `build_tool_server`, is enumerable via `registered_tool_names()` — the same
    set the Claude adapter exposes (axis-7 parity prep).
"""
from __future__ import annotations

import asyncio
import enum
import sys
from types import SimpleNamespace

import pytest

from ap2.adapters import (
    AgentAdapter,
    AgentOptions,
    AgentResult,
    AgentRunOptions,
    AgentTools,
    AgentUsage,
    CodexAdapter,
    load_codex_sdk,
)
from ap2.adapters.base import AgentEvent


# --------------------------------------------------------------------------
# Stub codex handle: a fake `openai_codex` MODULE mirroring the real client
# surface the adapter touches. `AsyncCodex().thread_start(...)` →
# `thread.turn(prompt)` → `turn.stream()` replays a canned `Notification` list
# (each `SimpleNamespace(method=..., payload=...)`, mirroring
# `openai_codex.models.Notification`), and `ApprovalMode` / `Sandbox` mirror the
# real `str, Enum` presets the adapter coerces options onto.
# --------------------------------------------------------------------------


class FakeApprovalMode(str, enum.Enum):
    """Mirror of `openai_codex.ApprovalMode` (a `str, Enum`)."""

    deny_all = "deny_all"
    auto_review = "auto_review"


class FakeSandbox(str, enum.Enum):
    """Mirror of `openai_codex.Sandbox` (a `str, Enum`; hyphenated values)."""

    read_only = "read-only"
    workspace_write = "workspace-write"
    full_access = "full-access"


class FakeCodex:
    """Stand-in for the `openai_codex` module handle.

    `AsyncCodex(...)` returns a fake client whose `thread_start(**kwargs)` /
    `thread.turn(prompt, **kwargs)` capture what the adapter passed, and whose
    `turn.stream()` replays the canned notification list as an async stream.
    `raise_on` injects a mid-stream fault; `stream_delay` sleeps before each
    notification (for the timeout path). The `captured` dict records the
    thread_start / turn kwargs + prompt; `closed` records that the client was
    closed.
    """

    ApprovalMode = FakeApprovalMode
    Sandbox = FakeSandbox

    def __init__(self, notifications, *, raise_on=None, stream_delay=None):
        self._notifications = notifications
        self._raise_on = raise_on
        self._stream_delay = stream_delay
        self.captured: dict = {}
        self.closed = False
        module = self

        class _AsyncTurnHandle:
            def __init__(self, turn_id):
                self.id = turn_id

            async def stream(self):
                for i, n in enumerate(module._notifications):
                    if module._stream_delay is not None:
                        await asyncio.sleep(module._stream_delay)
                    if module._raise_on is not None and i == module._raise_on:
                        raise RuntimeError("boom")
                    yield n

        class _AsyncThread:
            def __init__(self, thread_id):
                self.id = thread_id

            async def turn(self, prompt, **kwargs):
                module.captured["turn_prompt"] = prompt
                module.captured["turn"] = kwargs
                return _AsyncTurnHandle("turn_1")

            async def run(self, prompt, **kwargs):  # convenience; unused by adapter
                raise NotImplementedError

        class _AsyncCodex:
            def __init__(self, config=None):
                module.captured["config"] = config

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def close(self):
                module.closed = True

            async def thread_start(self, **kwargs):
                module.captured["thread_start"] = kwargs
                return _AsyncThread("thread_1")

        self.AsyncCodex = _AsyncCodex
        #: The adapter uses `AsyncCodex`; alias `Codex` for surface completeness.
        self.Codex = _AsyncCodex


# --- Notification builders mirroring the real `openai_codex` turn stream ----


def _item_completed(item_fields, *, turn_id="turn_1"):
    """An `item/completed` notification carrying a `ThreadItem`-shaped item."""
    return SimpleNamespace(
        method="item/completed",
        payload=SimpleNamespace(
            item=SimpleNamespace(**item_fields), turn_id=turn_id
        ),
    )


def _agent_message(text):
    return _item_completed({"type": "agentMessage", "text": text})


def _command_execution(command):
    return _item_completed({"type": "commandExecution", "command": command})


def _mcp_tool_call(tool, arguments):
    return _item_completed(
        {"type": "mcpToolCall", "tool": tool, "arguments": arguments}
    )


def _token_usage(*, input_tokens, output_tokens, cached_input_tokens=0, turn_id="turn_1"):
    breakdown = SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached_input_tokens,
    )
    return SimpleNamespace(
        method="thread/tokenUsage/updated",
        payload=SimpleNamespace(
            token_usage=SimpleNamespace(total=breakdown, last=breakdown),
            turn_id=turn_id,
        ),
    )


def _turn_started(turn_id="turn_1"):
    return SimpleNamespace(
        method="turn/started",
        payload=SimpleNamespace(turn=SimpleNamespace(id=turn_id)),
    )


def _turn_completed(turn_id="turn_1", status="completed", error=None):
    return SimpleNamespace(
        method="turn/completed",
        payload=SimpleNamespace(
            turn=SimpleNamespace(id=turn_id, status=status, error=error)
        ),
    )


def _default_envelopes():
    """A realistic turn-scoped notification stream: turn start, two agent
    messages around a command execution, a token-usage update, and a terminal
    turn-completed."""
    return [
        _turn_started(),
        _agent_message("working on it"),
        _command_execution("ls"),
        _agent_message("all done"),
        _token_usage(input_tokens=120, output_tokens=60, cached_input_tokens=20),
        _turn_completed(),
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


def test_load_codex_sdk_resolves_injected_module_via_sys_modules(monkeypatch):
    """`load_codex_sdk()` resolves the real module name `openai_codex` through
    `sys.modules`, so a test installing a fake there has it picked up — the
    injected-handle seam the daemon-start gate and the adapter share."""
    sentinel = FakeCodex(_default_envelopes())
    monkeypatch.setitem(sys.modules, "openai_codex", sentinel)
    assert load_codex_sdk() is sentinel


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
    # Final assistant text: the last agentMessage item's text.
    assert result.text == "all done"

    u = result.usage
    assert isinstance(u, AgentUsage)
    assert u.usage == {
        "input_tokens": 120,
        "output_tokens": 60,
        "cache_read_input_tokens": 20,
    }
    # Codex reports no per-run cost / turn count / model in its stream, so the
    # normalized record defaults those — the one-shape contract still holds.
    assert u.total_cost_usd == 0.0
    assert u.num_turns == 0
    assert u.model == ""
    assert u.note == ""
    # The normalized token sum the cost guards / `ap2 status` read.
    assert u.combined_tokens == 180


def test_run_yields_one_event_per_envelope_plus_terminal():
    adapter = CodexAdapter(codex=FakeCodex(_default_envelopes()))

    async def _collect():
        return [ev async for ev in adapter.run("p", _tools(), _options())]

    events = asyncio.run(_collect())
    # 6 turn notifications + 1 synthesized terminal "result" event.
    assert [e.type for e in events] == [
        "turn/started",
        "item/completed",
        "item/completed",
        "item/completed",
        "thread/tokenUsage/updated",
        "turn/completed",
        "result",
    ]
    # Only the terminal event carries a result.
    assert all(e.result is None for e in events[:-1])
    terminal = events[-1]
    assert isinstance(terminal, AgentEvent)
    assert terminal.result is not None
    assert terminal.result.status == "complete"
    assert terminal.result.usage.combined_tokens == 180
    # The first agentMessage notification's normalized summary/text.
    assert events[1].text == "working on it"
    assert events[1].summary["text_preview"] == "working on it"
    # The commandExecution notification surfaces the action in its summary.
    assert events[2].summary["tool_calls"][0]["name"] == "command"


# --------------------------------------------------------------------------
# Options / tools normalization + the real thread_start/turn dispatch
# --------------------------------------------------------------------------


def test_normalize_options_maps_codex_native_kwargs():
    adapter = CodexAdapter(codex=FakeCodex(_default_envelopes()))
    kwargs = adapter.normalize_options(_options())
    assert kwargs["model"] == "gpt-5-codex"
    assert kwargs["effort"] == "high"
    assert kwargs["cwd"] == "/tmp/proj"
    assert kwargs["approval_mode"] == "bypassPermissions"
    # timeout_s is owned by run_to_result's wait_for, NOT mapped onto options.
    assert "timeout_s" not in kwargs
    # max_turns has no real-SDK analog — not mapped.
    assert "max_turns" not in kwargs


def test_normalize_options_threads_extra():
    opts = AgentOptions(model="gpt-5-codex", extra={"sandbox": "workspace-write"})
    kwargs = CodexAdapter().normalize_options(opts)
    assert kwargs["sandbox"] == "workspace-write"


def test_register_tools_maps_tool_policy_and_mcp_servers():
    adapter = CodexAdapter(codex=FakeCodex(_default_envelopes()))
    tools = _tools()
    kwargs = adapter.register_tools(tools)
    assert kwargs["allowed_tools"] == ["Bash", "Read"]
    assert kwargs["disallowed_tools"] == ["Edit"]
    assert kwargs["mcp_servers"] is tools.mcp_servers


def test_run_drives_thread_start_and_turn():
    """`run()` constructs `AsyncCodex`, starts a thread with the
    conversation-level kwargs (model / cwd / approval_mode coerced onto the real
    `ApprovalMode` enum), then a turn with the per-turn `effort`, passing the
    prompt — and closes the client."""
    codex = FakeCodex(_default_envelopes())
    adapter = CodexAdapter(codex=codex)
    opts = AgentOptions(
        model="gpt-5-codex",
        effort="high",
        cwd="/tmp/proj",
        permission_mode="bypassPermissions",
        extra={"sandbox": "workspace-write"},
    )
    asyncio.run(adapter.run_to_result("the prompt", _tools(), opts))

    ts = codex.captured["thread_start"]
    assert ts["model"] == "gpt-5-codex"
    assert ts["cwd"] == "/tmp/proj"
    # permission_mode -> approval_mode coerced onto the real enum (bypass = auto).
    assert ts["approval_mode"] is FakeApprovalMode.auto_review
    # sandbox preset coerced onto the real Sandbox enum.
    assert ts["sandbox"] is FakeSandbox.workspace_write
    # effort is per-turn, not a thread_start kwarg.
    assert "effort" not in ts

    turn = codex.captured["turn"]
    assert turn["effort"] == "high"
    assert codex.captured["turn_prompt"] == "the prompt"
    # The client was closed after the stream drained.
    assert codex.closed is True


def test_run_drops_unresolvable_approval_mode():
    """An unmappable `permission_mode` is dropped rather than passed as an
    invalid value, so the SDK falls back to its own default."""
    codex = FakeCodex(_default_envelopes())
    adapter = CodexAdapter(codex=codex)
    opts = AgentOptions(model="m", permission_mode="totally-unknown-mode")
    asyncio.run(adapter.run_to_result("p", _tools(), opts))
    assert "approval_mode" not in codex.captured["thread_start"]


# --------------------------------------------------------------------------
# Failure / edge paths (base run_to_result, exercised through the codex stream)
# --------------------------------------------------------------------------


def test_stream_incomplete_when_no_usage_envelope():
    """A stream with no token-usage notification yields the `stream_incomplete`
    sentinel, mirroring the Claude path's empty-usage fallback."""
    envelopes = [_agent_message("partial")]
    adapter = CodexAdapter(codex=FakeCodex(envelopes))
    result = asyncio.run(adapter.run_to_result("p", _tools(), _options()))
    assert result.status == "complete"
    assert result.text == "partial"
    assert result.usage.note == "stream_incomplete"
    assert result.usage.usage == {}
    assert result.usage.total_cost_usd == 0.0
    assert result.usage.num_turns == 0


def test_failed_turn_maps_to_error():
    """A `turn/completed` reporting a failed turn yields a `status="error"`
    result carrying the turn's error message."""
    envelopes = [
        _turn_started(),
        _agent_message("trying"),
        _turn_completed(
            status="failed", error=SimpleNamespace(message="sandbox denied")
        ),
    ]
    adapter = CodexAdapter(codex=FakeCodex(envelopes))
    result = asyncio.run(adapter.run_to_result("p", _tools(), _options()))
    assert result.status == "error"
    assert result.error is not None
    assert "sandbox denied" in result.error


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
    adapter = CodexAdapter(
        codex=FakeCodex([_turn_completed()], stream_delay=10)
    )
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

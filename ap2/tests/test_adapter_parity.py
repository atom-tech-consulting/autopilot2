"""TB-359 / goal.md axis 7 — backend-parametrized adapter-contract parity suite.

There are two real `AgentAdapter` implementations — `ClaudeCodeAdapter`
(`ap2/adapters/claude_code.py`) and `CodexAdapter` (`ap2/adapters/codex.py`) —
and both must satisfy ONE shared contract. The per-backend suites
(`test_agent_adapter.py` / `test_codex_adapter.py`) pin each backend in
isolation; without a single cross-backend parity contract, a Codex regression
that diverges from the Claude behavior reference is invisible. Per goal.md's
axis-7 delete-test: "without a shared parity contract … Codex regressions land
silently." This suite parametrizes the SAME hermetic assertions over BOTH
backends so such a divergence fails loudly here.

Each `BackendCase` bundles an adapter factory driven by a STUBBED backend handle
(no live process) replaying a canned envelope list, plus the normalized values
that envelope list must produce. The suite then asserts both backends normalize
to the SAME `AgentResult` / `AgentUsage` / `AgentEvent` shapes — making the
contract the single source of truth both answer to:

  - conformance to the `AgentAdapter` ABC (isinstance + abstract surface +
    backend id);
  - `run()` yields one `AgentEvent` per stream envelope plus a terminal
    `type="result"` event carrying the `AgentResult`;
  - `run_to_result` normalizes usage (input/output tokens, cost, turns, model);
  - the `stream_incomplete` fallback (no usage-bearing envelope);
  - the error path (a mid-stream backend fault → `status="error"`);
  - the timeout path (a `timeout_s` exceedance → `status="timeout"`);
  - `registered_tool_names()` returns the IDENTICAL ap2 tool short-name set for
    both `ClaudeCodeAdapter` and `CodexAdapter`.

The stub handles and the `_EXPECTED_AP2_TOOL_SHORT_NAMES` toolset constant are
reused from the per-backend suites so this parity suite and the per-backend
suites never drift on what the contract is.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable

import pytest

from ap2.adapters import (
    AgentAdapter,
    AgentOptions,
    AgentResult,
    AgentTools,
    AgentUsage,
    ClaudeCodeAdapter,
    CodexAdapter,
)
from ap2.adapters.base import AgentEvent

# Reuse the Claude suite's stub SDK + message classes, the canonical ap2-toolset
# constant, and the on-disk project scaffold; reuse the codex suite's stub
# handle + envelope list. Importing the per-backend suites' helpers (rather than
# re-declaring them) is deliberate: the parity contract is asserted against the
# SAME stubs the per-backend suites use, so the two can never drift on what the
# contract is.
from ap2.tests.test_agent_adapter import (
    _EXPECTED_AP2_TOOL_SHORT_NAMES,
    AssistantMessage as _ClaudeAssistantMessage,
    FakeSDK as _FakeSDK,
    _default_envelopes as _claude_default_envelopes,
    _scaffold_cfg,
    _text_block as _claude_text_block,
)
from ap2.tests.test_codex_adapter import (
    FakeCodex as _FakeCodex,
    _default_envelopes as _codex_default_envelopes,
)


# --------------------------------------------------------------------------
# Slow stub handles for the shared timeout path: each sleeps past the
# bounded drain so the base `run_to_result`'s `asyncio.wait_for` fires.
# --------------------------------------------------------------------------


class _SlowSDK(_FakeSDK):
    async def _stream(self):
        await asyncio.sleep(10)
        yield _ClaudeAssistantMessage([_claude_text_block("never reached")])


class _SlowCodex(_FakeCodex):
    async def _stream(self):
        await asyncio.sleep(10)
        yield {"type": "turn.completed", "usage": {}}


# --------------------------------------------------------------------------
# Backend-neutral options / tools the parity drains share. The per-backend
# native mapping differs (claude `extra_args` vs codex `reasoning_effort`),
# but `run()`'s normalized output is driven by the envelope stream, so one
# generic options/tools object exercises both contracts identically.
# --------------------------------------------------------------------------


def _options() -> AgentOptions:
    return AgentOptions(
        model="m",
        effort="high",
        max_turns=80,
        cwd="/tmp/proj",
        permission_mode="bypassPermissions",
        setting_sources=["project"],
    )


def _tools() -> AgentTools:
    return AgentTools(
        allowed=["Bash", "Read"],
        disallowed=["Edit"],
        mcp_servers={"autopilot": object()},
    )


# --------------------------------------------------------------------------
# The parametrized backend cases. Each bundles the adapter/handle factories,
# the canned envelope lists, and the normalized values those envelopes must
# produce — so one set of assertions runs against both backends.
# --------------------------------------------------------------------------


@dataclass
class BackendCase:
    name: str
    backend_id: str
    #: (envelopes, *, raise_on=None) -> adapter wrapping a stub handle.
    make_adapter: Callable[..., AgentAdapter]
    #: fresh adapter with no injected handle (for tool registration).
    bare_adapter: Callable[[], AgentAdapter]
    #: canned happy-path envelope list (one usage-bearing terminal envelope).
    default_envelopes: Callable[[], list]
    #: a stream with NO usage envelope → the `stream_incomplete` fallback.
    incomplete_envelopes: Callable[[], list]
    #: adapter whose stub stream sleeps past the bounded drain.
    slow_adapter: Callable[[], AgentAdapter]
    expected_text: str
    expected_usage: dict
    expected_total_cost: float
    expected_num_turns: int
    expected_model: str
    expected_stream_types: list[str]
    #: envelope index at which the stub raises (mid-stream fault).
    error_raise_on: int
    incomplete_text: str


def _claude_adapter(envelopes: list, *, raise_on: Any = None) -> ClaudeCodeAdapter:
    return ClaudeCodeAdapter(sdk=_FakeSDK(envelopes, raise_on=raise_on))


def _codex_adapter(envelopes: list, *, raise_on: Any = None) -> CodexAdapter:
    return CodexAdapter(codex=_FakeCodex(envelopes, raise_on=raise_on))


_CLAUDE_CASE = BackendCase(
    name="claude",
    backend_id="claude",
    make_adapter=_claude_adapter,
    bare_adapter=ClaudeCodeAdapter,
    default_envelopes=_claude_default_envelopes,
    incomplete_envelopes=lambda: [
        _ClaudeAssistantMessage([_claude_text_block("partial")])
    ],
    slow_adapter=lambda: ClaudeCodeAdapter(sdk=_SlowSDK([])),
    expected_text="all done",
    expected_usage={"input_tokens": 100, "output_tokens": 50},
    expected_total_cost=0.0123,
    expected_num_turns=3,
    expected_model="claude-opus-4-7",
    expected_stream_types=[
        "AssistantMessage",
        "AssistantMessage",
        "ResultMessage",
        "result",
    ],
    error_raise_on=1,
    incomplete_text="partial",
)


_CODEX_CASE = BackendCase(
    name="codex",
    backend_id="codex",
    make_adapter=_codex_adapter,
    bare_adapter=CodexAdapter,
    default_envelopes=_codex_default_envelopes,
    incomplete_envelopes=lambda: [
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "partial"},
        }
    ],
    slow_adapter=lambda: CodexAdapter(codex=_SlowCodex([])),
    expected_text="all done",
    expected_usage={
        "input_tokens": 120,
        "output_tokens": 60,
        "cache_read_input_tokens": 20,
    },
    expected_total_cost=0.0456,
    expected_num_turns=4,
    expected_model="gpt-5-codex",
    expected_stream_types=[
        "thread.started",
        "turn.started",
        "item.completed",
        "item.completed",
        "item.completed",
        "turn.completed",
        "result",
    ],
    error_raise_on=2,
    incomplete_text="partial",
)


_CASES = [_CLAUDE_CASE, _CODEX_CASE]


@pytest.fixture(params=_CASES, ids=[c.name for c in _CASES])
def backend(request) -> BackendCase:
    """Parametrize every parity test over both adapter backends."""
    return request.param


# --------------------------------------------------------------------------
# Conformance — both backends are `AgentAdapter`s with the full surface.
# --------------------------------------------------------------------------


def test_adapter_conforms_to_abc(backend: BackendCase):
    adapter = backend.make_adapter(backend.default_envelopes())
    assert isinstance(adapter, AgentAdapter)
    assert adapter.backend == backend.backend_id
    # The full abstract + concrete surface is present on both backends.
    for attr in (
        "run",
        "normalize_options",
        "register_tools",
        "run_to_result",
        "build_tool_server",
        "registered_tool_names",
    ):
        assert callable(getattr(adapter, attr)), attr


# --------------------------------------------------------------------------
# Round-trip — a stubbed happy-path stream normalizes identically.
# --------------------------------------------------------------------------


def test_run_to_result_round_trips_usage(backend: BackendCase):
    adapter = backend.make_adapter(backend.default_envelopes())
    result = asyncio.run(
        adapter.run_to_result("do the thing", _tools(), _options())
    )
    assert isinstance(result, AgentResult)
    assert result.status == "complete"
    assert result.text == backend.expected_text

    u = result.usage
    assert isinstance(u, AgentUsage)
    assert u.usage == backend.expected_usage
    assert u.total_cost_usd == backend.expected_total_cost
    assert u.num_turns == backend.expected_num_turns
    assert u.model == backend.expected_model
    assert u.note == ""


def test_run_yields_one_event_per_envelope_plus_terminal(backend: BackendCase):
    adapter = backend.make_adapter(backend.default_envelopes())

    async def _collect() -> list[AgentEvent]:
        return [ev async for ev in adapter.run("p", _tools(), _options())]

    events = asyncio.run(_collect())
    # One `AgentEvent` per stream envelope + a synthesized terminal "result".
    assert [e.type for e in events] == backend.expected_stream_types
    # Only the terminal event carries a result.
    assert all(e.result is None for e in events[:-1])
    terminal = events[-1]
    assert isinstance(terminal, AgentEvent)
    assert terminal.type == "result"
    assert terminal.result is not None
    assert terminal.result.status == "complete"
    assert terminal.result.usage.total_cost_usd == backend.expected_total_cost


# --------------------------------------------------------------------------
# Failure / edge paths — `run_to_result`'s shared drain handling.
# --------------------------------------------------------------------------


def test_stream_incomplete_when_no_usage_envelope(backend: BackendCase):
    """A stream with no usage-bearing terminal envelope yields the
    `stream_incomplete` sentinel on BOTH backends — the empty-usage fallback
    the daemon's `*_run_usage` emission stamps."""
    adapter = backend.make_adapter(backend.incomplete_envelopes())
    result = asyncio.run(adapter.run_to_result("p", _tools(), _options()))
    assert result.status == "complete"
    assert result.text == backend.incomplete_text
    assert result.usage.note == "stream_incomplete"
    assert result.usage.usage == {}
    assert result.usage.total_cost_usd == 0.0
    assert result.usage.num_turns == 0


def test_run_to_result_error_path(backend: BackendCase):
    """A backend fault mid-stream becomes a `status="error"` result carrying
    the `<Type>: <msg>` string — the daemon's error vocabulary, shared by the
    base `run_to_result` across backends."""
    adapter = backend.make_adapter(
        backend.default_envelopes(), raise_on=backend.error_raise_on
    )
    result = asyncio.run(adapter.run_to_result("p", _tools(), _options()))
    assert result.status == "error"
    assert result.error is not None
    assert "RuntimeError" in result.error
    # No usage envelope was captured before the fault → stream_incomplete.
    assert result.usage.note == "stream_incomplete"


def test_run_to_result_timeout_path(backend: BackendCase):
    """When `timeout_s` is exceeded the drain is cancelled and a
    `status="timeout"` result is returned on BOTH backends (the base adapter's
    `asyncio.wait_for` behavior, owned once and shared)."""
    adapter = backend.slow_adapter()
    opts = _options()
    opts.timeout_s = 0.05
    result = asyncio.run(adapter.run_to_result("p", _tools(), opts))
    assert result.status == "timeout"
    assert result.usage.note == "stream_incomplete"


# --------------------------------------------------------------------------
# Cross-backend toolset parity — the heart of axis 7.
#
# ap2's REAL custom tool set, handed to each adapter's `build_tool_server`
# surface via `tools.build_mcp_server`, must enumerate to the IDENTICAL
# short-name set for both `ClaudeCodeAdapter` and `CodexAdapter`. A tool
# dropped or renamed on one backend (or registered on only one) trips this.
# --------------------------------------------------------------------------


def test_both_backends_register_identical_ap2_toolset(tmp_path):
    """`registered_tool_names()` returns the identical ap2 tool short-name set
    for both `ClaudeCodeAdapter` and `CodexAdapter` — the cross-backend
    toolset-parity assertion (reuses `_EXPECTED_AP2_TOOL_SHORT_NAMES`)."""
    import ap2.tools as tools

    cfg = _scaffold_cfg(tmp_path)

    claude = ClaudeCodeAdapter()
    codex = CodexAdapter()
    # Hand the SAME real ap2 toolset to each adapter's registration surface.
    tools.build_mcp_server(cfg, adapter=claude)
    tools.build_mcp_server(cfg, adapter=codex)

    claude_names = set(claude.registered_tool_names())
    codex_names = set(codex.registered_tool_names())

    # Each backend exposes exactly the canonical ap2 toolset …
    assert claude_names == _EXPECTED_AP2_TOOL_SHORT_NAMES
    assert codex_names == _EXPECTED_AP2_TOOL_SHORT_NAMES
    # … and therefore the two backends register one identical set.
    assert claude_names == codex_names


def test_bare_adapter_enumerates_empty_before_registration(backend: BackendCase):
    """Both backends start with an empty toolset enumeration — the accessor
    reflects the most recent `build_tool_server` call, not a hardcoded list."""
    adapter = backend.bare_adapter()
    assert adapter.registered_tool_names() == []

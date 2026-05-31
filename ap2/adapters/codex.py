"""`CodexAdapter` — the OpenAI `codex` backend behind the `AgentAdapter` seam
(TB-357 / goal.md axis 4).

Axes 1-3 landed the backend-agnostic seam — the `AgentAdapter` ABC plus the
`AgentOptions` / `AgentTools` / `AgentResult` / `AgentUsage` / `AgentEvent`
types (`ap2/adapters/base.py`), the canonical options + normalized usage
record, and the `build_tool_server` / `registered_tool_names` tool-registration
surface — with exactly one implementation, `ClaudeCodeAdapter`. Per goal.md's
axis-4 delete-test: "an abstraction with one implementation is no actual Codex
support." This module adds the second implementation: a `CodexAdapter`
(`backend = "codex"`) that drives OpenAI's `codex` CLI agent through the same
contract, structurally mirroring `ClaudeCodeAdapter`'s three-method shape.

What it preserves from the shared contract:

  - Options mapping (`normalize_options`): the backend-neutral `AgentOptions`
    (`model` / `effort` / `max_turns` / `cwd` / `permission_mode` / `stderr`)
    map onto the codex CLI's native invocation kwargs (`model` /
    `reasoning_effort` / `max_turns` / `cwd` / `approval_policy` / `stderr`).
    `timeout_s` is intentionally NOT mapped — the base `run_to_result` owns the
    per-run timeout via `asyncio.wait_for`, exactly as it does for Claude.
  - Tool exposure (`register_tools` / `build_tool_server`): `AgentTools`
    (allow/deny policy + `mcp_servers`) map onto codex's tool-exposure kwargs,
    and ap2's custom tool set is handed to `build_tool_server` as a unit. The
    registered short-names are recorded on `self._registered_tool_names` so the
    base `registered_tool_names()` enumerates them — axis 7's cross-backend
    parity test reads this to assert both backends register one identical set.
  - Stream parsing (`run`): each codex stream envelope is normalized to an
    `AgentEvent` carrying the same compact/full/text triple the Claude path
    produces, then a terminal `AgentEvent(type="result")` carries the
    normalized `AgentResult` (status / text / commit / usage). Commit / usage
    extraction populates the normalized record so the cost guards and
    `ap2 status` read one shape regardless of backend.

The codex stream shape mirrors OpenAI's `codex exec --json` thread-event
output: a sequence of envelopes each with a `type` (`"thread.started"` /
`"turn.started"` / `"item.completed"` / `"turn.completed"` / ...), `item.*`
envelopes carrying an `item` payload (`agent_message`, `command_execution`,
`mcp_tool_call`, `file_change`, ...), and a terminal `turn.completed` carrying
`usage`. `_summarize_codex_event` / `_serialize_codex_event_full` /
`_extract_codex_text` normalize each envelope into the SAME compact-summary
shape `ap2.message_dump` produces for the Claude path, so the usage derivation
(`usage_from_summary`) and the base `run_to_result` drain loop are reused
verbatim — no per-backend branching downstream.

The codex handle is injectable (constructor arg, lazy import when `None`)
exactly as `ClaudeCodeAdapter` injects `sdk`, so the contract test runs
hermetically against a stub with no live `codex` process. No production
dispatch site is repointed to this adapter in axis 4 — that migration is axis
6, one TB per site.
"""
from __future__ import annotations

import json as _json
from collections.abc import AsyncIterator, Sequence
from typing import Any

from ..message_dump import _truncate
from .base import (
    AgentAdapter,
    AgentEvent,
    AgentOptions,
    AgentResult,
    AgentTools,
    usage_from_summary,
)


# --------------------------------------------------------------------------
# codex stream-envelope normalization
#
# The codex CLI (`codex exec --json`) emits thread events as JSON objects, so
# the envelopes are dict-shaped; we tolerate object-shaped envelopes too (a
# future SDK may hand back typed objects) via `_field`. Each helper builds the
# SAME compact-summary shape `ap2.message_dump._summarize_message` produces for
# the Claude path so the shared `usage_from_summary` / `run_to_result` drain
# loop work unchanged across backends.
# --------------------------------------------------------------------------


def _field(env: Any, key: str, default: Any = None) -> Any:
    """Read `key` off a codex envelope that may be a dict (the `--json`
    shape) or an object (a future typed SDK)."""
    if isinstance(env, dict):
        return env.get(key, default)
    return getattr(env, key, default)


def _codex_event_type(env: Any) -> str:
    """The codex thread-event type string (`"turn.completed"`, ...), falling
    back to the envelope's class name when absent."""
    t = _field(env, "type")
    if isinstance(t, str) and t:
        return t
    return type(env).__name__


def _codex_item(env: Any) -> Any:
    """The `item` payload an `item.*` envelope carries (the agent message /
    command-execution / mcp-tool-call record), or `None`."""
    return _field(env, "item")


def _extract_codex_text(env: Any) -> str:
    """Best-effort extraction of an envelope's assistant text.

    Codex carries assistant text on an `agent_message` item
    (`item.completed` → `item.text`); some variants put it directly on the
    envelope (`.text` / `.message`). Returns `""` when none — the same empty
    sentinel the Claude path's `_extract_text` returns for tool-only turns.
    """
    item = _codex_item(env)
    if item is not None:
        itype = _field(item, "type")
        if itype in ("agent_message", "assistant_message"):
            txt = _field(item, "text")
            if not isinstance(txt, str):
                txt = _field(item, "message")
            if isinstance(txt, str) and txt.strip():
                return txt
    txt = _field(env, "text")
    if isinstance(txt, str) and txt.strip():
        return txt
    return ""


def _codex_tool_call(item: Any) -> dict | None:
    """Normalize a codex action item (command-execution / mcp-tool-call /
    file-change) into the `{name, args_preview}` shape `_summarize_message`
    emits for a Claude `ToolUseBlock`, or `None` for a non-action item."""
    itype = _field(item, "type")
    if itype == "command_execution":
        cmd = _field(item, "command")
        return {
            "name": "command",
            "args_preview": _truncate(_json.dumps(cmd, default=str), 200),
        }
    if itype == "mcp_tool_call":
        return {
            "name": _field(item, "tool") or "mcp_tool_call",
            "args_preview": _truncate(
                _json.dumps(_field(item, "arguments"), default=str), 200
            ),
        }
    if itype == "file_change":
        return {
            "name": "file_change",
            "args_preview": _truncate(
                _json.dumps(_field(item, "changes"), default=str), 200
            ),
        }
    return None


def _codex_usage(env: Any) -> dict:
    """Normalize a codex `usage` blob (`input_tokens` / `cached_input_tokens`
    / `output_tokens`) into the Anthropic-style usage dict the normalized
    `AgentUsage.combined_tokens` reads (`input_tokens` + `output_tokens`, with
    cache reads under `cache_read_input_tokens`). Returns `{}` when the
    envelope carries no usage."""
    usage = _field(env, "usage")
    if not isinstance(usage, dict):
        return {}
    out: dict = {
        "input_tokens": usage.get("input_tokens", 0) or 0,
        "output_tokens": usage.get("output_tokens", 0) or 0,
    }
    cached = usage.get("cached_input_tokens", 0) or 0
    if cached:
        out["cache_read_input_tokens"] = cached
    return out


def _summarize_codex_event(env: Any) -> dict:
    """Compact per-envelope summary in the exact shape
    `ap2.message_dump._summarize_message` produces for a Claude envelope:
    `{type, text_preview?, tool_calls?, usage?, total_cost_usd?, num_turns?,
    model?}`. Optional fields are omitted when absent so the stream stays
    scannable and so the base `run_to_result` only treats usage-bearing
    envelopes as the cost source."""
    out: dict = {"type": _codex_event_type(env)}
    text = _extract_codex_text(env)
    if text:
        out["text_preview"] = _truncate(text, 200)
    item = _codex_item(env)
    if item is not None:
        tc = _codex_tool_call(item)
        if tc is not None:
            out["tool_calls"] = [tc]
    usage = _codex_usage(env)
    if usage:
        out["usage"] = usage
    for src, dst in (
        ("total_cost_usd", "total_cost_usd"),
        ("cost_usd", "total_cost_usd"),
        ("num_turns", "num_turns"),
        ("model", "model"),
    ):
        v = _field(env, src)
        if v is not None and dst not in out:
            out[dst] = v
    return out


def _serialize_codex_event_full(env: Any) -> dict:
    """Full-content per-envelope record for the `.messages.jsonl` debug dump —
    the codex analogue of `_serialize_message_full`. Dict envelopes pass
    through verbatim (codex `--json` is already serialized); object envelopes
    are projected onto their well-known fields."""
    if isinstance(env, dict):
        return dict(env)
    out: dict = {"type": _codex_event_type(env)}
    for k in (
        "item",
        "usage",
        "total_cost_usd",
        "cost_usd",
        "num_turns",
        "model",
        "text",
        "message",
        "thread_id",
    ):
        v = _field(env, k)
        if v is not None:
            out[k] = v
    return out


class CodexAdapter(AgentAdapter):
    """`AgentAdapter` implementation driving OpenAI's `codex` CLI agent.

    `codex` may be injected (the daemon will pass an already-imported codex
    handle; tests pass a stub exposing `CodexOptions` + `run_streamed`). When
    `None`, the module is imported lazily on first use — so this module loads
    without the codex SDK installed and the contract test can inject a stub
    with no live `codex` process.
    """

    backend = "codex"

    def __init__(self, codex: Any = None) -> None:
        self._codex = codex
        #: Short-names of the tools registered by the last `build_tool_server`
        #: call; surfaced via the base `registered_tool_names()` accessor.
        self._registered_tool_names: list[str] = []

    def _get_codex(self) -> Any:
        if self._codex is None:
            import codex_sdk as codex  # type: ignore

            self._codex = codex
        return self._codex

    def normalize_options(self, options: AgentOptions) -> dict[str, Any]:
        """Map a backend-neutral `AgentOptions` to the codex CLI's native
        invocation kwargs.

        `timeout_s` is intentionally NOT mapped onto the native options: the
        base `run_to_result` owns the per-run timeout via `asyncio.wait_for`,
        matching the Claude adapter. `effort` becomes codex's
        `reasoning_effort`; `permission_mode` becomes codex's `approval_policy`.
        `extra` is threaded straight through for forward-compatibility.
        """
        kwargs: dict[str, Any] = {}
        if options.model is not None:
            kwargs["model"] = options.model
        if options.effort is not None:
            kwargs["reasoning_effort"] = options.effort
        if options.max_turns is not None:
            kwargs["max_turns"] = options.max_turns
        if options.cwd is not None:
            kwargs["cwd"] = options.cwd
        if options.permission_mode is not None:
            kwargs["approval_policy"] = options.permission_mode
        if options.stderr is not None:
            kwargs["stderr"] = options.stderr
        if options.extra:
            kwargs.update(options.extra)
        return kwargs

    def register_tools(self, tools: AgentTools) -> dict[str, Any]:
        """Map a backend-neutral `AgentTools` to codex's tool-exposure kwargs
        (`mcp_servers` / `allowed_tools` / `disallowed_tools`). The
        `{"autopilot": <server>}` MCP map carries ap2's custom tools verbatim,
        exactly as the Claude path threads them."""
        kwargs: dict[str, Any] = {}
        if tools.mcp_servers:
            kwargs["mcp_servers"] = tools.mcp_servers
        if tools.allowed is not None:
            kwargs["allowed_tools"] = tools.allowed
        if tools.disallowed is not None:
            kwargs["disallowed_tools"] = tools.disallowed
        return kwargs

    def build_tool_server(
        self,
        tool_set: Sequence[Any],
        *,
        server_name: str = "autopilot",
        version: str = "unknown",
    ) -> Any:
        """Expose ap2's custom tool set to the codex backend (axis 3 surface).

        `tool_set` is ap2's canonical custom-tool inventory (the same
        `report_result` / `cron_propose` / `pipeline_task_start` / ... list
        `tools.build_mcp_server` hands the Claude adapter). The registered
        short-names (read off each tool's `.name`, exactly as the Claude path)
        are captured for the base `registered_tool_names()` enumeration — axis
        7's parity test asserts both backends register the identical set.

        Returns a codex-native MCP-server descriptor. If the injected codex
        handle exposes a `create_mcp_server(...)` builder it is used; otherwise
        a plain descriptor dict is returned so the surface works hermetically
        (no live codex process required for tool registration).
        """
        tools_list = list(tool_set)
        self._registered_tool_names = [
            name
            for name in (getattr(t, "name", None) for t in tools_list)
            if name
        ]
        builder = (
            getattr(self._codex, "create_mcp_server", None)
            if self._codex is not None
            else None
        )
        if callable(builder):
            return builder(
                name=server_name,
                version=version,
                tools=tools_list,
            )
        return {
            "name": server_name,
            "version": version,
            "backend": "codex",
            "tools": tools_list,
        }

    async def run(
        self,
        prompt: str,
        tools: AgentTools,
        options: AgentOptions,
    ) -> AsyncIterator[AgentEvent]:
        """Dispatch the run via the codex CLI agent.

        Async generator: yields one normalized `AgentEvent` per codex stream
        envelope (parsed through this module's codex-specific summary / full /
        text helpers, which mirror the Claude path's `ap2.message_dump`
        triple), then a terminal `AgentEvent(type="result")` carrying a
        `status="complete"` `AgentResult` whose usage is derived from the last
        usage-bearing envelope via the shared `usage_from_summary`. Exceptions
        from the codex backend propagate to the caller — the base
        `run_to_result` turns them into a `status="error"` result, and a
        `timeout_s` exceedance into `status="timeout"`.
        """
        codex = self._get_codex()
        opt_kwargs = {
            **self.normalize_options(options),
            **self.register_tools(tools),
        }
        native_options = codex.CodexOptions(**opt_kwargs)

        final_text = ""
        last_usage_summary: dict | None = None
        last_envelope: Any = None

        async for env in codex.run_streamed(
            prompt=prompt, options=native_options
        ):
            summary = _summarize_codex_event(env)
            full = _serialize_codex_event_full(env)
            text = _extract_codex_text(env)
            if text:
                final_text = text
            if "usage" in summary or "total_cost_usd" in summary:
                last_usage_summary = summary
                last_envelope = env
            yield AgentEvent(
                type=summary.get("type") or _codex_event_type(env),
                raw=env,
                summary=summary,
                full=full,
                text=text,
            )

        yield AgentEvent(
            type="result",
            result=AgentResult(
                status="complete",
                text=final_text,
                usage=usage_from_summary(last_usage_summary),
                raw_result=last_envelope,
            ),
        )

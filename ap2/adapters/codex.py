"""`CodexAdapter` — the OpenAI Codex backend behind the `AgentAdapter` seam
(TB-357 / goal.md axis 4), driven through OpenAI's official `openai_codex`
Python SDK.

Axes 1-3 landed the backend-agnostic seam — the `AgentAdapter` ABC plus the
`AgentOptions` / `AgentTools` / `AgentResult` / `AgentUsage` / `AgentEvent`
types (`ap2/adapters/base.py`) — with exactly one implementation,
`ClaudeCodeAdapter`. Per goal.md's axis-4 delete-test: "an abstraction with one
implementation is no actual Codex support." This module adds the second
implementation: a `CodexAdapter` (`backend = "codex"`) that drives OpenAI's
Codex agent through the same contract, structurally mirroring
`ClaudeCodeAdapter`'s three-method shape.

TB-372 repointed this adapter onto the REAL OpenAI SDK. The handle is the
`openai_codex` module (distribution `openai-codex`, which bundles the Codex CLI
binary); the dispatch path is the SDK's typed client surface:

    client = openai_codex.AsyncCodex()
    thread = await client.thread_start(
        model=..., sandbox=..., approval_mode=..., cwd=...,
    )
    turn = await thread.turn(prompt, effort=...)
    async for notif in turn.stream():   # `openai_codex.models.Notification`
        ...
    # a terminal `turn/completed` notification ends the stream

What it preserves from the shared contract:

  - Options mapping (`normalize_options`): the backend-neutral `AgentOptions`
    (`model` / `effort` / `cwd` / `permission_mode`) map onto the real SDK
    parameters — `model` / `effort` (a `ReasoningEffort`) / `cwd` /
    `approval_mode` (an `ApprovalMode`); `extra` may carry a `sandbox`
    (`Sandbox`) preset. `timeout_s` is intentionally NOT mapped — the base
    `run_to_result` owns the per-run timeout via `asyncio.wait_for`, exactly as
    it does for Claude. The normalized kwargs are partitioned across the SDK's
    two calls (`thread_start` configures the conversation; `turn` configures the
    single turn) so we never pass a kwarg a given call does not accept.
  - Tool exposure (`register_tools` / `build_tool_server`): ap2's custom tool
    set is handed to `build_tool_server` as a unit and the registered
    short-names are recorded for the base `registered_tool_names()`
    enumeration — axis 7's cross-backend parity test reads this to assert both
    backends register one identical set. (Delivering ap2's *in-process* MCP tool
    server to a live Codex agent — which consumes external stdio MCP servers via
    its own config — is a separate concern not wired into this dispatch path.)
  - Stream parsing (`run`): each `openai_codex` turn `Notification` is
    normalized to an `AgentEvent` carrying the same compact/full/text triple the
    Claude path produces, then a terminal `AgentEvent(type="result")` carries
    the normalized `AgentResult` (status / text / usage). Usage is derived from
    the `thread/tokenUsage/updated` notification's `ThreadTokenUsage` so the cost
    guards and `ap2 status` read one shape regardless of backend.

The `openai_codex` turn stream is a sequence of `Notification(method, payload)`
records: `turn/started`, `item/completed` (carrying a `ThreadItem` — an
`AgentMessageThreadItem` / `CommandExecutionThreadItem` / `McpToolCallThreadItem`
/ `FileChangeThreadItem`), `thread/tokenUsage/updated` (carrying a
`ThreadTokenUsage`), and a terminal `turn/completed` (carrying the `Turn` with
its `status`). `_summarize_codex_event` / `_serialize_codex_event_full` /
`_extract_codex_text` normalize each record into the SAME compact-summary shape
`ap2.message_dump` produces for the Claude path, so the usage derivation
(`usage_from_summary`) and the base `run_to_result` drain loop are reused
verbatim — no per-backend branching downstream.

The codex handle is injectable (constructor arg, lazy import when `None`)
exactly as `ClaudeCodeAdapter` injects `sdk`, so the contract test runs
hermetically against a stub mirroring the real `Codex` / `thread_start` / `turn`
/ notification surface with no live `codex` process. No production dispatch site
is repointed to this adapter in axis 4 — that migration is axis 6, one TB per
site.
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


def load_codex_sdk() -> Any:
    """Import and return the `openai_codex` module (OpenAI's real Codex SDK).

    TB-369/TB-372: the single relocation point for the lazy `import
    openai_codex` that `CodexAdapter._get_codex()` resolves through. Routing both
    the adapter and the daemon-start codex-availability gate
    (`daemon._require_codex_handle_if_referenced`) through this one helper means
    they agree, bit-for-bit, on what "codex is available" means — the exact
    mirror of `claude_code.load_claude_sdk` for the Claude SDK gate.

    The import is resolved at call time against `sys.modules`, so a test that
    installs a fake `openai_codex` module (via `monkeypatch.setitem(sys.modules,
    "openai_codex", fake)`) still has its fake picked up here — the
    injected-handle seam is preserved. The import is lazy (inside the function
    body) so importing `ap2.adapters` does not require the codex SDK to be
    installed.
    """
    import openai_codex as codex  # type: ignore

    return codex


# --------------------------------------------------------------------------
# codex notification normalization
#
# The `openai_codex` turn stream yields `Notification(method, payload)` records.
# We read them by duck-typing (`_field`) rather than `isinstance` against the
# real payload classes so this module imports without the codex SDK installed
# and the hermetic contract test can replay lightweight notification stubs. Each
# helper builds the SAME compact-summary shape `ap2.message_dump._summarize_message`
# produces for the Claude path so the shared `usage_from_summary` /
# `run_to_result` drain loop work unchanged across backends.
# --------------------------------------------------------------------------


def _field(obj: Any, key: str, default: Any = None) -> Any:
    """Read `key` off a value that may be a dict or an object (a pydantic
    model / dataclass / stub)."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _codex_method(notif: Any) -> str:
    """The `openai_codex` notification method string (`"turn/completed"`,
    `"item/completed"`, ...), falling back to the payload's (or notification's)
    class name when absent."""
    m = _field(notif, "method")
    if isinstance(m, str) and m:
        return m
    payload = _field(notif, "payload")
    if payload is not None:
        return type(payload).__name__
    return type(notif).__name__


def _codex_payload(notif: Any) -> Any:
    """The notification's `payload` (one of the `openai_codex` notification
    models), tolerating a payload-less record by falling back to the record
    itself."""
    return _field(notif, "payload", notif)


def _codex_item(notif: Any) -> Any:
    """The `ThreadItem` an `item/*` notification carries, unwrapped from the
    pydantic `RootModel` (`.root`) to the concrete item (`AgentMessageThreadItem`
    / `CommandExecutionThreadItem` / ...), or `None`."""
    item = _field(_codex_payload(notif), "item")
    if item is None:
        return None
    return _field(item, "root", item)


def _extract_codex_text(notif: Any) -> str:
    """Best-effort extraction of a notification's assistant text.

    Codex carries assistant text on an `AgentMessageThreadItem`
    (`item/completed` → `item.text`) and incrementally on the
    `item/agentMessage/delta` notification (`payload.delta`). Returns `""` when
    none — the same empty sentinel the Claude path's `_extract_text` returns for
    tool-only turns.
    """
    payload = _codex_payload(notif)
    delta = _field(payload, "delta")
    if isinstance(delta, str) and delta.strip():
        return delta
    item = _codex_item(notif)
    if item is not None:
        itype = _field(item, "type")
        if itype in ("agentMessage", "assistantMessage"):
            txt = _field(item, "text")
            if isinstance(txt, str) and txt.strip():
                return txt
    return ""


def _codex_tool_call(item: Any) -> dict | None:
    """Normalize a codex action item (`CommandExecutionThreadItem` /
    `McpToolCallThreadItem` / `FileChangeThreadItem`) into the
    `{name, args_preview}` shape `_summarize_message` emits for a Claude
    `ToolUseBlock`, or `None` for a non-action item."""
    itype = _field(item, "type")
    if itype == "commandExecution":
        cmd = _field(item, "command")
        return {
            "name": "command",
            "args_preview": _truncate(_json.dumps(cmd, default=str), 200),
        }
    if itype == "mcpToolCall":
        return {
            "name": _field(item, "tool") or "mcpToolCall",
            "args_preview": _truncate(
                _json.dumps(_field(item, "arguments"), default=str), 200
            ),
        }
    if itype == "fileChange":
        return {
            "name": "fileChange",
            "args_preview": _truncate(
                _json.dumps(_field(item, "changes"), default=str), 200
            ),
        }
    return None


def _token_breakdown_to_dict(breakdown: Any) -> dict:
    """Normalize a codex `TokenUsageBreakdown` (`input_tokens` /
    `cached_input_tokens` / `output_tokens`) into the Anthropic-style usage dict
    the normalized `AgentUsage.combined_tokens` reads (`input_tokens` +
    `output_tokens`, with cache reads under `cache_read_input_tokens`)."""
    out: dict = {
        "input_tokens": _field(breakdown, "input_tokens", 0) or 0,
        "output_tokens": _field(breakdown, "output_tokens", 0) or 0,
    }
    cached = _field(breakdown, "cached_input_tokens", 0) or 0
    if cached:
        out["cache_read_input_tokens"] = cached
    return out


def _codex_usage(notif: Any) -> dict:
    """Normalize a `thread/tokenUsage/updated` notification's `ThreadTokenUsage`
    (`.total` cumulative / `.last`) into the Anthropic-style usage dict. The
    cumulative `total` is the per-run figure ap2's `combined_tokens` reads.
    Returns `{}` when the notification carries no token usage."""
    tu = _field(_codex_payload(notif), "token_usage")
    if tu is None:
        return {}
    breakdown = _field(tu, "total")
    if breakdown is None:
        breakdown = _field(tu, "last")
    if breakdown is None:
        return {}
    return _token_breakdown_to_dict(breakdown)


def _summarize_codex_event(notif: Any) -> dict:
    """Compact per-notification summary in the exact shape
    `ap2.message_dump._summarize_message` produces for a Claude envelope:
    `{type, text_preview?, tool_calls?, usage?}`. Optional fields are omitted
    when absent so the stream stays scannable and so the base `run_to_result`
    only treats usage-bearing notifications as the usage source. Codex does not
    report a per-run cost / turn count / model in its stream, so those keys are
    simply absent (the normalized `AgentUsage` defaults to 0 / "")."""
    out: dict = {"type": _codex_method(notif)}
    text = _extract_codex_text(notif)
    if text:
        out["text_preview"] = _truncate(text, 200)
    item = _codex_item(notif)
    if item is not None:
        tc = _codex_tool_call(item)
        if tc is not None:
            out["tool_calls"] = [tc]
    usage = _codex_usage(notif)
    if usage:
        out["usage"] = usage
    return out


def _serialize_codex_event_full(notif: Any) -> dict:
    """Full-content per-notification record for the `.messages.jsonl` debug dump
    — the codex analogue of `_serialize_message_full`. Pydantic payloads are
    dumped via `model_dump`; dict / stub payloads are projected onto their
    well-known fields."""
    payload = _codex_payload(notif)
    dumped: Any = None
    dump = getattr(payload, "model_dump", None)
    if callable(dump):
        try:
            dumped = dump(mode="json", by_alias=True)
        except Exception:  # noqa: BLE001
            dumped = None
    if dumped is None:
        if isinstance(payload, dict):
            dumped = dict(payload)
        else:
            dumped = {}
            for k in ("item", "token_usage", "turn", "delta", "thread", "error"):
                v = _field(payload, k)
                if v is not None:
                    dumped[k] = v if isinstance(v, (str, int, float, bool)) else str(v)
    return {"method": _codex_method(notif), "payload": dumped}


# --------------------------------------------------------------------------
# Option mapping helpers.
#
# The real `openai_codex` surface splits configuration across two calls:
# `thread_start` configures the conversation, `turn`/`run` configures a single
# turn. The parameter-name sets below are introspected from the installed
# `openai_codex` package; `run()` partitions the normalized kwargs across the
# two calls so we never pass a kwarg a given call does not accept.
# --------------------------------------------------------------------------

#: Kwargs `Codex.thread_start` / `AsyncCodex.thread_start` accept (subset ap2
#: maps onto). The conversation-level configuration surface.
_THREAD_START_PARAMS = frozenset(
    {
        "model",
        "cwd",
        "approval_mode",
        "sandbox",
        "base_instructions",
        "developer_instructions",
        "model_provider",
        "personality",
        "service_tier",
        "config",
    }
)

#: Kwargs `Thread.turn` / `AsyncThread.turn` accept (subset ap2 maps onto). The
#: per-turn configuration surface; `effort` (a `ReasoningEffort`) lives here.
_TURN_PARAMS = frozenset(
    {
        "effort",
        "model",
        "cwd",
        "approval_mode",
        "sandbox",
        "personality",
        "service_tier",
        "summary",
        "output_schema",
    }
)

#: ap2's autonomy-oriented `permission_mode` strings → codex's `ApprovalMode`
#: posture (codex offers only `deny_all` / `auto_review`). Used only as a
#: fallback when the value does not already match an `ApprovalMode` value/name.
_APPROVAL_MODE_FALLBACKS = {
    "bypasspermissions": "auto_review",
    "acceptedits": "auto_review",
    "acceptall": "auto_review",
    "auto": "auto_review",
    "yolo": "auto_review",
    "default": "deny_all",
    "plan": "deny_all",
    "ask": "deny_all",
}


def _resolve_enum(enum_cls: Any, value: Any, fallbacks: dict | None = None) -> Any:
    """Coerce `value` onto the `openai_codex` enum `enum_cls`, or return `None`
    when unresolvable.

    Handles three cases in order: the value is already an `enum_cls` instance;
    the value matches a member by value or name (codex enums are `str, Enum`, so
    `enum_cls(value)` resolves a known value string); or an ap2-semantic
    `fallbacks` map names a member. An unresolvable value yields `None` so the
    caller can drop the kwarg and let the SDK use its own default rather than
    receiving an invalid value.
    """
    if isinstance(value, enum_cls):
        return value
    s = str(_field(value, "value", value))
    try:
        return enum_cls(s)
    except (ValueError, KeyError):
        pass
    by_name = {m.name: m for m in enum_cls}
    if s in by_name:
        return by_name[s]
    if fallbacks:
        target = fallbacks.get(s.lower())
        if target is not None and target in by_name:
            return by_name[target]
    return None


class CodexAdapter(AgentAdapter):
    """`AgentAdapter` implementation driving OpenAI's Codex agent via the real
    `openai_codex` SDK.

    `codex` may be injected (the daemon will pass an already-imported
    `openai_codex` handle; tests pass a stub mirroring the real
    `AsyncCodex` / `thread_start` / `turn` / notification surface). When `None`,
    the module is imported lazily on first use — so this module loads without the
    codex SDK installed and the contract test can inject a stub with no live
    `codex` process.
    """

    backend = "codex"

    def __init__(self, codex: Any = None) -> None:
        self._codex = codex
        #: Short-names of the tools registered by the last `build_tool_server`
        #: call; surfaced via the base `registered_tool_names()` accessor.
        self._registered_tool_names: list[str] = []

    def _get_codex(self) -> Any:
        if self._codex is None:
            # TB-369: resolve the codex handle through the module-level
            # `load_codex_sdk` seam (sibling to `claude_code.load_claude_sdk`)
            # so the adapter and the daemon-start codex-availability gate agree
            # on what "codex is available" means.
            self._codex = load_codex_sdk()
        return self._codex

    def normalize_options(self, options: AgentOptions) -> dict[str, Any]:
        """Map a backend-neutral `AgentOptions` to the real `openai_codex`
        invocation kwargs (`model` / `effort` / `cwd` / `approval_mode`).

        `timeout_s` is intentionally NOT mapped onto the native options: the base
        `run_to_result` owns the per-run timeout via `asyncio.wait_for`, matching
        the Claude adapter. `effort` becomes codex's `effort` (a
        `ReasoningEffort`, coerced from the string by the SDK); `permission_mode`
        becomes codex's `approval_mode` (an `ApprovalMode`, coerced in `run()`).
        `max_turns` is NOT mapped — the real SDK exposes no per-run turn cap.
        `extra` (which may carry a `sandbox` preset) is threaded straight through
        for forward-compatibility. The returned kwargs are partitioned across
        `thread_start` / `turn` by `run()`.
        """
        kwargs: dict[str, Any] = {}
        if options.model is not None:
            kwargs["model"] = options.model
        if options.effort is not None:
            kwargs["effort"] = options.effort
        if options.cwd is not None:
            kwargs["cwd"] = options.cwd
        if options.permission_mode is not None:
            kwargs["approval_mode"] = options.permission_mode
        if options.extra:
            kwargs.update(options.extra)
        return kwargs

    def register_tools(self, tools: AgentTools) -> dict[str, Any]:
        """Map a backend-neutral `AgentTools` to a tool-policy kwargs dict
        (`mcp_servers` / `allowed_tools` / `disallowed_tools`).

        This is the backend-neutral tool *policy* surface the parity suite reads;
        the real `openai_codex` `thread_start` / `turn` calls do not accept these
        kwargs (codex consumes external stdio MCP servers via its own config), so
        `run()` does not thread this onto the SDK calls — the in-process MCP
        delivery to a live Codex agent is configured out-of-band. The
        registration + enumeration surface that axis 7's parity test reads is
        `build_tool_server` / `registered_tool_names`.
        """
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

    def _split_native_kwargs(
        self, codex: Any, options: AgentOptions
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Partition the normalized native kwargs across the SDK's two calls.

        `thread_start` gets the conversation-level kwargs (model / cwd /
        approval_mode / sandbox / ...); `turn` gets the per-turn kwargs (effort,
        plus any turn-only key). A key accepted by both lives on `thread_start`.
        String `approval_mode` / `sandbox` values are coerced onto the real
        `ApprovalMode` / `Sandbox` enums when the handle exposes them; an
        unresolvable value is dropped so the SDK falls back to its default.
        """
        native = self.normalize_options(options)

        approval_cls = getattr(codex, "ApprovalMode", None)
        if approval_cls is not None and "approval_mode" in native:
            resolved = _resolve_enum(
                approval_cls, native["approval_mode"], _APPROVAL_MODE_FALLBACKS
            )
            if resolved is None:
                native.pop("approval_mode", None)
            else:
                native["approval_mode"] = resolved

        sandbox_cls = getattr(codex, "Sandbox", None)
        if sandbox_cls is not None and "sandbox" in native:
            resolved = _resolve_enum(sandbox_cls, native["sandbox"])
            if resolved is None:
                native.pop("sandbox", None)
            else:
                native["sandbox"] = resolved

        thread_kwargs = {k: v for k, v in native.items() if k in _THREAD_START_PARAMS}
        turn_kwargs = {
            k: v
            for k, v in native.items()
            if k in _TURN_PARAMS and k not in thread_kwargs
        }
        return thread_kwargs, turn_kwargs

    async def run(
        self,
        prompt: str,
        tools: AgentTools,
        options: AgentOptions,
    ) -> AsyncIterator[AgentEvent]:
        """Dispatch the run via the real `openai_codex` SDK.

        Async generator: constructs an `AsyncCodex` client, starts a thread
        (`thread_start`, mapping the normalized options), starts a turn
        (`turn`, mapping `effort`), then yields one normalized `AgentEvent` per
        turn `Notification` (parsed through this module's codex-specific summary
        / full / text helpers, which mirror the Claude path's `ap2.message_dump`
        triple), then a terminal `AgentEvent(type="result")` carrying an
        `AgentResult` whose usage is derived from the last token-usage
        notification via the shared `usage_from_summary`. A `turn/completed`
        notification reporting a failed turn yields a `status="error"` result.
        Exceptions from the codex backend propagate to the caller — the base
        `run_to_result` turns them into a `status="error"` result, and a
        `timeout_s` exceedance into `status="timeout"`.

        `tools` is the backend-neutral tool policy; ap2's in-process MCP server
        is delivered to a live Codex agent via codex's own MCP config, not as a
        `thread_start` / `turn` kwarg (the real SDK exposes no allow/deny/
        mcp_servers kwarg), so it is not threaded onto the SDK calls here. The
        `register_tools` / `build_tool_server` methods remain the tool-policy /
        parity surface.
        """
        codex = self._get_codex()
        thread_kwargs, turn_kwargs = self._split_native_kwargs(codex, options)

        final_text = ""
        final_status = "complete"
        final_error: str | None = None
        last_usage_summary: dict | None = None
        last_envelope: Any = None

        client = codex.AsyncCodex()
        try:
            thread = await client.thread_start(**thread_kwargs)
            turn = await thread.turn(prompt, **turn_kwargs)
            async for notif in turn.stream():
                summary = _summarize_codex_event(notif)
                full = _serialize_codex_event_full(notif)
                text = _extract_codex_text(notif)
                if text:
                    final_text = text
                if "usage" in summary or "total_cost_usd" in summary:
                    last_usage_summary = summary
                    last_envelope = notif
                method = _codex_method(notif)
                if method in ("turn/completed", "turn.completed"):
                    turn_obj = _field(_codex_payload(notif), "turn")
                    status = _field(turn_obj, "status")
                    status_val = _field(status, "value", status)
                    if status_val == "failed":
                        err = _field(turn_obj, "error")
                        msg = _field(err, "message") if err is not None else None
                        final_status = "error"
                        final_error = (
                            f"codex turn failed: {msg}" if msg else "codex turn failed"
                        )
                yield AgentEvent(
                    type=summary.get("type") or method,
                    raw=notif,
                    summary=summary,
                    full=full,
                    text=text,
                )
        finally:
            closer = getattr(client, "close", None)
            if callable(closer):
                result = closer()
                if hasattr(result, "__await__"):
                    await result

        yield AgentEvent(
            type="result",
            result=AgentResult(
                status=final_status,
                text=final_text,
                error=final_error,
                usage=usage_from_summary(last_usage_summary),
                raw_result=last_envelope,
            ),
        )

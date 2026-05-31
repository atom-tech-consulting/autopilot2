"""Backend-agnostic agent-dispatch interface (TB-353 / goal.md axis 1).

This module declares the seam every agent run in ap2 will eventually flow
through: the `AgentAdapter` ABC. Today every dispatch site
(`run_task`, `_run_control_agent`, the verifier prose-judge, ideation-scrub,
the validator-judge / janitor-judge component calls) invokes
`claude_agent_sdk.query()` directly. Axis 1 of the **codex support through an
agent adaptor layer** focus introduces the interface so a second backend
(the `CodexAdapter`, axis 4) has a contract to conform to. Per goal.md's
axis-1 delete-test: "if the Claude path isn't behind the interface, the Codex
adapter has no contract to conform to."

This file declares the surface; `ap2/adapters/claude_code.py` lands the first
implementation (`ClaudeCodeAdapter`) wrapping today's `sdk.query()` path
bit-for-bit. No production dispatch site is repointed this task — that is
axis 6, one TB per site — so the full suite proves zero behavior change.

The option / result / usage types here are deliberately minimal and
forward-compatible: axes 2 and 3 harden them (a backend-neutral options
struct, a normalized result/usage record read by the cost guards /
`task_run_usage` emission / `ap2 status`, and MCP-tool exposure through the
adapter). Axis 1 carries only the fields today's Claude path needs.

Design — `AgentAdapter.run()` is the single seam:

  - A caller hands it a `prompt`, an `AgentTools` (the allow/deny tool policy
    plus MCP servers), and a normalized `AgentOptions` (model, effort,
    max_turns, timeout, cwd, permission mode, setting sources, stderr sink).
  - `run()` is an async generator yielding normalized `AgentEvent`s — one per
    backend stream envelope — and a final terminal event of `type="result"`
    whose `.result` is the `AgentResult` (status, text, commit, usage).
    Python async generators cannot `return` a value (PEP 525), so the
    terminal `AgentResult` rides the last yielded event rather than a
    generator return; the concrete `run_to_result()` convenience drains the
    stream and hands the caller just the terminal `AgentResult`, also folding
    in optional timeout / error handling (status `timeout` / `error`) the way
    the daemon's `asyncio.wait_for` wrappers do today.
  - `normalize_options()` is the options-normalization entry (backend-neutral
    options → the backend's native options object's kwargs); `register_tools()`
    maps a pre-built `AgentTools` (allow/deny policy + MCP-server map) onto the
    backend's native tool-exposure kwargs. Both are abstract so each backend
    owns its mapping.
  - `build_tool_server()` (axis 3) is the tool-registration surface: ap2's
    custom tool set is handed to the adapter as a unit and the adapter is
    responsible for exposing it to its backend (the Claude path's
    `create_sdk_mcp_server(...)` assembly now lives here, not at the dispatch
    site). `registered_tool_names()` is the backend-agnostic enumeration
    accessor axis 7's parity test reads to assert both backends register one
    identical toolset.
"""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentOptions:
    """Backend-neutral options for a single agent run.

    Mirrors the knobs the daemon feeds to `ClaudeAgentOptions` today
    (`model` / `effort` (a.k.a. reasoning) / `max_turns` / `cwd` /
    `permission_mode` / `setting_sources` / `stderr`), plus a per-run
    `timeout_s` that the daemon currently applies via `asyncio.wait_for`
    around the consume loop rather than via the SDK options object. `extra`
    is a forward-compatible escape hatch for backend-specific kwargs axes 2-4
    may need; the Claude adapter threads it straight into
    `ClaudeAgentOptions(**...)`.

    Every field is optional so a partial caller (and the contract test) can
    build a minimal options object.

    Axis 2 promotes this struct to the canonical backend-neutral options name
    (`model` / `effort` / `max_turns` / `timeout`) every consumer imports;
    `AgentRunOptions` remains as a back-compat alias for the TB-353 name.
    """

    model: str | None = None
    effort: str | None = None
    max_turns: int | None = None
    timeout_s: float | None = None
    cwd: str | None = None
    permission_mode: str | None = None
    setting_sources: list[str] | None = None
    stderr: Callable[[str], None] | None = None
    extra: dict[str, Any] = field(default_factory=dict)


#: Back-compat alias for the TB-353 name; axis 2 renamed the struct to the
#: canonical `AgentOptions` but existing callers / tests importing
#: `AgentRunOptions` keep resolving to the same dataclass.
AgentRunOptions = AgentOptions


@dataclass
class AgentTools:
    """Backend-neutral tool exposure: the allow/deny policy + MCP servers.

    `allowed` / `disallowed` are the per-site tool policies the daemon passes
    as `allowed_tools` / `disallowed_tools` today; `mcp_servers` is the
    `{"autopilot": <server>}` map carrying ap2's custom MCP tools
    (report_result, cron_propose, pipeline_task_start, the prose judge).

    Axis 3 ("MCP / tool exposure through the adapter") hardens this so both
    backends register the same toolset; axis 1 carries the fields the Claude
    path threads into `ClaudeAgentOptions` today.
    """

    allowed: list[str] | None = None
    disallowed: list[str] | None = None
    mcp_servers: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentUsage:
    """Normalized per-run token / cache / cost / turn record.

    Shape mirrors the payload `daemon._emit_task_run_usage` /
    `_emit_control_run_usage` build from the trailing `ResultMessage`
    envelope: `usage` (input/output/cache token counts), `model_usage` (the
    same broken down by model variant), `total_cost_usd`, `num_turns`, and the
    `model` string. `note` carries `"stream_incomplete"` when no terminal
    result envelope was captured (SDK error / timeout before stream end) —
    the same sentinel the daemon stamps so cross-run aggregators don't
    silently drop the run.

    Axis 2 ("Options + result/usage normalization") promotes this to the one
    shape the cost guards / `task_run_usage` emission / `ap2 status` read
    regardless of backend.
    """

    usage: dict = field(default_factory=dict)
    model_usage: dict = field(default_factory=dict)
    total_cost_usd: float = 0.0
    num_turns: int = 0
    model: str = ""
    note: str = ""

    def event_payload(self) -> dict:
        """The usage portion of a `task_run_usage` / `control_run_usage`
        event payload, in the exact key order the daemon emitted before
        axis 2 (`usage` / `model_usage` / `total_cost_usd` / `num_turns` /
        `model`, then `note` only when set).

        Axis 2 routes `daemon._emit_task_run_usage` /
        `_emit_control_run_usage` through here so the emitted payload is built
        from the one normalized record rather than re-indexing the raw
        SDK-derived summary dict per field. `note` (the `stream_incomplete`
        sentinel) is included only when set — matching the original
        crash-path-only emission so success-path payloads stay key-identical.
        """
        payload: dict = {
            "usage": self.usage,
            "model_usage": self.model_usage,
            "total_cost_usd": self.total_cost_usd,
            "num_turns": self.num_turns,
            "model": self.model,
        }
        if self.note:
            payload["note"] = self.note
        return payload

    @classmethod
    def from_event(cls, event: dict) -> "AgentUsage":
        """Reconstruct a normalized usage record from a persisted
        `task_run_usage` / `control_run_usage` event payload — the inverse of
        `event_payload`.

        The cost guards (`auto_approve._event_combined_tokens`) and the
        `ap2 status` usage read route their per-event token reads through this
        so they consume one normalized shape rather than indexing the raw
        event dict per field; the same accessor will serve the Codex backend
        (axis 4) without per-backend branching.
        """
        return cls(
            usage=event.get("usage") or {},
            model_usage=event.get("model_usage") or {},
            total_cost_usd=event.get("total_cost_usd") or 0.0,
            num_turns=event.get("num_turns") or 0,
            model=event.get("model") or "",
            note=str(event.get("note") or ""),
        )

    @property
    def combined_tokens(self) -> int:
        """`input_tokens + output_tokens` from the normalized `usage` blob —
        the cost-guard / auto-approve / `ap2 status` token-sum read. Robust
        against a missing / non-dict `usage` (returns 0), preserving the
        pre-axis-2 `_event_combined_tokens` shape bit-for-bit.
        """
        u = self.usage if isinstance(self.usage, dict) else {}
        inp = int(u.get("input_tokens", 0) or 0)
        outp = int(u.get("output_tokens", 0) or 0)
        return inp + outp


@dataclass
class AgentResult:
    """Terminal result of an agent run.

    `status` is `"complete"` on a clean stream, `"timeout"` when the run
    exceeded `timeout_s`, or `"error"` on an SDK / subprocess crash — the same
    vocabulary `_run_control_agent` derives today. `text` is the final
    assistant text block (the daemon's `_extract_text` result). `usage` is the
    normalized `AgentUsage`. `commit` is left empty in axis 1 — commit /
    report_result extraction stays in the daemon's `run_task` this task and is
    repointed in axis 6. `error` carries the `"<Type>: <msg>"` string on the
    error path; `raw_result` retains the backend-native terminal envelope for
    callers that still need it during the migration.
    """

    status: str = "unknown"
    text: str = ""
    commit: str = ""
    usage: AgentUsage = field(default_factory=AgentUsage)
    error: str | None = None
    raw_result: Any = None


@dataclass
class AgentEvent:
    """A normalized stream event yielded by `AgentAdapter.run()`.

    One `AgentEvent` is emitted per backend stream envelope, plus a final
    terminal event of `type="result"` whose `.result` is the `AgentResult`.

    - `type`: the backend message class name (`"AssistantMessage"` /
      `"ResultMessage"` / ...) for stream envelopes, or the literal
      `"result"` for the synthesized terminal event.
    - `raw`: the backend-native message object (for callers mid-migration).
    - `summary` / `full`: the compact and full per-envelope dicts the daemon's
      `_summarize_message` / `_serialize_message_full` produce (drives the
      `.stream.jsonl` / `.messages.jsonl` debug dumps).
    - `text`: this envelope's extracted text (`""` when none).
    - `result`: populated only on the terminal `"result"` event.
    """

    type: str
    raw: Any = None
    summary: dict = field(default_factory=dict)
    full: dict = field(default_factory=dict)
    text: str = ""
    result: AgentResult | None = None


class AgentAdapter(ABC):
    """Backend-agnostic agent-dispatch interface (axis 1).

    Concrete adapters (`ClaudeCodeAdapter`, and the future `CodexAdapter`)
    implement `normalize_options`, `register_tools`, and `run`. The
    `run_to_result` convenience is concrete here — it drains `run()`'s stream
    and returns just the terminal `AgentResult`, folding in the optional
    timeout / error handling the daemon applies via `asyncio.wait_for` today.
    """

    #: Short stable backend identifier (`"claude"`, `"codex"`). Used by axis 5
    #: per-agent-kind selection + the auth gate.
    backend: str = ""

    @abstractmethod
    def normalize_options(self, options: AgentOptions) -> dict[str, Any]:
        """Options-normalization entry: map a backend-neutral
        `AgentOptions` to the kwargs the backend's native options object
        accepts. Returns a kwargs dict so `register_tools`'s output can be
        merged before the native options object is constructed."""
        raise NotImplementedError

    @abstractmethod
    def register_tools(self, tools: AgentTools) -> dict[str, Any]:
        """MCP-tool registration hook: map a backend-neutral `AgentTools`
        (allow/deny policy + MCP servers) to the kwargs the backend's native
        options object accepts for tool exposure."""
        raise NotImplementedError

    @abstractmethod
    def build_tool_server(
        self,
        tool_set: Sequence[Any],
        *,
        server_name: str = "autopilot",
        version: str = "unknown",
    ) -> Any:
        """Tool-registration surface (axis 3): accept ap2's custom tool set
        and expose it to the backend.

        `tool_set` is ap2's canonical custom-tool inventory — the
        `report_result` / `cron_propose` / `pipeline_task_start` /
        `operator_queue_append` / ... tools — handed to the adapter as a unit
        rather than assembled at each dispatch site. The adapter wraps the set
        into its backend-native tool-server object (for the Claude backend, a
        `claude_agent_sdk.create_sdk_mcp_server(...)` server) and returns it so
        the dispatch site can thread it through `AgentTools.mcp_servers`.

        Implementations MUST record the registered tool short-names so
        `registered_tool_names()` can enumerate them backend-agnostically —
        axis 4's `CodexAdapter` implements against this same surface and axis
        7's parity test asserts both backends register the identical set.
        """
        raise NotImplementedError

    def registered_tool_names(self) -> list[str]:
        """Backend-agnostic enumeration of the tool short-names most recently
        registered through `build_tool_server()` (e.g. `"report_result"`,
        `"cron_propose"`, `"pipeline_task_start"`).

        Returns an empty list until `build_tool_server()` has run. This is the
        accessor axis 7's cross-backend parity test reads to assert the Claude
        and Codex adapters expose one identical toolset; it is concrete here
        (reading the short-names each `build_tool_server` stashes on
        `self._registered_tool_names`) so every backend enumerates uniformly.
        """
        return list(getattr(self, "_registered_tool_names", []))

    @abstractmethod
    def run(
        self,
        prompt: str,
        tools: AgentTools,
        options: AgentOptions,
    ) -> AsyncIterator[AgentEvent]:
        """Dispatch one agent run.

        Async generator: yields one `AgentEvent` per backend stream envelope,
        then a final terminal `AgentEvent(type="result")` carrying the
        `AgentResult`. Exceptions from the underlying backend propagate to the
        caller (mirroring the daemon's bare `sdk.query()` consume loop);
        `run_to_result` is where they become an `error`-status result.
        """
        raise NotImplementedError

    async def run_to_result(
        self,
        prompt: str,
        tools: AgentTools,
        options: AgentOptions,
    ) -> AgentResult:
        """Drain `run()` and return the terminal `AgentResult`.

        Folds in the timeout / error handling the daemon applies via
        `asyncio.wait_for` today: when `options.timeout_s` is set the drain is
        bounded and a `TimeoutError` yields a `status="timeout"` result; any
        other exception yields `status="error"` with the `"<Type>: <msg>"`
        string in `.error`. On a clean stream the terminal event's
        pre-built `AgentResult` is returned verbatim. Failure-path usage is
        rebuilt from the last usage-bearing envelope seen before the fault —
        the same `stream_incomplete` fallback the daemon uses.
        """
        final_text = ""
        last_usage_summary: dict | None = None
        terminal: AgentResult | None = None

        async def _drain() -> None:
            nonlocal final_text, last_usage_summary, terminal
            async for ev in self.run(prompt, tools, options):
                if ev.result is not None:
                    terminal = ev.result
                if ev.text:
                    final_text = ev.text
                if ev.summary and (
                    "usage" in ev.summary or "total_cost_usd" in ev.summary
                ):
                    last_usage_summary = ev.summary

        timed_out = False
        error: str | None = None
        try:
            if options.timeout_s is not None:
                await asyncio.wait_for(_drain(), timeout=options.timeout_s)
            else:
                await _drain()
        except asyncio.TimeoutError:
            timed_out = True
        except Exception as e:  # noqa: BLE001
            error = f"{type(e).__name__}: {e}"

        if not timed_out and error is None and terminal is not None:
            return terminal

        status = "timeout" if timed_out else ("error" if error else "complete")
        return AgentResult(
            status=status,
            text=final_text,
            usage=usage_from_summary(last_usage_summary),
            error=error,
        )


def usage_from_summary(summary: dict | None) -> AgentUsage:
    """Build a normalized `AgentUsage` from a `_summarize_message` dict.

    Mirrors `daemon._emit_task_run_usage` / `_emit_control_run_usage`
    bit-for-bit: read `usage` / `model_usage` / `total_cost_usd` / `num_turns`
    / `model` off the last usage-bearing envelope, falling back to the
    `stream_incomplete` sentinel (empty usage, zero cost / turns) when no such
    envelope was captured.
    """
    if summary is None:
        return AgentUsage(note="stream_incomplete")
    return AgentUsage(
        usage=summary.get("usage") or {},
        model_usage=summary.get("model_usage") or {},
        total_cost_usd=summary.get("total_cost_usd") or 0.0,
        num_turns=summary.get("num_turns") or 0,
        model=summary.get("model") or "",
    )

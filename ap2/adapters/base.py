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
    plus MCP servers), and a normalized `AgentRunOptions` (model, effort,
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
    is the MCP-tool registration hook (ap2's tool surface → the backend's
    native tool config). Both are abstract so each backend owns its mapping.
"""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentRunOptions:
    """Backend-neutral options for a single agent run (axis 1 subset).

    Mirrors the knobs the daemon feeds to `ClaudeAgentOptions` today
    (`model` / `effort` / `max_turns` / `cwd` / `permission_mode` /
    `setting_sources` / `stderr`), plus a per-run `timeout_s` that the daemon
    currently applies via `asyncio.wait_for` around the consume loop rather
    than via the SDK options object. `extra` is a forward-compatible escape
    hatch for backend-specific kwargs axes 2-4 may need; the Claude adapter
    threads it straight into `ClaudeAgentOptions(**...)`.

    Every field is optional so a partial caller (and the axis-1 contract
    test) can build a minimal options object; axes 2 and 3 harden this struct
    once real callers are repointed.
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
    def normalize_options(self, options: AgentRunOptions) -> dict[str, Any]:
        """Options-normalization entry: map a backend-neutral
        `AgentRunOptions` to the kwargs the backend's native options object
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
    def run(
        self,
        prompt: str,
        tools: AgentTools,
        options: AgentRunOptions,
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
        options: AgentRunOptions,
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

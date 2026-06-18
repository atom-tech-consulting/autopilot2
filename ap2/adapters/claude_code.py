"""`ClaudeCodeAdapter` — the Claude backend behind the `AgentAdapter` seam
(TB-353 / goal.md axis 1).

Relocates today's `claude_agent_sdk.query()` dispatch path behind the
`AgentAdapter` interface, bit-for-bit. This is the behavior reference: it must
reproduce the daemon's existing consume loop exactly so the axis-7 parity
tests have a ground truth, and so axes 2-6 (options/result normalization,
MCP-tool exposure, the CodexAdapter, per-kind selection, and the one-TB-each
dispatch-site migrations) build against a faithful Claude path.

What it preserves from `daemon.run_task` / `_run_control_agent`:

  - Options mapping: `model` / `effort` (`extra_args={"effort": ...}`) /
    `max_turns` / `cwd` / `permission_mode` / `setting_sources` / `stderr` →
    `ClaudeAgentOptions`, and `mcp_servers` / `allowed_tools` /
    `disallowed_tools` for tool exposure.
  - Stream parsing: each `AssistantMessage` / `ResultMessage` envelope is run
    through the SAME `ap2.message_dump` helpers the daemon uses
    (`_summarize_message`, `_serialize_message_full`, `_extract_text`) — so
    the normalized `AgentEvent.summary` / `.full` / `.text` are identical to
    the daemon's `.stream.jsonl` / `.messages.jsonl` rows.
  - Usage / cost: derived from the trailing usage-bearing envelope via the
    shared `usage_from_summary`, matching `_emit_task_run_usage`'s walk.

No production dispatch site is repointed to this adapter in axis 1 — the
daemon keeps its direct `sdk.query()` path. That migration is axis 6, one TB
per site, so the full suite proves zero behavior change this task.

The `claude_agent_sdk` import is lazy (inside `_get_sdk`) so the module loads
without the SDK installed and the contract test can inject a stub `sdk` — the
production daemon passes its already-imported `claude_agent_sdk` module in.
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

from ..message_dump import (
    _extract_text,
    _serialize_message_full,
    _summarize_message,
)
from .base import (
    AgentAdapter,
    AgentEvent,
    AgentOptions,
    AgentResult,
    AgentTools,
    usage_from_summary,
)


def load_claude_sdk() -> Any:
    """Import and return the `claude_agent_sdk` module.

    TB-366: the single relocation point for the residual
    `import claude_agent_sdk` statements that used to live across non-adapter
    source (`daemon.py`'s startup availability gate + the `sdk` handle it
    threads as the injected-test seam, `validator_judge/impl.py`'s hermetic
    fake-SDK capture). Routing those through this helper keeps
    `claude_agent_sdk` imported only inside `ap2/adapters/`, which the
    import-direction gate (`test_sdk_import_boundary.py`) pins.

    The import is resolved at call time against `sys.modules`, so a test that
    installs a fake `claude_agent_sdk` module (via
    `monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake)`) still has
    its fake picked up here — the injected-SDK seam is preserved bit-for-bit,
    just relocated behind the adapter boundary. The import is lazy (inside the
    function body) so importing `ap2.adapters` does not require the SDK to be
    installed.
    """
    import claude_agent_sdk as sdk  # type: ignore

    return sdk


class ClaudeCodeAdapter(AgentAdapter):
    """`AgentAdapter` implementation driving the bundled Claude Code binary
    via `claude_agent_sdk.query()`.

    `sdk` may be injected (the daemon passes its already-imported
    `claude_agent_sdk` module; tests pass a stub exposing `ClaudeAgentOptions`
    + `query`). When `None`, the module is imported lazily on first use.
    """

    backend = "claude"

    #: TB-419 provider default model tiers. HEAVY (`claude-opus-4-8`) backs the
    #: primary Claude agents (task / ideation / cron / status_report /
    #: mattermost dispatch when `agent_model` is unset); LIGHT
    #: (`claude-sonnet-4-6`) backs the cost-sensitive sub-calls (the validator
    #: judge, the ideation scrub).
    default_model_heavy = "claude-opus-4-8"
    default_model_light = "claude-sonnet-4-6"

    def __init__(self, sdk: Any = None) -> None:
        self._sdk = sdk
        #: Short-names of the tools registered by the last `build_tool_server`
        #: call; surfaced via the base `registered_tool_names()` accessor.
        self._registered_tool_names: list[str] = []

    def _get_sdk(self) -> Any:
        if self._sdk is None:
            self._sdk = load_claude_sdk()
        return self._sdk

    def normalize_options(self, options: AgentOptions) -> dict[str, Any]:
        """Map a backend-neutral `AgentOptions` to `ClaudeAgentOptions`
        kwargs.

        `timeout_s` is intentionally NOT mapped onto `ClaudeAgentOptions`:
        today's daemon applies the per-run timeout via `asyncio.wait_for`
        around the consume loop, not via the SDK options object, so the base
        class's `run_to_result` owns it. `effort` becomes
        `extra_args={"effort": ...}` exactly as the daemon builds it. `extra`
        is threaded straight through for forward-compatibility.
        """
        kwargs: dict[str, Any] = {}
        if options.cwd is not None:
            kwargs["cwd"] = options.cwd
        if options.permission_mode is not None:
            kwargs["permission_mode"] = options.permission_mode
        if options.max_turns is not None:
            kwargs["max_turns"] = options.max_turns
        if options.setting_sources is not None:
            kwargs["setting_sources"] = options.setting_sources
        if options.stderr is not None:
            kwargs["stderr"] = options.stderr
        if options.model is not None:
            kwargs["model"] = options.model
        if options.effort is not None:
            kwargs["extra_args"] = {"effort": options.effort}
        if options.extra:
            kwargs.update(options.extra)
        return kwargs

    def register_tools(self, tools: AgentTools) -> dict[str, Any]:
        """Map a backend-neutral `AgentTools` to the tool-exposure kwargs of
        `ClaudeAgentOptions` (`mcp_servers` / `allowed_tools` /
        `disallowed_tools`). The `{"autopilot": <server>}` MCP map carries
        ap2's custom tools verbatim."""
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
        """Expose ap2's custom tool set to the Claude backend (axis 3).

        Relocates — bit-for-bit — the `create_sdk_mcp_server(...)` assembly
        that lived at the dispatch wiring (`ap2.tools.build_mcp_server`) behind
        the adapter so Claude tool exposure flows through the `AgentAdapter`
        surface rather than being assembled per dispatch site. Each member of
        `tool_set` is an `SdkMcpTool` (a `@tool`-decorated handler closure over
        the daemon's `Config`); they are passed verbatim to
        `create_sdk_mcp_server` so the live tool inventory is unchanged. The
        registered short-names (read off each tool's `.name`) are captured for
        the base `registered_tool_names()` enumeration accessor.

        The `claude_agent_sdk` import is lazy (mirroring the original wiring's
        `from claude_agent_sdk import create_sdk_mcp_server`) so the module
        loads without the SDK installed.
        """
        from claude_agent_sdk import create_sdk_mcp_server  # type: ignore

        tools_list = list(tool_set)
        self._registered_tool_names = [
            name
            for name in (getattr(t, "name", None) for t in tools_list)
            if name
        ]
        return create_sdk_mcp_server(
            name=server_name,
            version=version,
            tools=tools_list,
        )

    async def run(
        self,
        prompt: str,
        tools: AgentTools,
        options: AgentOptions,
    ) -> AsyncIterator[AgentEvent]:
        """Dispatch the run via `claude_agent_sdk.query()`.

        Yields one normalized `AgentEvent` per SDK envelope (parsed through the
        daemon's shared `ap2.message_dump` helpers), then a terminal
        `AgentEvent(type="result")` carrying a `status="complete"`
        `AgentResult` whose usage is derived from the last usage-bearing
        envelope. Exceptions from the SDK propagate to the caller — the base
        `run_to_result` turns them into a `status="error"` result.
        """
        sdk = self._get_sdk()
        opt_kwargs = {
            **self.normalize_options(options),
            **self.register_tools(tools),
        }
        sdk_options = sdk.ClaudeAgentOptions(**opt_kwargs)

        final_text = ""
        last_usage_summary: dict | None = None
        last_result_msg: Any = None

        async for msg in sdk.query(prompt=prompt, options=sdk_options):
            summary = _summarize_message(msg)
            full = _serialize_message_full(msg)
            text = _extract_text(msg)
            if text:
                final_text = text
            if "usage" in summary or "total_cost_usd" in summary:
                last_usage_summary = summary
                last_result_msg = msg
            yield AgentEvent(
                type=summary.get("type") or type(msg).__name__,
                raw=msg,
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
                raw_result=last_result_msg,
            ),
        )

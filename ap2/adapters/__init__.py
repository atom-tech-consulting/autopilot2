"""Backend-agnostic agent-adapter package (TB-353 / goal.md axis 1).

Re-exports the `AgentAdapter` ABC, the first concrete implementation
(`ClaudeCodeAdapter`), and the option / tool / result / usage / event types
so callers do a single `from ap2.adapters import AgentAdapter,
ClaudeCodeAdapter, AgentOptions, AgentTools, AgentResult` rather than
reaching into the submodules. Axes 2-7 extend this surface (the
`CodexAdapter`, per-kind selection, parity tests) against the same package.

Axis 2 renamed the options struct to the canonical `AgentOptions`;
`AgentRunOptions` stays exported as a back-compat alias for the TB-353 name.
Axis 4 (TB-357) adds the second backend, `CodexAdapter`, against the same
interface.
"""
from __future__ import annotations

from .base import (
    AgentAdapter,
    AgentEvent,
    AgentOptions,
    AgentResult,
    AgentRunOptions,
    AgentTools,
    AgentUsage,
    usage_from_summary,
)
from .claude_code import ClaudeCodeAdapter, load_claude_sdk
from .codex import CodexAdapter
from .select import AGENT_KINDS, referenced_backends, select_adapter

__all__ = [
    "AGENT_KINDS",
    "AgentAdapter",
    "AgentEvent",
    "AgentOptions",
    "AgentResult",
    "AgentRunOptions",
    "AgentTools",
    "AgentUsage",
    "ClaudeCodeAdapter",
    "CodexAdapter",
    "load_claude_sdk",
    "referenced_backends",
    "select_adapter",
    "usage_from_summary",
]


def __getattr__(name: str):
    """Lazy re-export of the Claude SDK's `tool` decorator (TB-366).

    `tools.build_mcp_server` defines ap2's custom MCP tools with the SDK's
    `@tool` schema decorator. To keep `claude_agent_sdk` imported only inside
    `ap2/adapters/` (the import-direction gate pinned by
    `test_sdk_import_boundary.py`), `tools.py` now does
    `from ap2.adapters import tool` rather than importing it from
    `claude_agent_sdk` directly. PEP 562 module `__getattr__` makes the
    re-export lazy — `claude_agent_sdk` is imported only when `tool` is first
    accessed (at `build_mcp_server` call time, which already requires the
    SDK), so importing `ap2.adapters` for adapter selection / options
    normalization does not pull the SDK.
    """
    if name == "tool":
        from claude_agent_sdk import tool as _tool  # type: ignore

        return _tool
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

"""Backend-agnostic agent-adapter package (TB-353 / goal.md axis 1).

Re-exports the `AgentAdapter` ABC, the first concrete implementation
(`ClaudeCodeAdapter`), and the option / tool / result / usage / event types
so callers do a single `from ap2.adapters import AgentAdapter,
ClaudeCodeAdapter, AgentRunOptions, AgentTools, AgentResult` rather than
reaching into the submodules. Axes 2-7 extend this surface (the
`CodexAdapter`, per-kind selection, parity tests) against the same package.
"""
from __future__ import annotations

from .base import (
    AgentAdapter,
    AgentEvent,
    AgentResult,
    AgentRunOptions,
    AgentTools,
    AgentUsage,
    usage_from_summary,
)
from .claude_code import ClaudeCodeAdapter

__all__ = [
    "AgentAdapter",
    "AgentEvent",
    "AgentResult",
    "AgentRunOptions",
    "AgentTools",
    "AgentUsage",
    "ClaudeCodeAdapter",
    "usage_from_summary",
]

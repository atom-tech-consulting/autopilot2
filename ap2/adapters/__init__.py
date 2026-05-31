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
from .claude_code import ClaudeCodeAdapter
from .codex import CodexAdapter

__all__ = [
    "AgentAdapter",
    "AgentEvent",
    "AgentOptions",
    "AgentResult",
    "AgentRunOptions",
    "AgentTools",
    "AgentUsage",
    "ClaudeCodeAdapter",
    "CodexAdapter",
    "usage_from_summary",
]

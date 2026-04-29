"""TaskResult dataclass — the daemon's structured view of a task agent's
completion signal.

Pre-TB-104 this module also held the `RESULT:` text-block parser that
extracted these fields from the agent's final message via regex. Post-TB-104
the canonical signal is the `report_result` MCP tool call (TB-101); the
daemon synthesizes a TaskResult directly from the tool's args dict via
`daemon._task_result_from_tool_args`. The dataclass stays here as the
shared shape between that synthesizer and the rest of the daemon.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TaskResult:
    status: str = "unknown"
    commit: str = ""
    summary: str = ""
    files_changed: list[str] | None = None
    tests_passed: bool | None = None
    cron: list[dict] = field(default_factory=list)
    raw: str = ""

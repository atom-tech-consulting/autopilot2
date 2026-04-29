"""MCP tool inventory check (TB-103-style: catches the class of bug fixed in
737d2ce — tool decorated but not added to `create_sdk_mcp_server(tools=[...])`).

Builds the autopilot MCP server, invokes its `ListToolsRequest` handler in
process, and asserts every advertised tool corresponds to a TASK_ or CONTROL_
agent allowlist entry — and vice versa. No real SDK; just the in-process MCP
plumbing the daemon already exercises every tick.
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path


def _build_server():
    from ap2.config import Config
    from ap2.tools import build_mcp_server

    tmp = tempfile.mkdtemp()
    root = Path(tmp)
    (root / "TASKS.md").write_text(
        "# Tasks\n\n## Active\n\n## Ready\n\n## Backlog\n\n## Complete\n\n## Frozen\n"
    )
    cfg = Config.load(root)
    cfg.ensure_dirs()
    return build_mcp_server(cfg)


async def _list_advertised_tool_names(srv: dict) -> list[str]:
    from mcp import types as mcp_types

    instance = srv["instance"]
    handler = instance.request_handlers[mcp_types.ListToolsRequest]
    result = await handler(mcp_types.ListToolsRequest(method="tools/list"))
    return [t.name for t in result.root.tools]


def test_advertised_tools_match_agent_allowlists():
    """Every tool the MCP server advertises must show up in either
    TASK_AGENT_TOOLS or CONTROL_AGENT_TOOLS (with the `mcp__autopilot__`
    prefix Claude Code applies). The reverse direction also holds: every
    `mcp__autopilot__<tool>` entry in the allowlists must be backed by a
    real advertised tool — that's the load-bearing check that catches the
    "decorated but not registered" bug.
    """
    from ap2.tools import CONTROL_AGENT_TOOLS, TASK_AGENT_TOOLS

    srv = _build_server()
    advertised = set(asyncio.run(_list_advertised_tool_names(srv)))
    advertised_prefixed = {f"mcp__autopilot__{n}" for n in advertised}

    union_allowlists = set(TASK_AGENT_TOOLS) | set(CONTROL_AGENT_TOOLS)
    mcp_in_allowlists = {t for t in union_allowlists if t.startswith("mcp__autopilot__")}

    # Forward direction: every advertised tool is in some allowlist.
    missing_from_allowlists = advertised_prefixed - union_allowlists
    assert not missing_from_allowlists, (
        f"MCP server advertises tools that no agent can call: "
        f"{sorted(missing_from_allowlists)}"
    )
    # Reverse direction (the load-bearing one): every allowlisted tool is
    # actually advertised. Catches the 737d2ce bug — agent allowlist names
    # the tool but build_mcp_server forgot to include it in tools=[...].
    missing_from_server = mcp_in_allowlists - advertised_prefixed
    assert not missing_from_server, (
        f"Agent allowlists name tools the MCP server doesn't advertise: "
        f"{sorted(missing_from_server)} — likely missing from "
        f"create_sdk_mcp_server's tools=[...] list in build_mcp_server."
    )


def test_each_advertised_tool_has_a_string_schema():
    """SDK-compat sanity check: every tool's inputSchema declares fields as
    JSON-schema-compatible types (not raw Python `bool` / `list` classes,
    which the smoke for TB-101 showed correlate with deferred-tool surfacing
    issues). Each declared field should resolve to a known JSON Schema type."""
    from mcp import types as mcp_types

    srv = _build_server()
    instance = srv["instance"]
    handler = instance.request_handlers[mcp_types.ListToolsRequest]
    result = asyncio.run(handler(mcp_types.ListToolsRequest(method="tools/list")))
    for tool in result.root.tools:
        schema = getattr(tool, "inputSchema", None)
        assert schema is not None, f"tool {tool.name!r} has no inputSchema"
        # JSON-schema shape: top-level is dict with "type": "object" + "properties".
        assert schema.get("type") == "object", (
            f"tool {tool.name!r} schema.type should be 'object', "
            f"got {schema.get('type')!r}"
        )
        props = schema.get("properties") or {}
        for field_name, field_schema in props.items():
            assert "type" in field_schema, (
                f"tool {tool.name!r} field {field_name!r} has no type: "
                f"{field_schema!r}"
            )

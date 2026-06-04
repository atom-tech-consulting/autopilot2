"""Standalone stdio MCP server entrypoint for ap2's custom tool set (TB-373).

Why this exists — goal.md axis 3 ("ap2's custom tools … register through the
adapter so both backends see the same toolset. Delete-test: if tools stay
Claude-MCP-specific, a Codex agent can't report results and the loop breaks"):

ap2 delivers its tools to Claude as an *in-process* SDK MCP server
(`tools.build_mcp_server` → `ClaudeCodeAdapter.build_tool_server` →
`create_sdk_mcp_server`), handed to the Claude SDK via
`ClaudeAgentOptions(mcp_servers={...})`. The real `openai_codex` SDK exposes no
in-process `mcp_servers` kwarg — codex consumes *external stdio* MCP servers
declared in its own session config. So a live codex agent cannot see
`report_result` (and therefore cannot complete an ap2 task) unless ap2's tools
are also reachable over stdio.

This module is that second transport. Run as

    python -m ap2.mcp_stdio --project <root>

it builds the SAME tool set as `ap2/tools.py:build_mcp_server` — by calling the
shared `tools.build_tool_set(cfg)`, the single source of truth for ap2's custom
tool definitions and their input schemas — and serves it over the official `mcp`
package's stdio transport. Level 1: the proven in-process Claude path is
untouched; only the *assembly* differs (in-process `create_sdk_mcp_server` for
Claude vs this `mcp` package stdio `Server` for codex). The tool *handlers*
(`do_task_complete` behind `report_result`, `do_cron_propose`,
`do_pipeline_task_start`, the git-log / log-event / status-report handlers, …)
are re-wrapped, never forked.

`report_result`'s handler is a thin ack (`tools.do_task_complete`: "the daemon
owns the routing decision after the query returns", reading the call off the
agent event stream), so this bridge only has to advertise the catalog and ack
the call. The daemon's codex stream-walk (`daemon.run_task` →
`adapters.codex.codex_tool_call_payload`) captures the real `report_result`
payload from the codex `mcpToolCall` event and turns it into a `TaskResult`.
"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import Any

from .config import Config
from .tools import build_tool_set


def _input_schema_to_json_schema(input_schema: Any) -> dict:
    """Convert an `SdkMcpTool.input_schema` — the flat ``{field: pytype}`` dict
    ap2's `@tool` definitions use — into a JSON Schema object.

    Mirrors `claude_agent_sdk.create_sdk_mcp_server`'s own `_build_schema` so the
    stdio transport advertises the IDENTICAL inputSchema the in-process Claude
    server does (a dict already shaped as a full JSON Schema — carrying
    ``type``/``properties`` — is returned unchanged). Every ap2 tool schema is a
    flat map of ``str``/``int``/``list``-typed fields, so a small type table
    suffices; this is a generic type→schema projection, not a re-declaration of
    any tool's fields.
    """
    if not isinstance(input_schema, dict):
        return {"type": "object", "properties": {}}
    if isinstance(input_schema.get("type"), str) and "properties" in input_schema:
        return input_schema
    py_to_json = {
        str: "string",
        int: "integer",
        float: "number",
        bool: "boolean",
        list: "array",
        dict: "object",
    }
    properties = {
        name: {"type": py_to_json.get(pytype, "string")}
        for name, pytype in input_schema.items()
    }
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties.keys()),
    }


def tool_short_names(cfg: Config) -> list[str]:
    """The tool short-names this stdio server advertises — exactly
    `build_tool_set(cfg)`'s, i.e. the same set `tools.build_mcp_server`
    registers. The hermetic test asserts this equals the in-process server's
    short-name set (single source of truth)."""
    return [t.name for t in build_tool_set(cfg)]


async def dispatch_tool_call(
    tool_map: dict, name: str, arguments: dict | None
) -> Any:
    """Run one ap2 tool by short-name and return its result as an `mcp`
    `CallToolResult`.

    The tool's neutral handler (e.g. `tools.do_task_complete` behind
    `report_result`) returns the SAME ``{"content": [...], "isError"?: bool}``
    ack dict the in-process Claude path returns; this re-wraps that ack into the
    stdio transport's `CallToolResult` verbatim — same ack, different transport.
    """
    from mcp import types

    tool_def = tool_map.get(name)
    if tool_def is None:
        return types.CallToolResult(
            content=[
                types.TextContent(type="text", text=f"ERROR: tool {name!r} not found")
            ],
            isError=True,
        )
    ack = await tool_def.handler(arguments or {})
    content: list[Any] = []
    for item in (ack.get("content") or []):
        if isinstance(item, dict) and item.get("type") == "text":
            content.append(types.TextContent(type="text", text=item.get("text", "")))
    return types.CallToolResult(content=content, isError=bool(ack.get("isError", False)))


def build_stdio_server(cfg: Config) -> Any:
    """Build an `mcp` package low-level `Server` advertising ap2's custom tool
    set over stdio.

    Single source of truth: the tool objects come from `tools.build_tool_set`
    (the same list `tools.build_mcp_server` hands the Claude adapter), so there
    is exactly one definition of each tool, its input schema, and its handler.
    `list_tools` advertises the catalog (name + description + the same
    inputSchema the in-process server exposes); `call_tool` dispatches to each
    tool's own neutral handler closure and returns its ack — the same ack the
    in-process Claude server returns.
    """
    from mcp import types
    from mcp.server.lowlevel import Server

    tool_set = build_tool_set(cfg)
    tool_map = {t.name: t for t in tool_set}
    server: Any = Server("autopilot", version=_server_version())

    @server.list_tools()
    async def _list_tools() -> list:
        return [
            types.Tool(
                name=t.name,
                description=t.description,
                inputSchema=_input_schema_to_json_schema(t.input_schema),
            )
            for t in tool_set
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict) -> Any:
        return await dispatch_tool_call(tool_map, name, arguments)

    return server


def _server_version() -> str:
    from .tools import _mcp_server_version

    return _mcp_server_version()


async def serve(cfg: Config) -> None:
    """Serve ap2's tool set over stdio until the client (the codex agent)
    disconnects."""
    from mcp.server.stdio import stdio_server

    server = build_stdio_server(cfg)
    init_options = server.create_initialization_options()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, init_options)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m ap2.mcp_stdio",
        description=(
            "Serve ap2's custom tool set over a stdio MCP transport so an "
            "external codex agent can call report_result (and the rest of the "
            "ap2 toolset)."
        ),
    )
    parser.add_argument(
        "--project",
        required=True,
        help="Project root (the directory holding .cc-autopilot/).",
    )
    args = parser.parse_args(argv)
    cfg = Config.load(Path(args.project))
    asyncio.run(serve(cfg))


if __name__ == "__main__":
    main()

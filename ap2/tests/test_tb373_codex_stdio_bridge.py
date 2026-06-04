"""TB-373 / goal.md axis 3 (Level 1) — deliver ap2's toolset to a live codex
agent over stdio MCP so a codex `task` agent can call `report_result`.

ap2 delivers its tools to Claude as an *in-process* SDK MCP server
(`tools.build_mcp_server`). The real `openai_codex` SDK has no in-process
`mcp_servers` kwarg — codex consumes EXTERNAL stdio MCP servers via its own
config — so a live codex agent could not see `report_result`, which is the
axis-3 delete-test failure ("if tools stay Claude-MCP-specific, a Codex agent
can't report results and the loop breaks").

This suite pins the Level-1 fix, hermetically (no network / credentials / live
codex):

  (a) the stdio bridge (`ap2/mcp_stdio.py`) advertises the IDENTICAL tool
      short-name set as `tools.build_mcp_server` — single source of truth, the
      shared `tools.build_tool_set` — both as the module accessor and through
      the actual `mcp` `Server`'s `list_tools` handler;
  (b) a `report_result` call exercised THROUGH the stdio server's `call_tool`
      handler returns the same ack the in-process handler (`do_task_complete`)
      returns;
  (c) the `CodexAdapter` registers the stdio bridge as an external MCP server in
      the live codex thread's session config (the `python -m ap2.mcp_stdio`
      launch command), wired through `register_tools` / `build_tool_server`;
  (d) the daemon builds a complete `TaskResult` from a codex-shaped
      `mcpToolCall` event carrying `report_result` args — both via the
      adapter's `codex_tool_call_payload` + `_task_result_from_tool_args`
      composition AND end-to-end through `run_task` driving a stubbed codex
      backend.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from ap2.adapters import AgentOptions, AgentTools, ClaudeCodeAdapter, CodexAdapter
from ap2.adapters.codex import codex_tool_call_payload

# Reuse the codex suite's hermetic stub + notification builders and the Claude
# suite's project scaffold so this suite never drifts from the contract those
# pin.
from ap2.tests.test_codex_adapter import (
    FakeCodex,
    _agent_message as _codex_agent_message,
    _mcp_tool_call as _codex_mcp_tool_call,
    _scaffold_cfg,
    _token_usage as _codex_token_usage,
    _turn_completed as _codex_turn_completed,
    _turn_started as _codex_turn_started,
)


# --------------------------------------------------------------------------
# (a) The stdio bridge advertises the SAME tool short-name set as the
# in-process server — single source of truth.
# --------------------------------------------------------------------------


def _canonical_tool_names(cfg) -> set[str]:
    """The canonical ap2 tool short-name set, as the in-process Claude server
    (`tools.build_mcp_server`) registers it."""
    import ap2.tools as tools

    claude = ClaudeCodeAdapter()
    tools.build_mcp_server(cfg, adapter=claude)
    return set(claude.registered_tool_names())


def test_stdio_tool_short_names_equal_in_process_catalog(tmp_path):
    from ap2.mcp_stdio import tool_short_names

    cfg = _scaffold_cfg(tmp_path)
    assert set(tool_short_names(cfg)) == _canonical_tool_names(cfg)


def test_stdio_server_list_tools_equals_in_process_catalog(tmp_path):
    """Exercise the ACTUAL `mcp` `Server`'s `list_tools` handler (not just the
    accessor) and assert it advertises exactly the in-process catalog."""
    from mcp import types

    from ap2.mcp_stdio import build_stdio_server

    cfg = _scaffold_cfg(tmp_path)
    server = build_stdio_server(cfg)
    handler = server.request_handlers[types.ListToolsRequest]
    server_result = asyncio.run(handler(types.ListToolsRequest(method="tools/list")))
    advertised = {t.name for t in server_result.root.tools}
    assert advertised == _canonical_tool_names(cfg)
    # report_result is specifically present — the call a codex task agent makes.
    assert "report_result" in advertised


# --------------------------------------------------------------------------
# (b) A report_result call through the stdio server acks identically to the
# in-process handler.
# --------------------------------------------------------------------------


def _call_through_stdio(server, name: str, arguments: dict):
    from mcp import types

    handler = server.request_handlers[types.CallToolRequest]
    req = types.CallToolRequest(
        method="tools/call",
        params=types.CallToolRequestParams(name=name, arguments=arguments),
    )
    return asyncio.run(handler(req)).root  # CallToolResult


def test_report_result_through_stdio_matches_in_process_ack(tmp_path):
    import ap2.tools as tools

    cfg = _scaffold_cfg(tmp_path)
    args = {
        "status": "complete",
        "commit": "c0dexbeef",
        "summary": "did it via codex",
        "files_changed": "x.py,y.py",
        "tests_passed": "true",
    }

    # In-process handler ack (the thin `do_task_complete` ack the Claude path
    # returns).
    in_proc = tools.do_task_complete(cfg, args)
    expected = json.loads(in_proc["content"][0]["text"])

    # Same call, exercised through the stdio server's call_tool dispatch.
    from ap2.mcp_stdio import build_stdio_server

    result = _call_through_stdio(build_stdio_server(cfg), "report_result", args)
    assert result.isError is False
    assert json.loads(result.content[0].text) == expected


# --------------------------------------------------------------------------
# (c) CodexAdapter registers the stdio bridge in the live thread's config.
# --------------------------------------------------------------------------


def test_codex_run_registers_stdio_bridge_in_thread_config(tmp_path):
    """`CodexAdapter.run` declares ap2's stdio bridge as an external MCP server
    in `thread_start(config=...)`, launched as `python -m ap2.mcp_stdio
    --project <cwd>` — the real `openai_codex` config surface, no fabricated
    symbol."""
    import sys

    codex = FakeCodex([_codex_turn_completed()])
    adapter = CodexAdapter(codex=codex)
    tools = AgentTools(allowed=["Bash"], mcp_servers={"autopilot": object()})
    opts = AgentOptions(model="gpt-5-codex", cwd=str(tmp_path))

    asyncio.run(adapter.run_to_result("p", tools, opts))

    config = codex.captured["thread_start"]["config"]
    servers = config["mcp_servers"]
    assert "autopilot" in servers
    entry = servers["autopilot"]
    assert entry["command"] == sys.executable
    assert entry["args"] == ["-m", "ap2.mcp_stdio", "--project", str(tmp_path)]


def test_codex_external_mcp_config_empty_without_mcp_servers(tmp_path):
    """No MCP server in the tool policy → no `config` injected (the SDK uses its
    own default)."""
    codex = FakeCodex([_codex_turn_completed()])
    adapter = CodexAdapter(codex=codex)
    tools = AgentTools(allowed=["Bash"], mcp_servers={})
    asyncio.run(
        adapter.run_to_result("p", tools, AgentOptions(model="m", cwd=str(tmp_path)))
    )
    assert "config" not in codex.captured["thread_start"]


# --------------------------------------------------------------------------
# (d) The daemon builds a complete TaskResult from a codex mcpToolCall event.
# --------------------------------------------------------------------------


def test_codex_tool_call_payload_extracts_full_report_result_args():
    from ap2.daemon import _task_result_from_tool_args

    args = {
        "status": "complete",
        "commit": "c0dexbeef",
        "summary": "did it via codex",
        "files_changed": "x.py,y.py",
        "tests_passed": "true",
    }
    notif = _codex_mcp_tool_call("report_result", args)

    payload = codex_tool_call_payload(notif)
    assert payload is not None
    assert payload["name"] == "report_result"
    assert payload["input"] == args

    # The daemon feeds exactly `payload["input"]` into the TaskResult builder.
    task_result = _task_result_from_tool_args(payload["input"])
    assert task_result.status == "complete"
    assert task_result.commit == "c0dexbeef"
    assert task_result.files_changed == ["x.py", "y.py"]
    assert task_result.tests_passed is True


def test_codex_tool_call_payload_none_for_non_mcp_notification():
    """A non-mcpToolCall notification (an agent message) yields None — the
    capture branch only fires on a tool call."""
    assert codex_tool_call_payload(_codex_agent_message("just talking")) is None
    assert codex_tool_call_payload(_codex_turn_started()) is None


# --- end-to-end: run_task driving a stubbed codex backend -------------------


@pytest.fixture
def codex_task_cfg(tmp_path: Path):
    """A minimal project with one Ready task (TB-5), retries low and the
    project-wide verifier disabled — mirrors `test_daemon_recovery`'s fixture so
    a clean `report_result(status=complete)` lands the task in Complete."""
    from ap2.config import Config

    (tmp_path / "TASKS.md").write_text(
        "# Tasks\n\n"
        "## Active\n\n"
        "## Ready\n\n"
        "- [ ] **TB-5** **Victim** `#x` — Will be run. [→ brief](brief.md)\n\n"
        "## Backlog\n\n## Complete\n\n## Frozen\n"
    )
    (tmp_path / "CLAUDE.md").write_text(
        "## Autopilot\n\n- Task list: `TASKS.md`\n- Next task ID: TB-10\n"
    )
    saved_verify = os.environ.pop("AP2_VERIFY_CMD", None)
    os.environ["AP2_MAX_RETRIES"] = "2"
    os.environ["AP2_TASK_TIMEOUT_S"] = "60"
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()
    yield cfg
    os.environ.pop("AP2_MAX_RETRIES", None)
    os.environ.pop("AP2_TASK_TIMEOUT_S", None)
    if saved_verify is not None:
        os.environ["AP2_VERIFY_CMD"] = saved_verify


def test_run_task_completes_via_codex_report_result(codex_task_cfg, monkeypatch):
    """End-to-end: a codex-backed `task` agent's `report_result(status=complete)`
    round-trips through the daemon's codex stream-walk into a Complete task —
    the loop a codex backend could not close before TB-373."""
    import ap2.adapters.select as select_mod
    from ap2.board import Board
    from ap2 import events
    from ap2.daemon import run_task

    args = {
        "status": "complete",
        "commit": "c0dexbeef",
        "summary": "implemented via codex",
        "files_changed": "x.py,y.py",
        "tests_passed": "true",
    }
    envelopes = [
        _codex_turn_started(),
        _codex_agent_message("implementing"),
        _codex_mcp_tool_call("report_result", args),
        _codex_token_usage(input_tokens=120, output_tokens=60),
        _codex_turn_completed(),
    ]
    codex_adapter = CodexAdapter(codex=FakeCodex(envelopes))
    # Force the task kind onto the stubbed codex adapter (run_task resolves the
    # backend through select_adapter, imported at call time).
    monkeypatch.setattr(
        select_mod, "select_adapter", lambda kind, cfg: codex_adapter
    )

    task = Board.load(codex_task_cfg.tasks_file).get("TB-5")
    # sdk=None: the codex adapter ignores the injectable Claude handle.
    asyncio.run(run_task(codex_task_cfg, None, None, task))

    board = Board.load(codex_task_cfg.tasks_file)
    assert board.find("TB-5")[0] == "Complete"

    evts = events.tail(codex_task_cfg.events_file, 30)
    end = next(e for e in reversed(evts) if e["type"] == "task_complete")
    assert end["status"] == "complete"
    assert end["commit"] == "c0dexbeef"

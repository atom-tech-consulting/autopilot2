"""TB-367 / goal.md axis 6 capstone — mixed-config end-to-end through the
`AgentAdapter` seam: one claude-backed kind + one codex-backed kind in ONE
process.

Axis 6 migrated every dispatch site (`run_task`, the shared `_run_control_agent`
consumers, the verifier / validator / janitor judges, the ideation-scrub canary)
off direct `sdk.query()` onto `select_adapter(kind, cfg).run(...)`. With every
kind now adapter-routed, a *mixed* configuration — `ideation=claude`,
`task=codex` — is end-to-end testable for the first time. This is the test the
focus's Progress signal names:

    "A mixed configuration (ideation=claude, task=codex) runs an agent of each
     kind end-to-end: dispatch then tool calls then report_result then verify."

The test is hermetic — both adapters are driven by STUBBED backend handles (the
`FakeSDK` claude stub from `test_agent_adapter.py` and the `FakeCodex` stub from
`test_codex_adapter.py`); no real Claude SDK and no real `codex` CLI are
touched (that gated proof is the 6h `real-sdk-smoke` cron, out of scope here).

It pins the two things the focus cares about:

  (a) Per-kind backend selection routes to two DIFFERENT adapter
      implementations in one process — `ideation` resolves a `ClaudeCodeAdapter`
      (from the `[agent_backends]` config table), `task` resolves a
      `CodexAdapter` (from the `AP2_AGENT_BACKEND_TASK` env override), exercising
      BOTH selection surfaces.
  (b) The normalized result / usage shape is backend-agnostic: a tool call and
      `report_result` round-trip end-to-end through each backend, the daemon's
      `_task_result_from_tool_args` honors the captured `report_result` args
      identically, and verify + the cost guard read one `AgentResult` /
      `AgentUsage` shape regardless of which backend produced the run.

Assertions stay on the normalized seam (`select_adapter`, `AgentResult`,
`AgentUsage`), not on backend-internal stream details — per the briefing's
design note.
"""
from __future__ import annotations

import asyncio
from typing import Any

from ap2.adapters import (
    AgentOptions,
    AgentResult,
    AgentTools,
    AgentUsage,
    ClaudeCodeAdapter,
    CodexAdapter,
)
from ap2.adapters.base import AgentEvent
from ap2.adapters.select import AGENT_KINDS, select_adapter
from ap2.components.auto_approve import _event_combined_tokens as guard_tokens
from ap2.config import CONFIG_TOML_FILE, Config
from ap2.daemon import _task_result_from_tool_args

# Reuse the per-backend suites' hermetic stubs + the canonical ap2-toolset
# constant so this capstone never drifts from the contract those suites pin.
from ap2.tests.test_agent_adapter import (
    _EXPECTED_AP2_TOOL_SHORT_NAMES,
    AssistantMessage,
    FakeSDK,
    ResultMessage,
    _text_block,
    _tool_use_block,
)
from ap2.tests.test_codex_adapter import (
    FakeCodex,
    _agent_message as _codex_agent_message,
    _mcp_tool_call as _codex_mcp_tool_call,
    _token_usage as _codex_token_usage,
    _turn_completed as _codex_turn_completed,
    _turn_started as _codex_turn_started,
)


# --------------------------------------------------------------------------
# A mixed-backend project: `ideation=claude` via the `[agent_backends]` config
# table, `task=codex` via the `AP2_AGENT_BACKEND_TASK` env override — so the
# test exercises BOTH per-kind selection surfaces in one process.
# --------------------------------------------------------------------------


def _mixed_cfg(tmp_path, monkeypatch) -> Config:
    # Scrub ambient AP2_AGENT_BACKEND_* so the table / env precedence is
    # observable in isolation regardless of the shell env.
    for kind in AGENT_KINDS:
        monkeypatch.delenv(f"AP2_AGENT_BACKEND_{kind.upper()}", raising=False)

    (tmp_path / ".cc-autopilot").mkdir(exist_ok=True)
    (tmp_path / "TASKS.md").write_text(
        "# Tasks\n\n## Active\n\n## Ready\n\n## Backlog\n\n"
        "## Complete\n\n## Frozen\n"
    )
    # `ideation` pinned to claude via the file table; `task` flipped to codex via
    # the env-override surface (the two surfaces the prose bullet names).
    (tmp_path / CONFIG_TOML_FILE).write_text(
        '[agent_backends]\nideation = "claude"\n'
    )
    monkeypatch.setenv("AP2_AGENT_BACKEND_TASK", "codex")

    cfg = Config.from_toml(tmp_path / CONFIG_TOML_FILE)
    cfg.ensure_dirs()
    return cfg


# --------------------------------------------------------------------------
# Backend-neutral options / tools the drains share. `run()`'s normalized output
# is driven by the envelope stream, so one generic options/tools object
# exercises both contracts identically.
# --------------------------------------------------------------------------


def _options() -> AgentOptions:
    return AgentOptions(
        model="m",
        effort="high",
        max_turns=40,
        cwd="/tmp/proj",
        permission_mode="bypassPermissions",
        setting_sources=["project"],
    )


def _tools() -> AgentTools:
    return AgentTools(allowed=["Bash", "Read"], mcp_servers={"autopilot": object()})


# --------------------------------------------------------------------------
# Stub streams: each ends with a `report_result` tool call (the task-agent
# completion signal, registered on EVERY backend's tool surface per axis 3/7)
# plus a terminal usage-bearing envelope. The claude stream uses the SDK's
# `tool_use` block shape; the codex stream uses the real `openai_codex`
# notification shape (an `item/completed` carrying an `mcpToolCall` item, plus a
# `thread/tokenUsage/updated`).
# --------------------------------------------------------------------------


def _claude_report_envelopes(args: dict) -> list:
    return [
        AssistantMessage([_text_block("ideation: assessing the board")]),
        AssistantMessage([_tool_use_block("report_result", args)]),
        ResultMessage(
            usage={"input_tokens": 100, "output_tokens": 50},
            model_usage={"claude-opus-4-7": {"input_tokens": 100}},
            total_cost_usd=0.0123,
            num_turns=3,
            result="ideation done",
            model="claude-opus-4-7",
        ),
    ]


def _codex_report_envelopes(args: dict) -> list:
    return [
        _codex_turn_started(),
        _codex_agent_message("task: implementing"),
        _codex_mcp_tool_call("report_result", args),
        _codex_token_usage(input_tokens=120, output_tokens=60, cached_input_tokens=20),
        _codex_turn_completed(),
    ]


def _collect(adapter, prompt, tools, options) -> list[AgentEvent]:
    """Drive `adapter.run(...)` once and return every normalized event."""

    async def _drain() -> list[AgentEvent]:
        return [ev async for ev in adapter.run(prompt, tools, options)]

    return asyncio.run(_drain())


def _report_result_args_from_events(events: list[AgentEvent]) -> dict | None:
    """Walk the normalized stream for a `report_result` tool call and return its
    args dict, regardless of backend.

    Mirrors the daemon's `_log_message` capture for the claude path (a
    `tool_use` block on an `AssistantMessage`'s `.content`) and reads the
    `mcpToolCall` item the codex path surfaces — the same args the MCP
    `report_result` handler receives.
    """
    for ev in events:
        raw: Any = ev.raw
        # Claude: AssistantMessage whose `.content` carries tool_use blocks.
        content = getattr(raw, "content", None)
        if isinstance(content, list):
            for part in content:
                if getattr(part, "name", None) in (
                    "report_result",
                    "mcp__autopilot__report_result",
                ):
                    inp = getattr(part, "input", None)
                    if isinstance(inp, dict):
                        return dict(inp)
        # Codex: an `item/completed` notification carrying an mcpToolCall item
        # (`Notification(method, payload)` with `payload.item` a `ThreadItem`).
        payload = getattr(raw, "payload", None)
        item = getattr(payload, "item", None)
        if item is not None:
            item = getattr(item, "root", item)
            if getattr(item, "type", None) == "mcpToolCall" and getattr(
                item, "tool", None
            ) in ("report_result", "mcp__autopilot__report_result"):
                a = getattr(item, "arguments", None)
                if isinstance(a, dict):
                    return dict(a)
    return None


# --------------------------------------------------------------------------
# (a) Per-kind backend selection routes to two DISTINCT implementations.
# --------------------------------------------------------------------------


def test_mixed_config_routes_each_kind_to_a_distinct_adapter(tmp_path, monkeypatch):
    cfg = _mixed_cfg(tmp_path, monkeypatch)

    # `ideation` resolved from the `[agent_backends]` table, `task` from the
    # `AP2_AGENT_BACKEND_TASK` env override — two surfaces, one process.
    assert cfg.get_agent_backend("ideation") == "claude"
    assert cfg.get_agent_backend("task") == "codex"

    ideation = select_adapter("ideation", cfg)
    task = select_adapter("task", cfg)
    assert isinstance(ideation, ClaudeCodeAdapter)
    assert ideation.backend == "claude"
    assert isinstance(task, CodexAdapter)
    assert task.backend == "codex"
    # Two DIFFERENT adapter implementations live side-by-side in one process.
    assert type(ideation) is not type(task)

    # A sibling kind with neither an override nor a table entry stays on the
    # all-`claude` default (per-kind, not per-daemon, selection).
    assert isinstance(select_adapter("status_report", cfg), ClaudeCodeAdapter)


# --------------------------------------------------------------------------
# Tool surface: `report_result` is dispatchable through BOTH backends.
# --------------------------------------------------------------------------


def test_mixed_config_both_backends_expose_report_result_tool(tmp_path, monkeypatch):
    import ap2.tools as tools

    cfg = _mixed_cfg(tmp_path, monkeypatch)
    ideation = select_adapter("ideation", cfg)
    task = select_adapter("task", cfg)

    # Hand ap2's REAL custom toolset to each resolved adapter's registration
    # surface (the daemon's `build_mcp_server(cfg)` path), then enumerate.
    tools.build_mcp_server(cfg, adapter=ideation)
    tools.build_mcp_server(cfg, adapter=task)

    # `report_result` is exposed through BOTH backends' tool surface ...
    assert "report_result" in ideation.registered_tool_names()
    assert "report_result" in task.registered_tool_names()
    # ... and each backend registers the identical full ap2 toolset (no tool
    # dropped or renamed on either backend).
    assert set(ideation.registered_tool_names()) == _EXPECTED_AP2_TOOL_SHORT_NAMES
    assert set(task.registered_tool_names()) == _EXPECTED_AP2_TOOL_SHORT_NAMES


# --------------------------------------------------------------------------
# (b) End-to-end: dispatch -> tool call -> report_result -> verify, for EACH
# kind, both backends stubbed, asserting one normalized result/usage shape.
# --------------------------------------------------------------------------


def test_mixed_config_round_trips_tool_call_and_report_result_end_to_end(
    tmp_path, monkeypatch
):
    cfg = _mixed_cfg(tmp_path, monkeypatch)

    # --- claude-backed `ideation` kind --------------------------------------
    ideation = select_adapter("ideation", cfg)
    assert isinstance(ideation, ClaudeCodeAdapter)
    claude_args = {
        "status": "complete",
        "commit": "c1aude7",
        "summary": "ideation pass complete",
        "tests_passed": "true",
    }
    # Inject the stub handle exactly as the daemon does after `select_adapter`
    # (only the Claude backend carries an injectable `_sdk`).
    ideation._sdk = FakeSDK(_claude_report_envelopes(claude_args))
    claude_events = _collect(ideation, "ideation prompt", _tools(), _options())

    # --- codex-backed `task` kind -------------------------------------------
    task = select_adapter("task", cfg)
    assert isinstance(task, CodexAdapter)
    codex_args = {
        "status": "complete",
        "commit": "c0dex42",
        "summary": "task implemented",
        "tests_passed": "true",
    }
    task._codex = FakeCodex(_codex_report_envelopes(codex_args))
    codex_events = _collect(task, "task prompt", _tools(), _options())

    # Two DIFFERENT adapter implementations were driven in one process.
    assert type(ideation) is not type(task)

    # Same assertions over BOTH backends — the normalized seam is backend-agnostic.
    # `reports_full_meta` marks the claude path, which carries cost / model /
    # turn count in its stream; codex reports only token usage (the normalized
    # record degrades the rest gracefully to 0 / "").
    for events, expected_commit, reports_full_meta in (
        (claude_events, "c1aude7", True),
        (codex_events, "c0dex42", False),
    ):
        # report_result round-tripped through the adapter's tool surface and is
        # honored identically by the daemon's tool-args -> TaskResult builder.
        captured = _report_result_args_from_events(events)
        assert captured is not None
        task_result = _task_result_from_tool_args(captured)
        assert task_result.status == "complete"
        assert task_result.commit == expected_commit
        assert task_result.tests_passed is True

        # verify reads one normalized AgentResult / AgentUsage shape regardless
        # of which backend produced the run.
        terminal = events[-1]
        assert terminal.type == "result"
        result = terminal.result
        assert isinstance(result, AgentResult)
        assert result.status == "complete"

        usage = result.usage
        assert isinstance(usage, AgentUsage)
        # The token count is the cross-backend invariant: both backends report
        # input/output tokens, and the cost guard reads them off one shape.
        assert usage.combined_tokens > 0

        # The reads verify.py's prose-judge cost capture performs off the
        # normalized record.
        result_meta: dict = {}
        if usage.model:
            result_meta["model"] = usage.model
        if usage.num_turns:
            result_meta["num_turns"] = usage.num_turns
        if usage.total_cost_usd:
            result_meta["total_cost_usd"] = usage.total_cost_usd
        if usage.usage:
            result_meta["usage"] = usage.usage
        if reports_full_meta:
            # Claude carries cost / model / turns — the full capture succeeds.
            assert usage.total_cost_usd > 0.0
            assert {"model", "num_turns", "total_cost_usd", "usage"} <= set(
                result_meta
            )
        else:
            # Codex reports tokens but not cost / model / turns; the capture
            # degrades gracefully (it fills the keys it can — here, usage).
            assert usage.total_cost_usd == 0.0
            assert "usage" in result_meta

        # And the cost guard reads the SAME token count off the normalized
        # event payload — one shape, no per-backend branching.
        assert guard_tokens(usage.event_payload()) == usage.combined_tokens

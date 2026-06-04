"""Shared adapter-seam dispatch helpers for the backend-parametrized
tool-round-trip real-SDK smokes (TB-374 / goal.md axis 7).

The three tool-round-trip smokes (`test_report_result_real_sdk.py`,
`test_cron_propose_real_sdk.py`, `test_pipeline_task_start_real_sdk.py`) used to
hardcode `import claude_agent_sdk as sdk` → `sdk.ClaudeAgentOptions(...)` →
`sdk.query(...)`, so pointing a kind at codex had ZERO effect on them and there
was no codex tool-call coverage at all. TB-374 lifts them off the hardcoded
Claude SDK onto the production `AgentAdapter` seam — `select_adapter(kind, cfg)`
+ `adapter.run(...)` with the backend-neutral `AgentTools` / `AgentOptions` — and
parametrizes them over the `claude` and `codex` backends so the SAME
"a real agent invokes the tool and its captured args convert to a domain
object" assertion runs against BOTH backends.

This module centralizes the bits all three smokes share so they never drift:

  - `BACKENDS`: the two backends every tool-round-trip smoke parametrizes over.
  - `gate_backend`: the per-backend opt-in gate (the codex variant's
    `openai_codex` `importorskip`, mirroring `test_codex_real_sdk.py`).
  - `force_backend`: pin a kind's backend via `AP2_AGENT_BACKEND_<KIND>` — the
    literal "set the kind's backend to codex and run the existing smoke"
    operator capability `select_adapter` then resolves.
  - `extract_tool_calls`: backend-neutral tool-call extraction off the adapter's
    normalized `AgentEvent` stream, mirroring `daemon.run_task._log_message`'s
    capture walk (Claude `.content` `ToolUseBlock`s + the codex `mcpToolCall`
    shape via the adapter's `codex_tool_call_payload`).
  - `bootstrap_judge_cfg` / `run_judge_to_result` / `agent_result_transient`
    (TB-376): the judge-smoke trio's shared dispatch + transient-classify
    helpers. The verifier / validator / janitor judge real-SDK smokes parametrize
    the SAME verdict-correctness assertion over both backends through these — a
    judge that dispatches but mis-verdicts is the failure mode that matters, so
    the smoke routes the prompt through `select_adapter(<judge_kind>, cfg)` and
    asserts the parsed verdict VALUE, not just that a tool was called.
"""
from __future__ import annotations

from typing import Any

import pytest

#: The two backends every tool-round-trip smoke parametrizes over. Expressing
#: the smoke as parametrize-over-backend is the direct, literal form of axis 7's
#: "a contract both adapters satisfy" — and, unlike the hermetic parity suite,
#: it exercises a REAL agent invoking a REAL tool rather than matching names
#: over a stub.
BACKENDS = ("claude", "codex")


def gate_backend(backend: str) -> None:
    """Apply the per-backend opt-in gate inside a parametrized smoke.

    The module-level `AP2_REAL_SDK` skip marker already gates EVERY variant
    (claude and codex). The codex variant carries the SAME secondary gate the
    codex dispatch smoke (`test_codex_real_sdk.py`) uses: skip cleanly when the
    codex SDK handle the `CodexAdapter` imports lazily isn't installed, so a box
    that opted into the live smokes but has no codex backend skips rather than
    errors. (A missing credential surfaces as a transient transport error and is
    handled by `_transient.call_with_transient_retry`, not by this gate.)
    """
    if backend == "codex":
        pytest.importorskip(
            "openai_codex",
            reason=(
                "codex SDK (openai_codex) not installed; "
                "live round-trip unavailable"
            ),
        )


def force_backend(monkeypatch, kind: str, backend: str) -> None:
    """Pin agent `kind` to `backend` via the `AP2_AGENT_BACKEND_<KIND>` env
    override — the literal "set the kind's backend to codex and run the existing
    smoke" operator capability. `select_adapter(kind, cfg)` then reads the merged
    per-kind map (env override > `[agent_backends]` table > the all-`claude`
    default) and resolves to the matching adapter instance.
    """
    monkeypatch.setenv(f"AP2_AGENT_BACKEND_{kind.upper()}", backend)


def extract_tool_calls(raw: Any) -> list[dict]:
    """Backend-neutral extraction of `{name, input, id, result?}` tool calls off
    one normalized `AgentEvent.raw` envelope.

    Mirrors `daemon.run_task._log_message`'s capture walk so the smoke reads the
    SAME tool args production does, regardless of backend:

      - Claude: each `.content` `ToolUseBlock` carries `.name` / `.input` /
        `.id`.
      - Codex: the adapter's `codex_tool_call_payload(notif)` reconstructs an
        `mcpToolCall` item's tool short-name + FULL args (and any inline
        result); a codex notification carries no Claude `.content` blocks, and
        `codex_tool_call_payload` returns `None` for a Claude envelope, so the
        two branches never double-count.
    """
    from ap2.adapters.codex import codex_tool_call_payload

    calls: list[dict] = []
    for part in (getattr(raw, "content", None) or []):
        name = getattr(part, "name", None)
        inp = getattr(part, "input", None)
        if name is not None and inp is not None:
            calls.append(
                {
                    "name": name,
                    "input": inp,
                    "id": getattr(part, "id", None) or "",
                }
            )
    codex_call = codex_tool_call_payload(raw)
    if codex_call is not None:
        calls.append(
            {
                "name": codex_call.get("name"),
                "input": codex_call.get("input"),
                "id": codex_call.get("id") or "",
                "result": codex_call.get("result"),
            }
        )
    return calls


# --------------------------------------------------------------------------
# Judge-smoke helpers (TB-376).
#
# The three judge smokes (`test_prose_judge_real_sdk.py`,
# `test_validator_judge_real_sdk.py`, `test_janitor_judge_real_sdk.py`) prove a
# different contract than the tool smokes above: not "the agent CALLED a tool",
# but "the judge returned the CORRECT verdict on whichever backend the kind
# selects". They reuse `BACKENDS` / `gate_backend` / `force_backend` for the
# parametrization + codex opt-in gate, plus the three helpers below for the
# shared dispatch + transient-classify path.
# --------------------------------------------------------------------------


def bootstrap_judge_cfg(root: Any):
    """Write a minimal `TASKS.md` / `CLAUDE.md` skeleton at `root` and return a
    loaded `Config`.

    Enough for `Config.load` + `select_adapter(<judge_kind>, cfg)`:
    `get_agent_backend` reads the `AP2_AGENT_BACKEND_<KIND>` override
    `force_backend` set (env wins over the `[agent_backends]` table), so the
    resolved adapter matches the parametrized backend. Mirrors the
    `_bootstrap_project` skeleton the tool-round-trip smokes use.
    """
    from pathlib import Path

    from ap2.config import Config

    root = Path(root)
    (root / "TASKS.md").write_text(
        "# Tasks\n\n## Active\n\n## Ready\n\n## Backlog\n\n"
        "## Complete\n\n## Frozen\n"
    )
    (root / "CLAUDE.md").write_text(
        "# Smoke project\n\n## Autopilot\n\n- Next task ID: TB-2\n"
    )
    cfg = Config.load(root)
    cfg.ensure_dirs()
    return cfg


def run_judge_to_result(adapter, backend: str, prompt: str, tools, *, cwd: Any):
    """Dispatch a judge `prompt` through `adapter.run_to_result` and return the
    terminal `AgentResult`.

    Builds a backend-neutral `AgentOptions` that intentionally pins NO model:
    the production judge paths resolve a Claude model (`agent_model`'s
    `claude-opus-4-7` default for the verifier / janitor judges, the validator
    judge's hardcoded `claude-haiku-4-5`), which a live codex turn would reject.
    Leaving `model=None` lets each backend use its own default (claude's CLI
    default, codex's native default) — exactly as the tool-round-trip smokes do.
    The codex variant additionally gets `effort="low"` + a read-only sandbox to
    bound cost and stay side-effect-free, mirroring `test_report_result_real_sdk`
    / `test_codex_real_sdk`.
    """
    import asyncio

    from ap2.adapters import AgentOptions

    options = AgentOptions(
        cwd=str(cwd),
        permission_mode="bypassPermissions",
        max_turns=4,
        setting_sources=["project"],
    )
    if backend == "codex":
        options.effort = "low"
        options.extra = {"sandbox": "read-only"}
    return asyncio.run(adapter.run_to_result(prompt, tools, options))


def agent_result_transient(result: Any):
    """`transient_of` for `call_with_transient_retry` over a judge `AgentResult`.

    A `complete` adapter result is a real verdict the caller must parse and
    assert — so a confident-but-WRONG verdict still fails the smoke — and this
    returns None to let that assertion run. A non-`complete` result (the adapter
    folded an SDK transport/service error or a per-run timeout into
    `status="error"` / `"timeout"`) is an inconclusive wiring fault for a
    verdict-correctness smoke → classified transient so the smoke SKIPS after one
    bounded retry, mirroring the prose judge's historical `judge error → skip`
    posture and the codex dispatch smoke's transient handling.
    """
    from ._transient import transient_signature

    if getattr(result, "status", None) == "complete":
        return None
    err = getattr(result, "error", None) or getattr(result, "status", None)
    return transient_signature(f"judge error: {err}") or "judge error"


# --------------------------------------------------------------------------
# Control-agent tool-call helper (TB-378).
#
# The five backend-selectable control kinds — `ideation`, `ideation_scrub`,
# `status_report`, `cron`, `mattermost` — were routed through the adapter seam
# in the axis-6 migrations (TB-360 / TB-365) but never live-validated, so there
# was no proof a codex-backed (or even an end-to-end claude-backed) control
# agent actually produces its expected output. The smokes in this package close
# that gap: each proves its kind genuinely drives a load-bearing tool on BOTH
# backends (ideation→`board_edit`, status_report/mattermost→`mattermost_reply`,
# cron→`log_event`) or, for `ideation_scrub`, produces the expected scrubbed-text
# shape (that smoke routes through `run_judge_to_result` directly since the scrub
# kind returns text, not a tool call).
#
# `run_control_to_tool_calls` centralizes the four tool-driving kinds' dispatch
# so they never drift: it mirrors `daemon._run_control_agent`'s seam — the
# per-kind `select_adapter(<kind>, cfg)` resolver, ap2's MCP toolset registered
# through the selected adapter (the codex variant rides TB-373's stdio-MCP
# bridge), and the streaming `AgentAdapter.run(...)` drained into the SAME
# backend-neutral tool-call shape `_log_message` walks.
# --------------------------------------------------------------------------


def run_control_to_tool_calls(
    *, kind: str, backend: str, prompt: str, allowed_tools: Any, root: Any,
) -> list[dict]:
    """Dispatch a trivial control-agent `prompt` through the production seam and
    return the captured backend-neutral tool calls.

    Resolves `select_adapter(kind, cfg)` (the per-kind backend resolver
    `daemon._run_control_agent` calls), registers ap2's MCP toolset through the
    selected adapter via `build_mcp_server(cfg, adapter=...)`, drives the
    streaming `AgentAdapter.run(...)`, and walks each normalized `AgentEvent`
    with `extract_tool_calls` — the same capture path production logs. Asserts
    the resolved adapter's backend matches the parametrized `backend` so a
    mis-wired per-kind override fails loudly.

    `allowed_tools` is the kind's production tool policy (e.g. `IDEATION_TOOLS`,
    `CONTROL_AGENT_TOOLS`, `MM_HANDLER_TOOLS`). `model` is intentionally left
    unset (None) so each backend uses its own default — a production control
    dispatch resolves a Claude model (`agent_model`) a live codex turn would
    reject, the same reason `run_judge_to_result` pins no model. The codex
    variant additionally gets `effort="low"` + a read-only sandbox to bound cost
    and stay side-effect-free.

    A transient SDK transport/service error is *raised* out of the adapter drain
    — wrap the call in `call_with_transient_retry` so the smoke skips (not
    errors) on a transport hiccup while a genuine wiring regression (the tool
    not called) still flows to the caller's assertions.
    """
    import asyncio

    from ap2.adapters import AgentOptions, AgentTools, select_adapter
    from ap2.tools import build_mcp_server

    cfg = bootstrap_judge_cfg(root)
    adapter = select_adapter(kind, cfg)
    assert adapter.backend == backend, adapter.backend
    mcp_server = build_mcp_server(cfg, adapter=adapter)

    tools = AgentTools(
        allowed=list(allowed_tools),
        mcp_servers={"autopilot": mcp_server},
    )
    options = AgentOptions(
        cwd=str(root),
        permission_mode="bypassPermissions",
        max_turns=5,
        setting_sources=["project"],
    )
    if backend == "codex":
        options.effort = "low"
        options.extra = {"sandbox": "read-only"}

    async def go() -> list[dict]:
        calls: list[dict] = []
        async for ev in adapter.run(prompt, tools, options):
            if ev.result is not None:
                continue
            calls.extend(extract_tool_calls(ev.raw))
        return calls

    return asyncio.run(go())

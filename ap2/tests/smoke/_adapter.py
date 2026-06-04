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

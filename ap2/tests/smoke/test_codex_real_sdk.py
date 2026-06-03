"""Real-SDK round-trip for the codex backend's dispatch path (TB-359 /
goal.md axis 7; TB-372 repointed it onto OpenAI's real `openai_codex` SDK).

The codex sibling of the Claude real-SDK smokes: it drives the `CodexAdapter`
(`ap2/adapters/codex.py`) end-to-end against the LIVE Codex backend, exercising
the real `openai_codex` client surface the adapter is built on —
`AsyncCodex().thread_start(...)` → `thread.turn(prompt)` → `turn.stream()` →
normalized `AgentResult`. Validates what the hermetic parity suite
(`test_adapter_parity.py`) can't: that the adapter actually constructs a real
`Codex`/`AsyncCodex` client, starts a thread + turn, and normalizes the live
notification stream into ap2's `AgentEvent` / `AgentResult` shape.

OPT-IN — same gate as the Claude real-SDK smokes: this test makes real API
calls. It only runs when `AP2_REAL_SDK` is set:

    AP2_REAL_SDK=1 uv run pytest ap2/tests/smoke/ -v -s

The default `pytest` invocation (and CI) skips it via the module-level
`pytestmark` skip marker — IDENTICAL to the Claude smokes
(`test_report_result_real_sdk.py`, `test_cron_propose_real_sdk.py`, …). It is
run on the 6h `real-sdk-smoke` cron routine (`ap2.smoke_runner.run_smoke_check`,
which executes the whole `ap2/tests/smoke/` directory when `AP2_REAL_SDK` is
set) — so dropping this file into `ap2/tests/smoke/` wires it onto that cron
alongside the Claude smokes.

Secondary gate: even with `AP2_REAL_SDK` set, the test skips cleanly when the
codex SDK handle isn't importable (the `CodexAdapter`'s lazy `import
openai_codex`) — a box that opted into the live smokes but has no codex backend
installed skips rather than errors. (A missing credential surfaces as a
transient/transport error and is handled by the transient-retry skip.)

The task body is intentionally trivial (a one-line text reply, read-only
sandbox, no tool calls) to bound cost and isolate the dispatch-wiring test from
agent reasoning — the codex analog of the Claude smokes' trivial bodies.

Delivering ap2's *in-process* MCP tool server to a live Codex agent (so a real
codex run could call `report_result`) is a separate concern: codex consumes
external stdio MCP servers via its own config, not ap2's in-process server, and
that wiring is out of scope for the adapter-dispatch repoint (TB-372). This
smoke therefore validates the dispatch path, not the MCP-tool round-trip.
"""
from __future__ import annotations

import os

import pytest

from ._transient import call_with_transient_retry

# Same opt-in skip marker the Claude real-SDK smokes carry — skips by default
# and in CI; the 6h `real-sdk-smoke` cron is where the live round-trip runs.
pytestmark = pytest.mark.skipif(
    not os.environ.get("AP2_REAL_SDK"),
    reason="real-SDK smoke; set AP2_REAL_SDK=1 to run",
)

# A marker the trivial prompt asks the agent to echo, so we can assert the live
# turn produced assistant text that round-tripped through the adapter's stream
# normalization.
_MARKER = "AP2_CODEX_SMOKE_OK"


def test_codex_dispatch_round_trip_via_real_sdk(tmp_path):
    """Real codex backend, driven through the `CodexAdapter` seam. Asserts a
    trivial turn round-trips `thread_start` + `turn` + `stream()` to a
    normalized `complete` `AgentResult` carrying the agent's echoed marker — the
    codex analog of the Claude real-SDK dispatch smokes."""
    import asyncio

    # Secondary gate: skip cleanly when the codex SDK handle the adapter imports
    # lazily isn't installed (so AP2_REAL_SDK=1 on a box without the codex
    # backend skips rather than errors).
    pytest.importorskip(
        "openai_codex",
        reason="codex SDK (openai_codex) not installed; live round-trip unavailable",
    )

    from ap2.adapters import AgentOptions, AgentTools, CodexAdapter

    async def go():
        # A fresh CodexAdapter resolves the real `openai_codex` handle lazily on
        # first dispatch (no injected stub) — so this exercises the production
        # import + client construction path.
        adapter = CodexAdapter()
        tools = AgentTools()
        options = AgentOptions(
            cwd=str(tmp_path),
            effort="low",
            # Read-only sandbox + auto-approve: a pure text reply needs no
            # filesystem writes or approvals, keeping the smoke cheap and safe.
            permission_mode="bypassPermissions",
            extra={"sandbox": "read-only"},
            max_turns=2,
        )
        prompt = (
            "Reply with EXACTLY this text and nothing else: "
            f"{_MARKER}. Do not run any commands or read any files."
        )
        return await adapter.run_to_result(prompt, tools, options)

    # A transient codex transport/service error (or missing credential) is
    # raised out of the drain — retry once, then skip (not error). A genuine
    # dispatch regression flows to the asserts below and still fails.
    result = call_with_transient_retry(
        lambda: asyncio.run(go()),
        describe="codex dispatch round-trip smoke",
    )

    print(f"\n[smoke] codex result status={result.status!r} text={result.text!r}")
    assert result.status == "complete", (
        f"codex dispatch did not complete through the adapter: status="
        f"{result.status!r} error={result.error!r}"
    )
    assert _MARKER in (result.text or ""), (
        "codex agent reply did not round-trip through the adapter's stream "
        f"normalization; got text={result.text!r}"
    )
    print(
        f"[smoke] PASS — codex dispatch round-tripped a turn "
        f"(combined_tokens={result.usage.combined_tokens})"
    )

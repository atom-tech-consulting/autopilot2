## Goal

This task adds the capstone integration proof for the **Current focus:
codex support through an agent adaptor layer**: a mixed-configuration
end-to-end test in which one agent kind is claude-backed and another is
codex-backed, exercising the full loop (dispatch → tool calls →
report_result → verify) through the `AgentAdapter` seam. It directly closes
the focus's Progress signal "A mixed configuration (`ideation=claude`,
`task=codex`) runs an agent of each kind end-to-end: dispatch then tool calls
then report_result then verify." The test is hermetic — both adapters are
stubbed (no real Claude SDK, no real `codex` CLI), mirroring the existing
fake-SDK patterns in `ap2/tests/e2e/_fakes.py` and
`ap2/tests/test_agent_adapter.py`.

Why now: with axis 6's per-kind dispatch-site migrations complete, every
agent kind dispatches through the adapter for the first time, so a mixed
(`claude` + `codex`) configuration is end-to-end testable at last; a
cross-backend integration regression would otherwise stay invisible until a
live mixed deployment broke in production.

## Scope

1. Add `ap2/tests/e2e/test_mixed_backend_end_to_end.py`.
2. Configure a mixed backend map using the existing per-kind selection
   surface: select `claude` for one kind and `codex` for another via the
   `[agent_backends]` table and/or `AP2_AGENT_BACKEND_*` env overrides. A
   natural pairing is a control kind (e.g. `ideation`/`status_report`) on
   `claude` and `task` on `codex`, but the agent may pick whichever pair is
   cleanest to drive hermetically.
3. Drive an agent of EACH kind through the loop with both backends stubbed:
   assert each kind resolves its configured adapter via `select_adapter`, a
   tool call is dispatched through the adapter's tool surface, `report_result`
   is honored, and verify reads a normalized `AgentResult`.
4. Reuse the existing hermetic stubs (`ap2/tests/e2e/_fakes.py`, the
   `ClaudeCodeAdapter(sdk=<stub>)` injection pattern, and a stubbed Codex
   path) — do NOT call the real `codex` CLI or the real Claude SDK.

## Design

The test should pin two things the focus cares about: (a) per-kind backend
selection actually routes to two DIFFERENT adapter implementations in one
process, and (b) the normalized result/usage shape is backend-agnostic so
verify + cost capture read one shape regardless of which backend produced the
run. Keep assertions on the normalized seam (`select_adapter`,
`AgentResult`), not on backend-internal details.

## Verification

- `uv run pytest -q ap2/tests/e2e/test_mixed_backend_end_to_end.py` — the new mixed-config e2e test passes.
- `uv run pytest -q` — full suite stays green.
- `ap2/tests/e2e/test_mixed_backend_end_to_end.py` Prose: the test selects different backends per kind (one `claude`, one `codex`) via the `[agent_backends]` map / `AP2_AGENT_BACKEND_*` overrides and asserts each kind resolves its configured adapter through `select_adapter`, then round-trips a tool call and `report_result` end-to-end; judge confirms via Read.

## Out of scope

- Invoking the real `codex` CLI or real Claude SDK (that is the gated
  real-SDK smoke run via the 6h `real-sdk-smoke` cron).
- Per-message or in-task backend routing (a focus non-goal, goal.md
  L127-128).
- Any change to an agent's prompt, tool policy, or verification semantics.
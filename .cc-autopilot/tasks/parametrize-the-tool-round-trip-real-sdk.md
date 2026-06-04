# Parametrize the tool-round-trip real-SDK smokes onto the AgentAdapter so the same test runs against both backends

Tags: #autopilot #agent-adapter #codex #tests #smoke #parity #axis-7

## Goal

This advances **Current focus: codex support through an agent adaptor
layer** and is the second half of closing **axis 7** ("an adapter-contract
test suite both adapters satisfy, plus a Codex real-SDK smoke"). Today the
tool-round-trip smokes prove the wrong thing for a backend-pluggable core:

- The Claude real-SDK tool smokes (`ap2/tests/smoke/test_report_result_real_sdk.py`,
  `test_cron_propose_real_sdk.py`, `test_pipeline_task_start_real_sdk.py`)
  dispatch by **hardcoding** `import claude_agent_sdk as sdk` →
  `sdk.ClaudeAgentOptions(mcp_servers={...})` → `sdk.query(...)`. They never
  call `select_adapter(...)` or read the per-kind backend map, so pointing a
  kind at codex has **zero effect** on them — there is no codex tool-call
  coverage at all.
- The hermetic parity suite (`ap2/tests/test_adapter_parity.py`) asserts only
  that both backends register the IDENTICAL tool short-name set and that
  **stubbed** streams normalize identically (its MCP server is a placeholder
  `mcp_servers={"autopilot": object()}`). It never has a real agent invoke a
  tool.
- The codex live smoke (TB-372) makes **no** tool call.

So nothing exercises a live codex agent actually *calling* `report_result`.
This task lifts the tool-round-trip smokes off the hardcoded Claude SDK onto
`select_adapter(kind, cfg)` + the `AgentAdapter` seam and parametrizes them
over backend, so the SAME assertions ("a real agent invokes the tool and its
args convert to a valid domain object") run against **both** Claude and
codex. That makes axis 7's "both backends" real and gives the operator the
"set the kind's backend to codex and run the existing smoke" capability
directly.

Why now: TB-373 delivers ap2's toolset to a live codex agent over stdio MCP
(the prerequisite — a codex agent must be able to *receive* the tool before
a tool-round-trip smoke can pass for it), so this task is `@blocked:TB-373`.
With the bridge in place, parametrizing the smoke is what verifies it end to
end and pins it against regression. Operator-directed 2026-06-03.

## Scope

- **Refactor the tool-round-trip smokes to dispatch through the adapter**
  (`ap2/tests/smoke/test_report_result_real_sdk.py`,
  `test_cron_propose_real_sdk.py`, `test_pipeline_task_start_real_sdk.py`):
  replace the direct `import claude_agent_sdk` + `sdk.query` dispatch with
  `select_adapter(kind, cfg)` + `adapter.run` / `run_to_result` and the
  backend-neutral `AgentTools` / `AgentOptions`, so dispatch flows through
  the same seam production uses.
- **Parametrize over backend**: run each smoke's assertions for `claude`
  and for `codex`. The codex variant gates exactly like the existing codex
  smoke — `AP2_REAL_SDK` set AND the codex handle (`openai_codex`)
  importable, else `skip` cleanly (and a missing credential / transport
  error flows through the existing transient-retry-then-skip helper, not an
  error).
- **Assert the real tool round-trip for each backend**: the agent actually
  invokes the tool (the tool-call appears in the adapter's normalized
  event stream — accept the MCP-prefixed and bare tool names), and the
  captured args convert to the valid domain object (`report_result` →
  a `complete` `TaskResult`; `cron_propose` / `pipeline_task_start` → their
  respective structured payloads). This is the assertion the name-only
  parity test and the no-tool codex smoke both omit.
- **Preserve the opt-in posture**: keep the module-level
  `AP2_REAL_SDK`-gated skip marker so the default `pytest` run (and CI) still
  skips these, and they still run on the 6h `real-sdk-smoke` cron.
- **Keep the existing Claude assertions intact** — the refactor must not
  weaken what the Claude smokes already prove; it routes them through the
  adapter and adds the codex parametrization alongside.

## Design

- **One test, both backends, through the seam.** The `AgentAdapter` is ap2's
  backend-standardization point; expressing the tool-round-trip smoke as
  `select_adapter(kind)` + parametrize-over-backend is the direct, literal
  form of axis 7's "a contract both adapters satisfy" — and, unlike the
  hermetic parity suite, it exercises a *real* agent invoking a *real* tool
  rather than matching names over a stub.
- **Rides on TB-373's bridge.** The codex variant can only pass once a live
  codex agent can receive ap2's tools over stdio MCP (TB-373); hence the
  block. Until then the codex parametrization would skip; after it, it
  proves the round-trip.
- **Gated + transient-safe, like its siblings.** Reuse the established
  `AP2_REAL_SDK` gate, the codex-handle `importorskip`, and the
  transient-retry-then-skip wrapper so a credential/transport hiccup is a
  skip, not a red CI, identical to the current smokes.

## Verification

- `uv run --extra dev pytest -q ap2/tests/ --ignore=ap2/tests/smoke` — full suite passes; the smokes remain skipped by default, so this confirms the refactor didn't break collection or imports.
- `grep -lqE "select_adapter|AgentAdapter" ap2/tests/smoke/test_report_result_real_sdk.py` — the report_result smoke now dispatches through the adapter seam.
- `! grep -qE "^\\s*import claude_agent_sdk as sdk" ap2/tests/smoke/test_report_result_real_sdk.py` — it no longer hardcodes the Claude SDK for dispatch.
- `ap2/tests/smoke/test_report_result_real_sdk.py` Prose: the smoke dispatches through `select_adapter` + the `AgentAdapter` (not a bare `claude_agent_sdk` `sdk.query`) and is parametrized over the `claude` and `codex` backends; for each backend it asserts a real agent invokes `report_result` and the captured args convert to a `complete` `TaskResult`; the codex variant skips cleanly when `AP2_REAL_SDK` is unset or the codex handle is unavailable. Judge confirms via Read.
- `ap2/tests/smoke/test_cron_propose_real_sdk.py` and `test_pipeline_task_start_real_sdk.py` Prose: each is likewise adapter-routed and backend-parametrized, asserting a real tool invocation + structured-arg round-trip for both backends. Judge confirms via Read.

## Out of scope

- **Running the live smokes against either backend** — operator-owned (`AP2_REAL_SDK=1`, real credentials); cannot be verified unattended (TB-122 trap). This task ships the parametrized structure and a passing default (skipped) run; the operator runs the live both-backend round-trip.
- The stdio-MCP bridge itself (TB-373, the prerequisite).
- The judge-verdict smokes (`test_prose_judge_real_sdk.py`, `test_validator_judge_real_sdk.py`) — those assert judge verdicts, not agent tool-call round-trips; migrating them is a separate optional follow-up, not part of this task.
- Changing the AgentAdapter contract, the tool definitions, or production dispatch.

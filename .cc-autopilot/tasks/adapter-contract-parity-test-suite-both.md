## Goal

Axis 7 of the **Current focus: codex support through an agent adaptor layer**.
With two real `AgentAdapter` implementations — `ClaudeCodeAdapter`
(`ap2/adapters/claude_code.py`) and `CodexAdapter` (TB-357,
`ap2/adapters/codex.py`) — there is no single contract suite that BOTH adapters
must satisfy, so a Codex regression that diverges from the Claude behavior
reference is invisible. This task adds a backend-parametrized adapter-contract
test suite that runs the same hermetic assertions (conforms to the ABC; `run()`
yields one `AgentEvent` per stream envelope plus a terminal `type="result"`;
`run_to_result` normalizes usage and handles the stream-incomplete / error /
timeout paths; `registered_tool_names()` enumerates the identical ap2 toolset)
against each adapter using a stubbed backend handle, plus a `codex` real-SDK
smoke that round-trips a tool call, gated like the existing Claude real-SDK
smokes and run via the 6h `real-sdk-smoke` cron (TB-350).

Why now: TB-357 adds a second backend whose behavior must match the Claude
reference, but the existing `test_agent_adapter.py` only exercises the Claude
path — without a shared parity contract and a gated codex smoke, Codex
regressions land silently; this closes goal.md's axis-7 delete-test.

## Scope

- Add a backend-parametrized contract suite (e.g.
  `ap2/tests/test_adapter_parity.py`) asserting the shared `AgentAdapter`
  contract against BOTH `ClaudeCodeAdapter` and `CodexAdapter` using injected
  stub handles (no live process) — conformance, per-envelope `AgentEvent` stream
  + terminal result, normalized `AgentUsage`, the stream-incomplete + error +
  timeout paths, and `registered_tool_names()` returning the identical ap2 tool
  short-name set for both.
- Add a `codex` real-SDK smoke alongside the existing Claude real-SDK smoke,
  gated by the same opt-in marker / env the Claude smokes use (so it skips by
  default and in CI) and wired into the 6h `real-sdk-smoke` cron routine
  (TB-350) — round-trips a single tool call against the live codex backend.
- Reuse the existing `_EXPECTED_AP2_TOOL_SHORT_NAMES` toolset assertion shape
  from `test_agent_adapter.py` for the cross-backend toolset-parity check.

## Design

The parity suite parametrizes over `[ClaudeCodeAdapter, CodexAdapter]` and
drives each with a backend-appropriate stub handle replaying a canned envelope
list, asserting both normalize to the same `AgentResult` / `AgentUsage` /
`AgentEvent` shapes — making the contract the single source of truth both
backends answer to. The real-SDK codex smoke mirrors the Claude smoke's gating
(skips unless the real-SDK opt-in is set) so the default suite stays hermetic;
the 6h cron is where the live round-trip actually runs.

## Verification

- `uv run pytest -q ap2/tests/test_adapter_parity.py` — the parametrized parity
  suite passes for both adapters (hermetic, stubbed handles).
- `uv run pytest -q ap2/tests/test_agent_adapter.py` — the existing Claude
  contract suite still passes.
- `grep -q "CodexAdapter" ap2/tests/test_adapter_parity.py` — the parity suite
  exercises the codex adapter, not just claude.
- `ap2/tests/test_adapter_parity.py` Prose: the suite asserts
  `registered_tool_names()` returns the identical ap2 tool short-name set for
  both `ClaudeCodeAdapter` and `CodexAdapter`; judge confirms via Read that the
  cross-backend toolset-parity assertion is present.
- Prose: the codex real-SDK smoke skips by default under the same opt-in gate as
  the existing Claude real-SDK smoke (no live `codex` process in the default /
  CI run) and is wired into the 6h `real-sdk-smoke` cron routine; judge confirms
  via Read/Grep that the smoke carries the same skip marker as the Claude smoke
  and is referenced by the real-sdk-smoke routine.

## Out of scope

- Implementing the `CodexAdapter` itself (axis 4, TB-357 — a hard predecessor).
- Per-kind selection + the auth gate (axis 5).
- Migrating any production dispatch site to the adapter (axis 6).

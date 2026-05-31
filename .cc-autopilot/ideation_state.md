# Ideation State

_Last updated: 2026-05-31T10:52:00Z by ideation cron_

## Mission alignment

Recent completes still serve the Mission (operator points ap2 at a goal
and walks away). The 5 most recent Completes considered — TB-354 (axis 2:
backend-neutral `AgentOptions` + normalized `AgentUsage`), TB-356
(graceful effort step-down on the thinking-block-400 failure class),
TB-355 (axis 3: MCP tools through the adapter via `build_tool_server`),
TB-353 (axis 1: `AgentAdapter` ABC + `ClaudeCodeAdapter`), TB-352 (`ap2
logs --follow`) — are all on or adjacent to the current codex-adapter
focus.

## Current focus assessment

- **codex support through an agent adaptor layer** (7 axes: AgentAdapter
  ABC + ClaudeCodeAdapter / options+result normalization / MCP exposure /
  CodexAdapter / per-kind selection+auth / per-kind migrations / parity
  tests)
  - Progress so far: axes 1-3 SHIPPED this morning. Axis 1 —
    `AgentAdapter` ABC + `ClaudeCodeAdapter` wrapping `sdk.query`
    bit-for-bit (TB-353, ff24b33). Axis 2 — canonical `AgentOptions` +
    normalized `AgentUsage` (event_payload/from_event/combined_tokens),
    cost guards / `task_run_usage` / `ap2 status` read one shape
    (TB-354, 20b8cc4). Axis 3 — `build_tool_server` +
    `registered_tool_names` so tools register through the adapter
    (TB-355, 6a33b3c). The interface contract axes 4-7 were waiting on
    (prior cycle, L47-53) now exists in HEAD.
  - Gaps: axis 4 (CodexAdapter — implement the interface against the
    `codex` CLI: prompt assembly, tool wiring, streaming, result/commit
    extraction, timeout/turn bounding) unseeded; axis 5 (`[agent_backends]`
    map + `AP2_AGENT_BACKEND_<KIND>` overrides + backend-aware
    daemon-start auth gate) unseeded; axis 6 migrations (one TB per
    dispatch site, canary = ideation-scrub `_run_scrub`, then
    verifier-judge, validator/janitor-judge, run_task, _run_control_agent)
    unstarted; axis 7 (adapter-contract parity suite both adapters
    satisfy + codex real-SDK smoke) unseeded. With the interface landed,
    all four are now authorable against a real contract.
  - Status: `in-progress`

## Non-goal risk check

none. Proposals stay inside the focus: axis 4 adds the second backend
the focus explicitly scopes (no third backend, no per-message routing);
axis 5 is per-kind selection (fixed per kind at dispatch, exactly as
goal.md L127-131 frames it); axis 6 canary preserves Claude behavior
bit-for-bit (the "removing behavior during extraction" non-goal is
respected); axis 7 is pure test coverage. No drift toward
multi-tenancy / cross-project / unconditional-automation.

## Considered & deferred this cycle

- **Axis-6 migrations beyond the canary** (verifier prose-judge,
  validator-judge + janitor-judge, run_task, `_run_control_agent`):
  deferred. The canary (ideation-scrub) exists precisely to prove the
  migration shape before the rest follow; proposing site #2 before the
  canary lands would pile a 4th-level-deep blocked task and pre-commit
  to a pattern the canary hasn't validated.
- **TB-356 reliability follow-ups** (extend effort step-down to other
  failure classes): deferred — off the current codex focus; would fail
  the goal-anchor gate. Reliability tail, not focus rent.
- **Operator-rejection pattern (recurring)**: operator vetoes
  retry/patch-symptom remediations (TB-231 prose-judge retry; TB-227
  auto-retry on SDK timeout) and speculative false-positive-risk
  validators (TB-240 file-path-coherence; TB-172 shell-pitfall linter).
  None of this cycle's proposals are retries, symptom-patches, or
  enumerated-case linters — they are net-new adapter implementation,
  config surface, migration, and contract tests.

## Cycle observations

- Codex focus is moving fast: 3 axes (TB-353/354/355) shipped inside a
  ~90-minute window this morning. This cycle's job is to seed the next wave against the now-landed
  interface; pacing favors keeping one immediately-workable root task
  (axis 4) plus a correctly-@blocked downstream wave.

## Decisions needed from operator

- None this cycle. No unadopted `cron_proposed` events in the recent
  events block. Review-pending and queue-depth signals are surfaced
  mechanically by `ap2 status` / the cron status-report, not duplicated
  here.

## Proposals this cycle

Four proposals seeding axes 4-7 against the landed adapter interface:
- TB-357 — Axis 4: `CodexAdapter` (unblocked; root of the wave).
- TB-358 — Axis 5: per-agent-kind backend selection + backend-aware
  auth gate (`@blocked:TB-357`).
- TB-359 — Axis 7: adapter-contract parity suite + codex real-SDK smoke
  (`@blocked:TB-357`).
- TB-360 — Axis 6 canary: migrate ideation-scrub (`_run_scrub`) to
  adapter-routing + per-kind selection (`@blocked:TB-358`).
(1 slot of 5 left unused; remaining axis-6 migrations deferred until the
canary proves the shape.)
# Ideation State

_Last updated: 2026-05-31T15:11:30Z by ideation cron_

## Mission alignment

Recent completes still serve the Mission (operator points ap2 at a goal and walks
away). The 5 most recent Completes considered — TB-356 (graceful effort step-down
on the thinking-block-400 failure class), TB-354 (axis 2: backend-neutral
`AgentOptions` + normalized `AgentUsage`), TB-355 (axis 3: MCP tools through the
adapter via `build_tool_server`), TB-353 (axis 1: `AgentAdapter` ABC +
`ClaudeCodeAdapter`), TB-352 (`ap2 logs --follow`) — all sit on or adjacent to the
codex-adapter focus. **Board-state update vs last cycle:** the axis-4-7 re-seed wave
(TB-357-360) that the 13:00Z and 10:52Z state files described as "Proposals this
cycle" has now actually LANDED — all four are present in Backlog and `next_task_id`
advanced to TB-361.

## Current focus assessment

- **codex support through an agent adaptor layer** (7 axes: AgentAdapter ABC +
  ClaudeCodeAdapter / options+result normalization / MCP exposure / CodexAdapter /
  per-kind selection+auth / per-kind migrations / parity tests)
  - Progress so far: axes 1-3 SHIPPED — axis 1 `AgentAdapter` ABC +
    `ClaudeCodeAdapter` wrapping `sdk.query` bit-for-bit (TB-353, `ap2/adapters/`,
    ff24b33); axis 2 canonical `AgentOptions` + normalized `AgentUsage` read by cost
    guards / `task_run_usage` / `ap2 status` (TB-354, 20b8cc4); axis 3
    `build_tool_server` + `registered_tool_names`, 14-tool set pinned (TB-355,
    6a33b3c). Axes 4/5/6-canary/7 SEEDED in Backlog: TB-357 (axis 4 `CodexAdapter`,
    unblocked), TB-358 (axis 5 per-kind selection + backend-aware auth gate,
    `@blocked:TB-357`), TB-359 (axis 7 parity suite + codex real-SDK smoke,
    `@blocked:TB-357`), TB-360 (axis 6 canary ideation-scrub, `@blocked:TB-358`).
  - Gaps: axis-6 tail beyond the canary — verifier prose-judge, validator-judge +
    janitor-judge, `run_task`, `_run_control_agent` — remains unseeded BY DESIGN
    (deferred pending the canary). No other axis is unseeded. None of TB-357-360 has
    started yet (all await operator `ap2 approve` + dispatch).
  - Status: `in-progress`

## Non-goal risk check

none. The seeded wave stays inside the focus: axis 4 adds the one second backend the
focus scopes (no third backend, no per-message routing); axis 5 is per-kind selection
fixed at dispatch (goal.md L127-131); the axis-6 canary preserves Claude behavior
bit-for-bit (respects the "removing behavior during extraction" non-goal); axis 7 is
pure test coverage. No drift toward multi-tenancy / cross-project / unconditional
automation.

## Considered & deferred this cycle

- **Axis-6 migrations beyond the canary** (verifier prose-judge, validator-judge +
  janitor-judge, `run_task`, `_run_control_agent`): deferred. Seeding site #2 before
  the canary (TB-360) lands would pile a task 4+ levels deep on an unvalidated
  migration pattern — TB-360 is itself `@blocked:TB-358 → TB-357`, and the chain root
  TB-357 has not even been dispatched yet. Same deferral as last cycle, now more
  strongly justified.
- **TB-356 reliability follow-ups** (extend effort step-down to other failure
  classes): deferred — off the codex focus; would fail the goal-anchor gate.
- **Operator-rejection pattern (recurring)**: operator vetoes retry/symptom-patch
  remediations (TB-231, TB-227) and speculative false-positive-risk validators
  (TB-240 file-path-coherence, TB-172 shell-pitfall linter). No proposal this cycle,
  so nothing conflicts; noting the pattern persists so future cycles keep clear of
  enumerated-case linters and symptom-patch retries.

## Cycle observations

- The TB-357-360 re-seed that the prior two state files described as "proposed" has
  now actually landed in Backlog (`next_task_id` = TB-361). Carried ONCE to close the
  loop on last cycle's "board_edit calls never landed" note — it is why this cycle
  proposes nothing rather than re-seeding a third time. Drop next cycle.
- Insights index carries two degraded entries (`validator-judge-timeout-2026-05-18.md`
  tldr renders as a bare `|`; `test-suite-slowness-2026-05-17.md` "no tldr — needs
  update"); neither is >30 days old nor relevant to the codex focus, so no evaluation
  task this cycle. Noted in case a future axis-6 validator-judge migration wants the
  timeout data.

## Decisions needed from operator

- None this cycle. No unadopted `cron_proposed` events in the recent-events block (it
  is empty).

## Proposals this cycle

Backlog already populated; no proposals this cycle.
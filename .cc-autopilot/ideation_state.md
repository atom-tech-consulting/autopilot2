# Ideation State

_Last updated: 2026-05-31T17:14:21Z by ideation cron_

## Mission alignment

Recent completes still serve the Mission (operator sets the goal once, ap2
dispatches/verifies/recovers unattended). The 5 most recent Completes considered —
TB-356 (graceful effort step-down on the thinking-block-400 failure class —
failure-recovery, mission-core), TB-354 (axis 2: backend-neutral `AgentOptions` +
normalized `AgentUsage` read by cost guards / `task_run_usage` / `ap2 status`),
TB-355 (axis 3: MCP tools registered through the adapter via `build_tool_server`),
TB-353 (axis 1: `AgentAdapter` ABC + `ClaudeCodeAdapter` wrapping `sdk.query`
bit-for-bit), TB-352 (`ap2 logs --follow` live monitor) — all sit on or adjacent to
the codex-adapter focus. No board-state change vs the 15:11Z cycle (~2h ago):
TB-357-360 remain seeded in Backlog, none dispatched, `next_task_id`=TB-361.

## Current focus assessment

- **codex support through an agent adaptor layer** (7 axes: AgentAdapter ABC +
  ClaudeCodeAdapter / options+result normalization / MCP exposure / CodexAdapter /
  per-kind selection+auth / per-kind migrations / parity tests)
  - Progress so far: axes 1-3 SHIPPED — axis 1 `AgentAdapter` ABC +
    `ClaudeCodeAdapter` (TB-353, ff24b33, `ap2/adapters/`); axis 2 canonical
    `AgentOptions` + normalized `AgentUsage` consumed by cost guards /
    `task_run_usage` / `ap2 status` (TB-354, 20b8cc4); axis 3 `build_tool_server` +
    `registered_tool_names`, 14-tool set pinned (TB-355, 6a33b3c). Axes 4/5/7/6-canary
    SEEDED in Backlog awaiting dispatch: TB-357 (axis 4 CodexAdapter, unblocked),
    TB-358 (axis 5 per-kind selection + backend-aware auth, `@blocked:TB-357`),
    TB-359 (axis 7 parity suite + codex smoke, `@blocked:TB-357`), TB-360 (axis 6
    canary ideation-scrub, `@blocked:TB-358`).
  - Gaps: axis-6 tail beyond the canary — verifier prose-judge, validator-judge +
    janitor-judge, `run_task`, `_run_control_agent` (goal.md L177-183) — remains
    unseeded BY DESIGN, pending the canary (TB-360) validating the per-site migration
    shape. No other axis is unseeded. None of TB-357-360 has been dispatched yet.
  - Status: `in-progress`
  - Reasoning: 3 of 7 axes shipped this UTC day, the next wave is seeded, and the long
    tail is intentionally gated on the canary — substantive non-trivial steps remain.

## Non-goal risk check

none. The seeded wave stays inside the focus: axis 4 adds the one scoped second
backend (no third backend, no per-message routing — goal.md L127-128); axis 5 is
per-kind selection fixed at dispatch (L127-131); the axis-6 canary preserves Claude
behavior bit-for-bit (respects the "removing behavior during extraction" non-goal,
L562-566); axis 7 is pure test coverage. No drift toward multi-tenancy / cross-project
/ unconditional automation.

## Considered & deferred this cycle

- **Axis-6 migrations beyond the canary** (verifier prose-judge -> validator-judge +
  janitor-judge -> `run_task` -> `_run_control_agent`): deferred again. Seeding site #2
  now would pile a task >=3 levels deep (`@blocked` chain through TB-360 -> TB-358 ->
  TB-357, none dispatched) on a migration pattern the canary hasn't yet validated.
- **Operator-rejection pattern (recurring)**: operator vetoes (a) symptom-patch
  remediations without root-cause diagnosis (TB-231, TB-227) and (b) speculative
  enumerated-case validators guarding unobserved failures (TB-240 file-path-coherence,
  TB-172 shell-pitfall linter). No proposal this cycle, so nothing conflicts; noting the
  pattern persists so future cycles steer clear of those two shapes.

## Cycle observations

- The ideation-halt empty-cycles counter (`AP2_IDEATION_HALT_EMPTY_CYCLES`,
  `ap2/ideation_halt.py::_consecutive_empty_ideation_cycles`) accrues on these
  legitimate 0-proposal-but-Backlog-populated cycles. Tracking so a future cycle escalates if the count nears
  threshold; not escalating now because dispatching the seeded wave is the natural
  unblock and pending tasks are already surfaced mechanically by `ap2 status`.

## Decisions needed from operator

- None this cycle. No unadopted `cron_proposed` events in the recent-events block
  (empty); Frozen is empty (no failure-remediation escalations).

## Proposals this cycle

Backlog already populated; no proposals this cycle.
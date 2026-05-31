# Ideation State

_Last updated: 2026-05-31T19:18:27Z by ideation cron_

## Mission alignment

Recent completes still serve the Mission (operator sets the goal once; ap2
dispatches/verifies/recovers unattended). The 5 most recent Completes considered —
TB-357 (axis 4: `CodexAdapter` against the `codex` CLI, 866423a — the second
backend the abstraction needed to be real), TB-354 (axis 2: canonical
`AgentOptions` + normalized `AgentUsage` read by cost guards / `task_run_usage` /
`ap2 status`, 20b8cc4), TB-355 (axis 3: MCP tools registered through the adapter via
`build_tool_server`, 6a33b3c), TB-353 (axis 1: `AgentAdapter` ABC +
`ClaudeCodeAdapter` wrapping `sdk.query` bit-for-bit, ff24b33), TB-356 (graceful
effort step-down on the thinking-block-400 failure class, 50de1db) — all sit on or
adjacent to the codex-adapter focus. Cross-cutting note: TB-356's effort-downshift
now structurally covers the same thinking-block-400 `task_error` class that hit the
auto-approved TB-358 run flagged by the 17:14Z cycle; the operator acked
`auto_approve_window_resume` at 17:51Z (operator_log), so TB-358's re-dispatch should
now survive its retry. Board change vs 17:14Z: TB-357 SHIPPED (was seeded), so its
two dependents TB-358 + TB-359 are now unblocked; `next_task_id`=TB-361.

## Current focus assessment

- **codex support through an agent adaptor layer** (7 axes: AgentAdapter ABC +
  ClaudeCodeAdapter / options+result normalization / MCP exposure / CodexAdapter /
  per-kind selection+auth / per-kind migrations / parity tests)
  - Progress so far: axes 1-4 SHIPPED — axis 1 `AgentAdapter` ABC +
    `ClaudeCodeAdapter` (TB-353, ff24b33); axis 2 canonical `AgentOptions` +
    normalized `AgentUsage` consumed by cost guards / `task_run_usage` / `ap2 status`
    (TB-354, 20b8cc4); axis 3 `build_tool_server` + `registered_tool_names`, 14-tool
    set pinned (TB-355, 6a33b3c); axis 4 `CodexAdapter` (`ap2/adapters/codex.py`,
    backend="codex") with a hermetic 15-test contract suite (TB-357, 866423a). Axes
    5/7 SEEDED + now UNBLOCKED (TB-357 done): TB-358 (axis 5 per-kind
    `[agent_backends]` + `AP2_AGENT_BACKEND_<KIND>` + backend-aware auth gate), TB-359
    (axis 7 adapter-contract parity suite + gated codex real-SDK smoke). Axis-6 canary
    TB-360 (migrate `_run_scrub` to adapter-routing) still `@blocked:TB-358`.
  - Gaps: axis-6 tail beyond the canary — verifier prose-judge, validator-judge +
    janitor-judge, `run_task`, `_run_control_agent` (goal.md L177-183) — remains
    unseeded BY DESIGN, pending the canary (TB-360) validating the per-site migration
    shape. No other axis is unseeded. TB-358/359/360 not yet dispatched.
  - Status: `in-progress`
  - Reasoning: substantive non-trivial steps remain.

## Non-goal risk check

none. The seeded wave stays inside the focus: axis 4 added the one scoped second
backend (no third backend, no per-message routing — goal.md L127-128); axis 5 is
per-kind selection fixed at dispatch (L127-131); the axis-6 canary preserves Claude
behavior bit-for-bit (respects the "removing behavior during extraction" non-goal,
L562-566); axis 7 is pure test coverage. No drift toward multi-tenancy / cross-project
/ unconditional automation.

## Considered & deferred this cycle

- **Axis-6 migrations beyond the canary** (verifier prose-judge -> validator-judge +
  janitor-judge -> `run_task` -> `_run_control_agent`): deferred again. Seeding site #2
  now would pile a task >=3 levels deep (`@blocked` chain through TB-360 -> TB-358,
  neither dispatched) on a migration pattern the canary hasn't yet validated. TB-357 landing doesn't change it (the canary, not axis 4,
  is the gate on the long tail).
- **Operator-rejection pattern (recurring)**: operator vetoes (a) symptom-patch
  remediations without root-cause diagnosis (TB-231) and (b) speculative
  enumerated-case validators guarding unobserved failures (TB-240 file-path-coherence,
  TB-172 shell-pitfall linter). No proposal this cycle, so nothing conflicts; noting the
  pattern persists so future cycles steer clear of those two shapes.

## Cycle observations

- Backlog holds exactly N=2 workable (unblocked) items this cycle — TB-358 (axis 5)
  and TB-359 (axis 7), both freed by TB-357's landing — which is why this is a
  legitimate 0-proposal cycle. The seeded wave dispatching is the natural next motion; pending TB-Ns surface mechanically via
  `ap2 status`.

## Decisions needed from operator

- None this cycle. Frozen is empty (no retry-exhausted escalations); no unadopted
  `cron_proposed` events in the recent-events block; the TB-358 `task_error` halt the
  17:14Z cycle surfaced is already operator-resolved (`auto_approve_window_resume` ack
  at 17:51Z, operator_log) and is additionally now covered structurally by TB-356's
  effort step-down on the thinking-block-400 class.

## Proposals this cycle

Backlog already populated; no proposals this cycle.
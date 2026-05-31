# Ideation State

_Last updated: 2026-05-31T21:21:30Z by ideation cron_

## Mission alignment

Recent completes still serve the Mission (operator declares the goal once; ap2
dispatches/verifies/recovers unattended). The 5 most recent Completes considered —
TB-357 (axis 4: `CodexAdapter` against the `codex` CLI, 866423a — the second backend
that makes the abstraction real), TB-355 (axis 3: ap2 MCP tools registered through the
adapter via `build_tool_server`, 6a33b3c), TB-354 (axis 2: canonical `AgentOptions` +
normalized `AgentUsage` read by cost guards / `task_run_usage` / `ap2 status`, 20b8cc4),
TB-353 (axis 1: `AgentAdapter` ABC + `ClaudeCodeAdapter` wrapping `sdk.query` bit-for-bit,
ff24b33), TB-356 (graceful effort step-down on the thinking-block-400 failure class,
50de1db) — all sit on or adjacent to the codex-adapter focus. No mission drift: every
recent ship moves a dispatch concept behind the adapter or hardens the existing loop.
Board unchanged vs the 19:18Z cycle: axes 1-4 shipped; TB-358/359 workable (TB-357
satisfied their `@blocked:TB-357`); TB-360 still `@blocked:TB-358`; `next_task_id`=TB-361.

## Current focus assessment

- **codex support through an agent adaptor layer** (7 axes: AgentAdapter ABC +
  ClaudeCodeAdapter / options+result normalization / MCP exposure / CodexAdapter /
  per-kind selection+auth / per-kind migrations / parity tests)
  - Progress so far: axes 1-4 SHIPPED — axis 1 `AgentAdapter` ABC + `ClaudeCodeAdapter`
    (TB-353, ff24b33); axis 2 normalized `AgentOptions`/`AgentUsage` consumed by cost
    guards / `task_run_usage` / `ap2 status` (TB-354, 20b8cc4); axis 3 `build_tool_server`
    tool registration (TB-355, 6a33b3c); axis 4 `CodexAdapter` (`ap2/adapters/codex.py`)
    with a hermetic contract suite (TB-357, 866423a). Axes 5 + 7 SEEDED and now workable
    (TB-357 satisfied their `@blocked:TB-357`): TB-358 (axis 5 per-kind `[agent_backends]`
    + `AP2_AGENT_BACKEND_<KIND>` + backend-aware auth gate), TB-359 (axis 7
    adapter-contract parity suite + gated codex real-SDK smoke). Axis-6 canary TB-360
    (migrate `_run_scrub` to adapter-routing) seeded, still `@blocked:TB-358`.
  - Gaps: axis-6 long tail beyond the canary — verifier prose-judge, validator-judge +
    janitor-judge, `run_task`, `_run_control_agent` (goal.md L177-183) — remains unseeded
    BY DESIGN, gated on the canary (TB-360) proving the per-site migration shape. No other
    axis is unseeded. TB-358/359/360 not yet dispatched.
  - Status: `in-progress`
  - Reasoning: three axes (5, 6-tail, 7) still carry substantive non-trivial work.

## Non-goal risk check

none. The seeded wave stays inside the focus: axis 4 added the one scoped second backend
(no third backend, no per-message routing — goal.md L127-128); axis 5 is per-kind
selection fixed at dispatch (L127-131); the axis-6 canary preserves Claude behavior
bit-for-bit (respects the "removing behavior during extraction" non-goal, L562-566); axis
7 is pure test coverage. No drift toward multi-tenancy / cross-project / unconditional
automation.

## Considered & deferred this cycle

- **Axis-6 migrations beyond the canary** (verifier prose-judge -> validator-judge +
  janitor-judge -> `run_task` -> `_run_control_agent`): deferred again. Seeding site #2 now
  stacks a task >=3 levels deep (`@blocked` chain through TB-360 -> TB-358, neither
  dispatched) on a migration pattern the canary hasn't validated yet. The canary, not any
  shipped axis, is the gate on the long tail.
- **Operator-rejection pattern (recurring)**: vetoes cluster on (a) symptom-patch
  remediations without root-cause diagnosis (TB-231) and (b) speculative enumerated-case
  validators guarding unobserved failures (TB-240 file-path-coherence, TB-172 shell-pitfall
  linter). No proposal this cycle, so nothing conflicts; noting the pattern persists so
  future cycles steer clear of those two shapes.
- **Stale `test-suite-slowness-2026-05-17.md` insight** (no tldr in the index): a
  data-quality gap, but unrelated to the codex-adapter focus and the backlog is saturated —
  not worth a slot this cycle.

## Cycle observations

- 2nd consecutive 0-proposal cycle (prior 19:18Z), but a saturation dry-cycle: backlog holds exactly N=2 workable items (TB-358, TB-359) and the only
  uncovered work (axis-6 tail) is correctly gated on the undispatched canary TB-360. The
  empty-cycles counter advancing here reflects backlog saturation: the daemon hasn't dispatched the
  workable wave yet, so the saturation persists.

## Decisions needed from operator

- None this cycle. Frozen is empty (no retry-exhausted escalations); no unadopted
  `cron_proposed` events in the recent-events block; the TB-358-class thinking-block-400
  halt the 17:14Z cycle surfaced is operator-resolved (`auto_approve_window_resume` ack
  17:51Z, operator_log) and is now additionally covered structurally by TB-356's effort
  step-down on the thinking-block-400 class.

## Proposals this cycle

Backlog already populated; no proposals this cycle.
# Ideation State

_Last updated: 2026-06-01T14:25:52Z by ideation cron_

## Mission alignment

5 most recent Completes — TB-369 (codex-handle daemon-start availability
gate, f8824c3), TB-368 (backend-aware Claude-SDK-availability gate, e3d1faa),
TB-367 (mixed `ideation=claude`/`task=codex` e2e through the adapter,
e927df5), TB-366 (residual `claude_agent_sdk` imports relocated behind
`ap2/adapters/` + import-direction gate, f2edcf4), TB-365
(`_run_control_agent` adapter-routed, cbcc137). No mission drift: each ship
completes the AgentAdapter seam — the "pluggable agent backend" Constraint
(goal.md L578-587) and the structural prerequisite for the downstream
OSS-distribution shape (Mission L18-20, Done-when L68-70). No new completes
since 08:37Z (TB-369); board is fully drained (Active/Ready/Backlog/Pipeline/
Frozen all empty).

## Current focus assessment

- **codex support through an agent adaptor layer** (7 axes)
  - Progress so far: all 7 axes shipped — axis1 `TB-353` (AgentAdapter ABC +
    ClaudeCodeAdapter), axis2 `TB-354` (normalized options/result/usage),
    axis3 `TB-355` (MCP tools through adapter), axis4 `TB-357` (CodexAdapter
    against the codex CLI), axis5 `TB-358` + `TB-368` + `TB-369` (per-kind
    `[agent_backends]` map + both backend-aware daemon-start credential
    gates), axis6 all six dispatch-site migrations (`TB-360`/`TB-362`/`TB-363`/
    `TB-364`/`TB-365`/`TB-366`), axis7 `TB-359` (parity suite + gated codex
    real-SDK smoke). Mixed-config e2e: `TB-367`.
  - Progress signals (goal.md L204-215) all green: (1) `claude_agent_sdk`
    import-boundary pinned (`TB-366`); (2) every dispatch site adapter-routed
    (`TB-360`/`TB-362`/`TB-363`/`TB-364`/`TB-365`); (3) mixed
    `ideation=claude`/`task=codex` runs each kind end-to-end (`TB-367`); (4)
    usage/cost/`ap2 status` read one normalized shape (`TB-354`); (5)
    adapter-contract parity suite passes both adapters (`TB-359`).
  - Gaps: none workable. Every dispatch site is migrated; both daemon-start
    gates exist (`TB-368`/`TB-369`); parity + gated codex smoke exist
    (`TB-359`). Any remaining idea (real-SDK smoke hardening, tool-definition
    abstraction, `ap2 status` backend-display) fails the focus delete-test
    (goal.md L198-202): it polishes Claude-path internals or adds scaffolding
    without moving a dispatch site behind the adapter or letting a second
    backend drive a kind.
  - Status: `exhausted-needs-operator`

## Non-goal risk check

none. Nothing drifted into a third backend or per-message/in-task routing
(respects goal.md L127-128); no ship changed any agent's prompt / tool policy /
verification semantics (respects L129-131).

## Considered & deferred this cycle

- **Greenfield ap2-meta polish to fill the 5 unused slots**: deferred.
  Manufacturing proposals to fill slots is exactly the "ap2-meta polish /
  scope creep" goal.md L46-48 forbids and fails the focus delete-test
  (L198-202).
- **`ap2 status` backend-per-kind display / codex observability**: deferred.
  Operator-facing polish that neither moves a dispatch site behind the adapter
  nor lets a backend drive a kind — fails the focus delete-test; belongs to a
  future focus, not this one.
- **Codex real-SDK smoke promotion / hardening**: deferred (carried). `TB-359`
  ships the gated codex real-SDK smoke on the 6h cron; no observed failure
  signal motivates hardening — speculative, matching the TB-240/172/175 veto
  pattern below.
- **Abstract the tool-DEFINITION mechanism (`@tool` schema) for Codex**:
  deferred (carried). `TB-355`/`TB-357` cover tool registration through the
  adapter; abstracting tool-definition for a non-Claude backend has no
  concrete caller yet — fails the delete-test.
- **Operator-rejection pattern (recurring)**: the `## Recent operator
  rejections` header + operator_log show vetoes clustering on (a) symptom-patch
  remediations without root-cause (TB-231) and (b) speculative
  enumerated-case validators guarding unobserved failures (TB-240, TB-172,
  TB-175). Both residual codex ideas above are type-(b) shaped — a second
  reason to hold them. Pattern re-noted so future cycles stay clear.

## Cycle observations

- Failure-review step found nothing this cycle: board fully drained, zero
  Frozen tasks, recent events carry only clean `task_complete` for
  TB-368/TB-369 with no `verification_failed`/`retry_exhausted`/
  `verification_partial`. No edit-briefing/split/follow-up remediation
  candidates exist — reinforcing that the dry cycle is genuine focus
  exhaustion, not a queue-management artifact.

## Decisions needed from operator

- Decision needed: define the next focus via `ap2 update-goal` (or explicitly
  confirm the goal is done). The codex-adaptor focus remains in progress; goal.md's Mission (L18-20) and
  Done-when (L68-70) both name a downstream "OSS distribution" focus framed as
  "which components default to enabled" plus packaging extras, but no
  `## Current focus` heading defines it yet. Unblock-condition: until a new
  active-focus heading exists, every ideation cycle has no in-progress focus to
  propose against and exits via a dry `ideation_cycle_summary` marker — this is
  now the 3rd consecutive dry cycle (the prior cycle reported the 2nd), so the
  empty-cycles halt counter is accruing toward `AP2_IDEATION_HALT_EMPTY_CYCLES`;
  adding the heading (or confirming done) lets the next cycle resume proposing
  goal-aligned work.

## Proposals this cycle

0 proposals; codex-adaptor focus marked `exhausted-needs-operator` — all 7
axes and all 5 Progress signals shipped, no focus-renting gap remains. Awaiting
operator to define the next (OSS-distribution) focus via `ap2 update-goal`.
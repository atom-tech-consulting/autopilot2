# Ideation State

_Last updated: 2026-06-01T10:20:30Z by ideation cron_

## Mission alignment

5 most recent Completes — TB-369 (codex-handle daemon-start availability gate,
f8824c3), TB-368 (backend-aware Claude-SDK-availability gate, e3d1faa), TB-367
(mixed-config `ideation=claude`/`task=codex` e2e through the adapter, e927df5),
TB-366 (residual `claude_agent_sdk` imports relocated behind `ap2/adapters/` +
import-direction gate, f2edcf4), TB-365 (`_run_control_agent` adapter-routed,
cbcc137). No mission drift: every ship completes the AgentAdapter seam — the
"pluggable agent backend" Constraint (goal.md L580-587) and the structural
prerequisite for the downstream OSS-distribution shape (L18-20, L68-70).

## Current focus assessment

- **codex support through an agent adaptor layer** (7 axes)
  - Progress so far: axis1 `TB-353`
    (AgentAdapter ABC + ClaudeCodeAdapter), axis2 `TB-354` (normalized
    AgentOptions/AgentResult/usage), axis3 `TB-355` (MCP tools through adapter),
    axis4 `TB-357` (CodexAdapter against the codex CLI), axis5 `TB-358` (per-kind
    `[agent_backends]` map + backend-aware credential gate) + `TB-368`
    (Claude-SDK-availability gate) + `TB-369` (codex-handle availability gate),
    axis6 all six dispatch-site migrations (`TB-360` scrub, `TB-362`
    verifier-judge, `TB-363` validator+janitor judge, `TB-364` run_task, `TB-365`
    `_run_control_agent`, `TB-366` residual-import consolidation), axis7 `TB-359`
    (parity suite + gated codex real-SDK smoke). Mixed-config e2e: `TB-367`.
  - Progress signals: (1) `claude_agent_sdk`
    import-boundary pinned by `TB-366`; (2) every dispatch site adapter-routed
    (`TB-360`/`TB-362`/`TB-363`/`TB-364`/`TB-365`); (3) mixed `ideation=claude`,
    `task=codex` runs each kind end-to-end (`TB-367`); (4) usage/cost/`ap2 status`
    read one normalized shape (`TB-354`); (5) adapter-contract parity suite passes
    for both adapters (`TB-359`).
  - Gaps: The prior cycle's single remaining edge case (codex
    daemon-start handle gate) shipped this cycle as `TB-369`. Every dispatch site
    is migrated, both daemon-start gates exist, parity + smoke exist. The two
    residual ideas (codex smoke hardening; tool-DEFINITION abstraction) have no
    concrete caller / failure signal and fail the focus delete-test (L198-202).

## Non-goal risk check

none. Nothing drifted into a third backend or per-message routing (respects
L127-128) and no ship changed any agent's prompt / tool policy / verification
semantics (respects L129-131). The focus closed within its declared scope.

## Considered & deferred this cycle

- **Greenfield ap2-meta polish to fill the 5 unused slots**: deferred.
  Manufacturing 5 proposals to fill slots is exactly the "ap2-meta polish /
  scope creep" goal.md L46-48 forbids and fails the focus delete-test (L198-202).
- **Codex real-SDK smoke promotion / hardening**: deferred (carried). `TB-359`
  ships the gated codex real-SDK smoke on the 6h cron; no observed failure signal
  motivates further hardening — speculative, matching the TB-240/172/175 veto.
- **Abstract the tool-DEFINITION mechanism (`@tool` schema) for Codex**: deferred
  (carried). `TB-355`/`TB-357` cover tool registration through the adapter;
  abstracting tool-definition for a non-Claude backend has no concrete caller yet.
- **Operator-rejection pattern (recurring)**: vetoes cluster on (a) symptom-patch
  remediations without root-cause (TB-231) and (b) speculative enumerated-case
  validators guarding unobserved failures (TB-240, TB-172, TB-175). Pattern
  noted so future cycles stay clear.

## Cycle observations

- The codex focus is structurally complete and lands the "backend-pluggable core"
  the Mission (L18-20) names as the prerequisite for an OSS-distribution focus —
  so the natural next focus is well-seeded, pending operator definition.
- `insights/_index.md`: `test-suite-slowness-2026-05-17.md` carries `(no tldr —
  needs update)` with `?` dates — a malformed insight (missing YAML front
  matter). Not operator-actionable and not focus-relevant this cycle; noted only
  so a future grounding pass doesn't mistake it for fresh signal.

## Decisions needed from operator

- Define the next focus via `ap2 update-goal` — goal.md's Mission (L18-20, L68-70) repeatedly points at a
  downstream "OSS distribution" focus framed as "which components default to
  enabled" + packaging extras. Unblock-condition: without a new `## Current focus`
  heading the next ideation cycle has no in-progress focus to propose against and
  must keep declaring exhaustion / parking ideation; adding one lets the next
  cycle resume proposing goal-aligned work.

## Proposals this cycle

0 proposals; codex-support focus marked `exhausted-needs-operator`.
Awaiting operator to define the next (OSS-distribution) focus via `ap2
update-goal`.
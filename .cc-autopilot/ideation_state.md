# Ideation State

_Last updated: 2026-06-01T12:23:30Z by ideation cron_

## Mission alignment

5 most recent Completes — TB-369 (codex-handle daemon-start availability
gate, f8824c3), TB-368 (backend-aware Claude-SDK-availability gate, e3d1faa),
TB-367 (mixed-config `ideation=claude`/`task=codex` e2e through the adapter,
e927df5), TB-366 (residual `claude_agent_sdk` imports relocated behind
`ap2/adapters/` + import-direction gate, f2edcf4), TB-365 (`_run_control_agent`
adapter-routed, cbcc137). No mission drift: every ship completes the
AgentAdapter seam — the "pluggable agent backend" Constraint (goal.md L578-587)
and the structural prerequisite for the downstream OSS-distribution shape
(Mission L18-20, Done-when L68-70).

## Current focus assessment

- **codex support through an agent adaptor layer** (7 axes)
  - Progress so far: axis1 `TB-353` (AgentAdapter ABC + ClaudeCodeAdapter),
    axis2 `TB-354` (normalized AgentOptions/AgentResult/usage), axis3 `TB-355`
    (MCP tools through adapter), axis4 `TB-357` (CodexAdapter against the codex
    CLI), axis5 `TB-358` (per-kind `[agent_backends]` map + backend-aware
    credential gate) + `TB-368` (Claude-SDK-availability gate) + `TB-369`
    (codex-handle availability gate), axis6 all six dispatch-site migrations
    (`TB-360` scrub, `TB-362` verifier-judge, `TB-363` validator+janitor judge,
    `TB-364` run_task, `TB-365` `_run_control_agent`, `TB-366` residual-import
    consolidation), axis7 `TB-359` (parity suite + gated codex real-SDK smoke).
    Mixed-config e2e: `TB-367`.
  - Progress signals (goal.md L204-215): (1) `claude_agent_sdk` import-boundary
    pinned by `TB-366`; (2) every dispatch site adapter-routed (`TB-360`/
    `TB-362`/`TB-363`/`TB-364`/`TB-365`); (3) mixed `ideation=claude`,
    `task=codex` runs each kind end-to-end (`TB-367`); (4) usage/cost/`ap2
    status` read one normalized shape (`TB-354`); (5) adapter-contract parity
    suite passes for both adapters (`TB-359`).
  - Gaps: none workable. Every dispatch site is migrated, both daemon-start
    credential gates exist (`TB-368`/`TB-369`), parity + gated codex smoke
    exist (`TB-359`).

## Non-goal risk check

none. Nothing drifted into a third backend or per-message routing (respects
goal.md L127-128); no ship changed any agent's prompt / tool policy /
verification semantics (respects L129-131).

## Considered & deferred this cycle

- **Greenfield ap2-meta polish to fill the 5 unused slots**: deferred.
  Manufacturing 5 proposals to fill slots is exactly the "ap2-meta polish /
  scope creep" goal.md L46-48 forbids and fails the focus delete-test (L198-202).
- **Codex real-SDK smoke promotion / hardening**: deferred (carried). `TB-359`
  ships the gated codex real-SDK smoke on the 6h cron; no observed failure
  signal motivates further hardening — speculative, matching the
  TB-240/172/175 veto pattern below.
- **Abstract the tool-DEFINITION mechanism (`@tool` schema) for Codex**:
  deferred (carried). `TB-355`/`TB-357` cover tool registration through the
  adapter; abstracting tool-definition for a non-Claude backend has no concrete
  caller yet — fails the delete-test.
- **Operator-rejection pattern (recurring)**: the `## Recent operator
  rejections` header + operator_log show vetoes clustering on (a) symptom-patch
  remediations without root-cause (TB-231) and (b) speculative
  enumerated-case validators guarding unobserved failures (TB-240, TB-172,
  TB-175). Both residual codex ideas above are type-(b) shaped, which is a
  second reason to hold them. Pattern re-noted so future cycles stay clear.

## Cycle observations

- `insights/_index.md`: `test-suite-slowness-2026-05-17.md` still carries `(no
  tldr — needs update)` with `?` dates — malformed YAML front matter, unchanged
  since prior cycle. Carried (not promoted): still accurate and not
  operator-actionable, kept only so a future grounding pass doesn't mistake the
  empty tldr for fresh signal. Not focus-relevant this cycle.

## Decisions needed from operator

- Decision needed: define the next focus via `ap2 update-goal` (or explicitly
  confirm the goal is done). goal.md's
  Mission (L18-20) and Done-when (L68-70) both name a downstream "OSS
  distribution" focus framed as "which components default to enabled" plus
  packaging extras, but no `## Current focus` heading defines it yet.
  Unblock-condition: until a new active-focus heading exists, every ideation
  cycle has no in-progress focus to propose against and will keep exiting via
  dry `ideation_cycle_summary` markers (this is the 2nd consecutive dry cycle —
  the empty-cycles halt counter is now accruing toward
  `AP2_IDEATION_HALT_EMPTY_CYCLES`); adding the heading lets the next cycle
  resume proposing goal-aligned work.

## Proposals this cycle

0 proposals; awaiting operator to define the next
(OSS-distribution) focus via `ap2 update-goal`.
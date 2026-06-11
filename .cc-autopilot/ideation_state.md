# Ideation State

_Last updated: 2026-06-11T05:47Z by ideation cron_

## Mission alignment

5 most recent Completes — TB-398 (carved howto's config knobs + config-keys +
codex backend setup into `skills/ap2-config/SKILL.md`, retargeted env-knob &
config-key drift gates, 40b1db5); TB-397 (canary: carved the observability
domain into `skills/ap2-observability/SKILL.md`, retargeted the event-type
drift gate, established SKILL.md + in-commit gate-retarget conventions,
d16a92c); TB-396 (provider-neutral `agent_model` default → None, c2597f1);
TB-395 (synced `skills/ap2/SKILL.md` + howto to the task_solve/task_verify
event vocabulary, 64e9af2); TB-392 (minimal-kernel dispatch→verify→report e2e
green with every component disabled). All serve the Mission's "portable skills
as the prerequisite for an OSS cut" — TB-397/398 are the first two carves
turning the howto monolith into auto-triggered domain skills.

## Current focus assessment

- **Current focus: consolidate the operator manual into auto-triggered,
  cross-runtime skills** (goal.md L101)
  - Progress so far: the canary settled and the convention is proven on two
    carves — TB-397 carved the observability domain + retargeted the event-type
    drift gate; TB-398 carved config knobs/keys/backend-setup + retargeted the
    env-knob & config-key gates (plus displaced docs-location pins), both
    leaving a one-line howto pointer. In flight (operator-approved, Backlog):
    TB-399 (board-ops CLI-verb + MCP-tool reference + gate retarget), TB-400
    (fold task-agent contract + verification-bullet authoring + classify-verdicts
    into `skills/ap2-task/`), TB-401 (axis-3 cross-runtime `~/.agents/skills` +
    AGENTS.md + managed pointer).
  - Gaps:
    (1) axis 1 — three howto domains still uncarved: **monitoring/status**
    (`## Components enumeration (ap2 status)` howto L776), **ideation + goal/focus
    management** (`## Authoring goal.md` L58 + `## Retrospective audit workflow`
    L629), **failure-recovery / operator-playbook** (`## Failure modes the daemon
    recovers from` L481 + `## Operator-question playbook` L910). This cycle
    proposes ideation+goal/focus (TB-403) and failure-recovery (TB-402);
    monitoring/status deferred (see below).
    (2) axis 2 — drift gates are being retargeted carve-by-carve; residual gates
    stay on `HOWTO_PATH` until the matching section is carved. No standalone
    work — each carve owns its gate retarget in-commit.
    (3) The final "retire `ap2/howto.md` as a file + drop the sync-assets howto
    target + flip any residual gates" task depends on EVERY carve landing; not
    yet rankable.
  - Status: `in-progress`

## Non-goal risk check

Docs/tooling restructure with no daemon behavior change (focus L117), so
"Removing behavior during component extraction" and "API stability" non-goals
don't bind. Real risk: over-fragmentation — goal.md L130-133 caps at ~6-9
coherent domain skills because summaries load up front. Mitigated by carving by
operator domain (not per-subcommand) and deferring monitoring/status until
TB-399's board-ops `ap2 status` reference lands, so its residual boundary is
visible before carving. One content-boundary guard: the ideation-goals skill
must NOT move the daemon ideation agent's briefing-authoring conventions — those
stay canonical in `ideation.default.md` (goal.md L126-129); the briefing pins
this. none otherwise.

## Considered & deferred this cycle

- **ap2-monitoring/status skill** (`## Components enumeration (ap2 status)`):
  deferred — its `ap2 status` surface overlaps both the already-carved
  observability skill (TB-397 stats/logs) and TB-399's in-flight board-ops
  CLI/MCP reference. Carving it now risks a boundary dispute / re-carve, the
  duplicate-axis shape the operator rejected in TB-384. Wait for TB-399 to land,
  then carve the residual status content.
- **Final "retire howto.md + drop sync-assets howto target + flip residual
  gates" task**: NOT proposed — depends on every carve landing first; proposing
  it now is the out-of-sequence axis shape rejected in TB-384.
- **Recurring operator-rejection pattern**: vetoes punish out-of-sequence /
  duplicate axis work (TB-384) and speculative enumerated-case validators
  (TB-172, TB-231, TB-240). Both proposals this cycle are in-sequence domain
  carves (the canary settled via TB-397/398) and add no enumerated-case lint.

## Cycle observations

- Carve↔gate coupling is load-bearing for every carve briefing: moving a gated
  howto section while its docs-drift gate still reads `HOWTO_PATH` breaks
  `uv run pytest`, so each carve must retarget its gate in-commit (TB-397/398
  both did). The briefing tells the agent to grep `ap2/tests/` for the displaced
  gate and retarget it; the full-suite + `test_docs_drift.py` bullets gate it.
  Carried because it dictates this wave's task shape.
- Queue-append's hard-predecessor judge (TB-235) is non-deterministic on
  identically-phrased carves: TB-403's briefing tripped the TB-397-predecessor
  check (leans on the canary's on-disk artifacts as templates) while TB-402's
  identical claim did not — resolved by gating TB-403 `@blocked:review,TB-397`
  (TB-397 already Complete, so it's a satisfied no-op + review gate). One-time
  note — not carried.

## Decisions needed from operator

none this cycle — the remaining skill taxonomy is expressed AS the review-gated
proposals (TB-402, TB-403); the operator steers boundaries by approving /
rejecting / reordering them. No unadopted `cron_proposed` events in the recent
window to surface.

## Proposals this cycle

- TB-402 — carve howto's `## Failure modes the daemon recovers from` +
  `## Operator-question playbook` into `skills/ap2-failure-recovery/` +
  retarget displaced gates (auto-recovery + operator-intervention playbook).
- TB-403 — carve howto's `## Authoring goal.md` + `## Retrospective audit
  workflow` into `skills/ap2-ideation-goals/` + retarget displaced gates
  (operator-facing goal/focus authoring + proposal-quality retrospective;
  `ideation.default.md` stays canonical for daemon briefing conventions).
  Gated `@blocked:review,TB-397`.
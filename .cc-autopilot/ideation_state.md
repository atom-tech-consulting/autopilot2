# Ideation State

_Last updated: 2026-06-11T10:23Z by ideation cron_

## Mission alignment

5 most recent Completes — TB-399 (carved howto's Custom MCP tools + Operator
CLI verbs into `skills/ap2-board-ops/SKILL.md`, ec52162); TB-400 (folded the
task-agent contract + verification-bullet authoring + classify-verdicts into
`skills/ap2-task/SKILL.md`, noting `ideation.default.md` stays canonical,
2c87c16); TB-401 (cross-runtime deploy: `~/.agents/skills` target + `AGENTS.md`
+ managed discovery pointer in CLAUDE.md / codex AGENTS.md, 4cea878); TB-402
(carved failure-modes + operator-question playbook into
`skills/ap2-failure-recovery/SKILL.md`, e0b9789); TB-403 (carved goal/focus
authoring + retrospective audit into `skills/ap2-ideation-goals/SKILL.md`,
398a60e). All serve the Mission's "portable skills as the OSS-cut
prerequisite": the operator manual is now seven domain skills + a cross-runtime
deploy, with only one residual section carve + the file-retirement step left.

## Current focus assessment

- **Current focus: consolidate the operator manual into auto-triggered,
  cross-runtime skills** (goal.md L101)
  - Progress so far: six domain carves shipped — observability (TB-397),
    config + env/config-key gate retarget (TB-398), board-ops CLI/MCP refs
    (TB-399), task + verification-bullet authoring folded into the existing
    ap2-task skill (TB-400), failure-recovery (TB-402), ideation/goal-focus
    (TB-403) — plus the cross-runtime deploy (TB-401: `~/.agents/skills` +
    `AGENTS.md` + managed pointer). Of the five Progress signals, four are
    already done: config-knob drift gate retargeted to ap2-config (TB-398),
    architecture.md untouched, daemon-agent conventions kept canonical in
    `ideation.default.md` (TB-400), skills deployed cross-runtime (TB-401).
  - Gaps: exactly two, both already in Backlog awaiting operator review:
    (1) axis 1 — the last domain carve: `## Components enumeration (ap2 status)`
    folds into ap2-observability (TB-405, pending review).
    (2) axes 2+3 file-retirement: relocate residual orientation/core sections,
    drop the sync-assets `ap2-howto.md` target (TB-401 deliberately kept it
    additive), flip residual `HOWTO_PATH` gates, delete the file (TB-406,
    pending review, hard-sequenced `@blocked:review,TB-405`).
  - Status: `in-progress`

## Non-goal risk check

Docs/tooling restructure, no daemon behavior change (focus L117) — the
component-extraction and API-stability non-goals don't bind. Over-fragmentation
risk (goal.md L130-133, ~6-9 skill cap) stays mitigated: TB-405 folds the
~75-line components-enumeration into the existing ap2-observability skill rather
than spawning an 8th thin skill. none otherwise.

## Considered & deferred this cycle

- **Project-wide `howto.md`-reference scrub task**: deferred — premature before
  TB-406 runs. Residual dangling references (README, code comments, the
  sandbox.py inline-flagged `ap2-howto.md` target) sit inside TB-406's "delete
  the file" scope and are best assessed from what TB-406 actually leaves behind,
  not pre-guessed into a parallel task.
- **SKILL.md frontmatter / trigger-boundary validator** (assert every skill has
  name+description, descriptions don't overlap): deferred — this is the
  speculative enumerated-case validator shape the operator has vetoed three
  times (TB-172, TB-231, TB-240); no observed drift failure justifies it.
- **Recurring operator-rejection pattern**: vetoes target out-of-sequence /
  duplicate-axis work (TB-384) and speculative enumerated-case
  validators/linters (TB-172, TB-231, TB-240). Both remaining steps are already
  proposed in dependency order (TB-405 → TB-406); a third task this cycle would
  land in one of those two vetoed shapes, so I propose none.

## Cycle observations

- Focus is in wind-down: all five Progress signals are done or covered by the
  two pending-review tasks (TB-405, TB-406), so there is no third high-leverage,
  in-sequence, non-fragmenting proposal to rank — this is a 0-proposal cycle by
  design, not for lack of slots (N=3). (Prior cycle's three observations —
  carve↔gate coupling, TB-235 predecessor-judge friction, the board-edit
  ID-allocation footgun — were all task-shaping or one-time notes; with no new
  task proposed they no longer inform reasoning and are dropped.)

## Decisions needed from operator

none this cycle — the two remaining steps are expressed AS the review-gated
proposals already in Backlog (TB-405 carve, TB-406 retire, hard-sequenced); the
operator advances them via `ap2 approve`. No unadopted `cron_proposed` events in
the recent window to surface.

## Proposals this cycle

Backlog already populated; no proposals this cycle. The two Backlog tasks
(TB-405 last carve, TB-406 file retirement) cover the full remaining focus
scope; adding a third would duplicate an axis or take a vetoed validator shape.
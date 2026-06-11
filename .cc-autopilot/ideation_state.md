# Ideation State

_Last updated: 2026-06-11T12:26Z by ideation cron_

## Mission alignment

5 most recent Completes — TB-399 (carved howto's Operator CLI verbs + Custom MCP
tools into `skills/ap2-board-ops/SKILL.md`, ec52162); TB-400 (folded the
task-agent contract + verification-bullet authoring + classify-verdicts into
`skills/ap2-task/SKILL.md`, keeping `ideation.default.md` canonical, 2c87c16);
TB-401 (cross-runtime deploy: `~/.agents/skills` target + `AGENTS.md` + managed
discovery pointers, additive — `ap2-howto.md` target retained, 4cea878); TB-402
(carved failure-modes + operator-question playbook into
`skills/ap2-failure-recovery/SKILL.md`, e0b9789); TB-403 (carved goal/focus
authoring + retrospective audit into `skills/ap2-ideation-goals/SKILL.md`,
398a60e). All serve the Mission's "portable skills as the OSS-cut
prerequisite": the operator manual is now seven domain skills + a cross-runtime
deploy, with only one residual carve + the file-retirement step left.

## Current focus assessment

- **Current focus: consolidate the operator manual into auto-triggered,
  cross-runtime skills** (goal.md L101)
  - Progress so far: six domain carves shipped — observability (TB-397, the
    canary that set the SKILL.md + gate-retarget conventions), config + env/
    config-key gate retarget (TB-398), board-ops CLI/MCP refs (TB-399), task +
    verification-bullet authoring folded into the existing ap2-task skill
    (TB-400), failure-recovery (TB-402), ideation/goal-focus (TB-403) — plus the
    cross-runtime deploy (TB-401: `~/.agents/skills` + `AGENTS.md` + managed
    pointer). Against goal.md's five Progress signals: drift gates retargeted
    onto skills (TB-397/398/399/400/402/403), `architecture.md` left standalone
    (untouched by the carves), skills deployed cross-runtime (TB-401), and
    daemon-relied briefing conventions kept canonical in `ideation.default.md`
    (TB-400, TB-403).
  - Gaps: exactly two, both already in Backlog awaiting operator review:
    (1) axis 1 — the last domain carve: `## Components enumeration (ap2 status)`
    folds into ap2-observability (TB-405).
    (2) axes 2+3 file-retirement: relocate residual orientation/core sections,
    drop the sync-assets `ap2-howto.md` target (TB-401 deliberately kept it
    additive per its summary + the inline `ap2/sandbox.py` flag), flip residual
    `HOWTO_PATH` gates, delete the file (TB-406, hard-sequenced
    `@blocked:review,TB-405`).
  - Status: `in-progress`

## Non-goal risk check

Docs/tooling restructure, no daemon behavior change (focus L117) — the
component-extraction and API-stability non-goals don't bind. Over-fragmentation
risk (goal.md L130-133, ~6-9 skill cap) stays mitigated: there are seven domain
skills today and TB-405 folds the ~75-line components-enumeration into the
existing ap2-observability skill rather than spawning an 8th thin skill. none
otherwise.

## Considered & deferred this cycle

- **Project-wide `howto.md`-reference scrub task**: deferred — premature before
  TB-406 runs. Residual dangling references (README, code comments, the
  `ap2/sandbox.py` inline-flagged `ap2-howto.md` target) sit inside TB-406's
  "delete the file" scope and are best assessed from what TB-406 actually
  leaves behind, not pre-guessed into a parallel task.
- **SKILL.md frontmatter / trigger-boundary validator** (assert every skill has
  name+description, descriptions don't overlap): deferred — this is the
  speculative enumerated-case validator shape the operator has vetoed three
  times (TB-172, TB-231, TB-240); no observed drift failure justifies it.
- **Recurring operator-rejection pattern**: vetoes target out-of-sequence /
  duplicate-axis work (TB-384, the most-recent rejection) and speculative
  enumerated-case validators/linters (TB-172, TB-231, TB-240). Both remaining
  steps are already proposed in dependency order (TB-405 → TB-406); a third task
  this cycle would land in one of those two vetoed shapes, so I propose none.

## Cycle observations

- Focus is in wind-down: both remaining steps are the two pending-review tasks
  (TB-405 carve, TB-406 retire), so there is no third high-leverage, in-sequence,
  non-fragmenting proposal to rank — this is a 0-proposal cycle by design, not
  slot starvation (N=3). (Carried from the prior cycle with re-justification: it
  is still the operative reason this cycle produces no proposals, and no board
  change since TB-403 completed at 08:08Z has altered it.)
- Orphan proposal record `ideation_proposals/TB-404.json` (self-referential
  `blocked_on: "review,TB-404"`, briefing `retire-...-r.md`) is a superseded
  duplicate of the retire-howto task; the live retire task is TB-406 (briefing
  `retire-...-r-2.md`, `@blocked:review,TB-405`). Recorded so a future audit of
  proposal-records-vs-board doesn't re-investigate the TB-404 mismatch.

## Decisions needed from operator

none this cycle — there is nothing requiring operator narrative judgment to
unblock the next ideation cycle. The two remaining steps are already expressed
as review-gated proposals; advancing them is routine `ap2 approve`, surfaced
mechanically by `ap2 status` / the cron status-report, not an ideation
escalation. No unadopted `cron_proposed` events in the recent window to surface.

## Proposals this cycle

0 proposals. The two Backlog tasks (TB-405 last carve, TB-406 file retirement)
cover the full remaining focus scope; a third proposal this cycle would either
duplicate a remaining axis (the TB-384 veto shape) or take a speculative
validator/linter shape (the TB-172/231/240 veto shape).
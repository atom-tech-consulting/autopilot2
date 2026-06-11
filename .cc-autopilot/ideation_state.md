# Ideation State

_Last updated: 2026-06-11T08:14Z by ideation cron_

## Mission alignment

5 most recent Completes — TB-399 (carved howto's Custom MCP tools + Operator
CLI verbs into `skills/ap2-board-ops/SKILL.md`, retargeted the CLI-verb gate,
ec52162); TB-400 (folded the task-agent contract + verification-bullet
authoring + classify-verdicts into `skills/ap2-task/SKILL.md`, 2c87c16); TB-401
(cross-runtime deploy: `~/.agents/skills` target + `AGENTS.md` + managed
discovery pointer, 4cea878); TB-402 (carved failure-modes + operator-question
playbook into `skills/ap2-failure-recovery/SKILL.md`, e0b9789); TB-403 (carved
goal/focus authoring + retrospective audit into `skills/ap2-ideation-goals/
SKILL.md`, 398a60e). All serve the Mission's "portable skills as the
prerequisite for an OSS cut" — six of the planned ~6-9 domain carves have now
landed, leaving exactly one domain section + the file-retirement step.

## Current focus assessment

- **Current focus: consolidate the operator manual into auto-triggered,
  cross-runtime skills** (goal.md L101)
  - Progress so far: six domain carves shipped — observability (TB-397),
    config (TB-398), board-ops (TB-399), task + verification authoring
    (TB-400), failure-recovery (TB-402), ideation/goal-focus (TB-403) — plus
    the cross-runtime deploy (TB-401). `ap2/howto.md` is down to 330 lines:
    one substantive uncarved operator-domain section
    (`## Components enumeration (ap2 status)`, L186-259), the orientation/core
    prose (What ap2 is, On-disk layout, daemon tick, Verification summary,
    Sandbox/Convergence models, Reading order), and pointer stubs for the six
    carved domains.
  - Gaps:
    (1) axis 1 — ONE domain left: monitoring/status
    (`## Components enumeration (ap2 status)`). This cycle proposes carving it
    into the existing ap2-observability skill (TB-405).
    (2) the file-retirement final step: relocate the residual orientation/core
    sections, drop the `ap2-howto.md` sync-assets deploy target (which
    `ap2/sandbox.py` itself flags in-line — "the `ap2-howto.md` target stays
    until the later howto-retirement task", L636/L858), flip the residual
    `HOWTO_PATH`-keyed test gates, delete the file. Proposed as TB-406,
    hard-sequenced after TB-405 via `@blocked:review,TB-405`.
    (3) axis 3 cross-runtime deploy shipped (TB-401) but kept the
    `ap2-howto.md` target additive; its removal is folded into TB-406, not a
    separate task.
  - Status: `in-progress`

## Non-goal risk check

Docs/tooling restructure, no daemon behavior change (focus L117) — the
component-extraction and API-stability non-goals don't bind. Real risk:
over-fragmentation (goal.md L130-133 caps at ~6-9 coherent skills). Mitigated by
folding the ~75-line components-enumeration into the existing ap2-observability
skill (stays at 7 domain skills) rather than spawning an 8th thin skill.
Boundary guards in TB-405: the AP2_* env-flag tuning catalogue stays canonical
in ap2-config and the `ap2 status` CLI verb in ap2-board-ops (cross-ref, no
duplication); daemon ideation briefing conventions stay in
`ideation.default.md` (goal.md L126-129), untouched by either proposal. none
otherwise.

## Considered & deferred this cycle

- **Standalone `ap2-monitoring` skill**: deferred — the components-enumeration
  section is ~75 lines, too thin for its own skill, and `ap2 status` runtime
  monitoring sits naturally in the already-carved ap2-observability domain.
  Folding keeps the skill count at 7 within the L130-133 anti-fragmentation
  cap. Operator can redirect to a standalone skill via TB-405's review gate.
- **Deployed-path skill→skill cross-reference resolution** (axis 2 "resolve at
  deployed paths"): folded into TB-406's cross-ref cleanup rather than a
  separate task — avoids over-fragmentation and the speculative enumerated-case
  validator shape the operator has rejected (TB-172/231/240).
- **Recurring operator-rejection pattern**: vetoes punish out-of-sequence /
  duplicate-axis work (TB-384) and speculative enumerated-case validators
  (TB-172, TB-231, TB-240). TB-405 is the last in-sequence carve; TB-406 is
  hard-sequenced AFTER it (a genuine predecessor, not a parallel duplicate
  axis); neither adds an enumerated-case lint.

## Cycle observations

- Carve↔gate coupling, refined: prose-only sections with no `HOWTO_PATH`-keyed
  coverage gate (failure-recovery TB-402, and now components-enumeration)
  register a docs-location pin (skill-path constant + no-duplication assert) in
  `test_docs_drift.py` rather than retargeting a gate — this dictates TB-405's
  verification shape (a pin, not a gate flip). Carried: directly shapes this
  cycle's task.
- TB-235 predecessor-judge friction (non-deterministic, per the 2026-06-11
  05:47Z note): the carve briefing's first attempt was rejected because it
  cited "mirroring the FAILURE_RECOVERY pattern (TB-402)" — the judge read the
  completed TB-402 as a hard predecessor. Resolved by scrubbing completed-TB
  citations from the carve briefing (describe the pin shape directly) rather
  than adding a satisfied-no-op `@blocked`. One-time note — not carried.
- Board-edit ID-allocation footgun (this cycle, recovered): `do_board_edit`
  auto-allocates the TB-N via `_allocate_id` and ignores the `task_id` arg, so
  when the first add in a paired batch is rejected, the second add takes the
  freed ID — the retire task landed as TB-404 with a self-referential
  `@blocked:review,TB-404`. Recovered by `remove TB-404` + re-adding the pair
  in dependency order (carve TB-405, retire TB-406). Lesson: add proposals one
  at a time and confirm each allocated ID before referencing it in a dependent
  briefing. One-time note — not carried.

## Decisions needed from operator

none this cycle — the remaining two steps are expressed AS the review-gated
proposals (TB-405 carve, TB-406 retire); the operator steers the boundary call
(fold-into-observability vs a standalone monitoring skill) and the sequencing by
approving / rejecting / reordering them. No unadopted `cron_proposed` events in
the recent window to surface.

## Proposals this cycle

- TB-405 — carve `## Components enumeration (ap2 status)` into the
  ap2-observability skill (last domain carve), leave a pointer stub, register a
  docs-location pin; env-flag tuning stays in ap2-config, status verb in
  ap2-board-ops.
- TB-406 — retire `ap2/howto.md` as a file: relocate residual orientation/core
  sections, drop the sync-assets `ap2-howto.md` target, flip residual
  `HOWTO_PATH` gates, delete the file. Gated `@blocked:review,TB-405`.
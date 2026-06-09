# Ideation State

_Last updated: 2026-06-09T04:27Z by ideation cron_

## Mission alignment

5 most recent Completes — TB-385 (`task_start`→`task_solve` + `verify_passed`/`judge_call`
folded into one terminal `task_verify` event, commit d8c8865), TB-383 (board_edit made
policy-free; auto-approve gate chain moved to a real PRE_DISPATCH loop pass, commit 563e9d0),
TB-382 (prose-judge extracted into a `verifier_judge` COMPONENT — now known wrong-direction,
commit e98d4d1), TB-381 (pipeline/cron canary tick-phase, 29c3fe6), TB-380 (env-staleness WARN
fix) — all serve the reframed component-boundary focus (Mission L11-24: small core + loop-level
components behind a uniform registry; prerequisite for an OSS cut). No drift. TB-385's
event-slimming is observability hygiene riding alongside; doesn't pay focus rent directly but
isn't drift either.

## Current focus assessment

Focus: **get the component boundary right — loop-level participants only**. Cron (axis 1)
landed; axis-3 decouple landed (TB-383); axes 2, 5a, 5b all queued as pending-review Backlog
tasks; axis 4 deferred by sequence.

- **(1) Cron component — LANDED (canary)**
  - Progress so far: cron runs as a `CRON_DISPATCH` tick-hook with a job-handler registry
    (TB-381, 29c3fe6).
  - Gaps: residual only — bespoke `cron_job_handlers()` folds into the generic accessor
    (axis 5b, TB-387, queued).
  - Status: `in-progress`

- **(2) Communication component (inbound + outbound)**
  - Progress so far: none against this axis yet.
  - Gaps: `channel_adapters()` still core-walked; `inbound_poll` hook at daemon; mattermost
    top-level — all covered by TB-389 (queued, `@blocked:review`).
  - Status: `in-progress`

- **(3) Decouple auto-approve from `board_edit`; remove POST_DISPATCH**
  - Progress so far: `board_edit` is policy-free; auto-approve runs as a real PRE_DISPATCH pass
    `run_auto_approve_pass` (TB-383, 563e9d0).
  - Gaps: dead `POST_DISPATCH` phase still defined + walked with zero registrants — folded into
    TB-388 (queued). TB-383's briefing predated the 22:40Z reframe so the removal shipped
    separately.
  - Status: `in-progress`

- **(4) Ideation component**
  - Progress so far: `Phase.IDEATION` reserved + walked (empty) since TB-381.
  - Gaps: `ap2/ideation.py::_maybe_ideate` + `ideation_halt.py` still core; not behind the
    IDEATION hook.
  - Status: `in-progress`

- **(5) Judges are adapters not components; one generic registry verb**
  - Progress so far: the `select_adapter` seam already exists (adapters/select.py); no demotion
    landed yet — TB-382 went the wrong way (verifier_judge-as-component) and is to be reverted.
  - Gaps: `verifier_judge` IS a component (revert), `validator_judge` still a component, registry
    still carries per-kind methods + core→component `hook_points` symbol-pull blocks — covered by
    TB-386 (5a) + TB-387 (5b-i) + TB-388 (5b-ii), all queued `@blocked:review`.
  - Status: `in-progress`

## Non-goal risk check

none. All four queued tasks are structural moves preserving env-knob names + event payloads
(L297-301 "Removing behavior during component extraction" non-goal respected). No goal.md
mutation, no cron-schedule change, no push.

## Considered & deferred this cycle

- **Axis 4 — ideation component (TB-390 candidate)**: deferred again. Goal L209-213 sequences it
  last; its briefing would reference the `contributions(point)` accessor TB-387 introduces, so
  it's premature until 5a/5b (TB-386/387) land. Re-propose once those clear review and ship.
- **Recurring operator-rejection pattern**: TB-384 (a separate cron task) rejected 2026-06-08
  21:24Z as redundant once TB-381 became the cron canary; earlier vetoes punished symptom-patches
  (TB-231) + speculative enumerated-case validators (TB-240, TB-172). Lesson reinforced: don't
  propose parallel/duplicate or out-of-sequence axis work while the canonical sequence is in
  flight — proposing axis 4 now would court the same redundancy veto.
- **Backlog already carries the whole remaining in-sequence frontier**: TB-386/387/388/389 cover
  axes 5a, 5b-i, 5b-ii+axis-3-residual, and 2. With slot=1 and 4 workable items pending review,
  no greenfield slot is warranted.

## Cycle observations

- TB-382 passed verification THEN the 22:40Z reframe invalidated its direction — a
  structurally-valid completion that is goal-wrong; the revert is folded into TB-386 (5a) rather
  than surfaced as a separate abandon, since the operator already logged the revert decision
  (23:31Z). Carried from last cycle: still the only completion whose status the next cycle must
  read as "done but to-be-undone" when reasoning about axis 5.

## Decisions needed from operator

None this cycle. The reframe (22:40Z) is fresh and unambiguous; the TB-382 revert is already
operator-decided (logged 23:31Z); the four queued tasks map 1:1 to reframed axes with explicit
delete-tests. Pending-review TB-Ns are surfaced mechanically by `ap2 status` — not duplicated
here (TB-182).

## Proposals this cycle

Backlog already populated; no proposals this cycle. TB-386/387/388/389 cover every in-sequence
remaining axis (2, 5a, 5b-i, 5b-ii); the only uncovered axis (4, ideation component) is
sequence-blocked on TB-386/387 shipping.
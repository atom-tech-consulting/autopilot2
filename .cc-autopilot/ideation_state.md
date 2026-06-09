# Ideation State

_Last updated: 2026-06-09T00:20Z by ideation cron_

## Mission alignment

The 5 most recent Completes — TB-383 (board_edit made policy-free + auto-approve
moved to a PRE_DISPATCH loop pass, commit 563e9d0), TB-382 (prose-judge extracted
into a `verifier_judge` COMPONENT, commit e98d4d1), TB-381 (cron component canary,
commit 29c3fe6), TB-380 (env-staleness WARN classification), TB-379 (`ap2 status`
live effective-config) — serve the reframed component-boundary focus. Mission
L11-24 frames ap2 as a small core + components behind a uniform registry, the
prerequisite for an OSS cut. No drift.

Load-bearing context: the operator REFRAMED the focus (4 `update_goal` ops
through 2026-06-08T22:40Z + ack/note 23:31Z). New boundary: a component = a
loop-level participant only; LLM judges are `select_adapter` layers NOT
components; one generic `contributions(point)` accessor; remove dead
POST_DISPATCH. This INVALIDATES TB-382's direction (verifier-judge-as-component)
— operator-logged "wrong direction, to be reverted, do not build on it."

## Current focus assessment

Focus: **get the component boundary right — loop-level participants only**. Cron
(axis 1) + board_edit-decouple (part of axis 3) have landed; the rest is open.

- **(1) Cron component — LANDED (canary)**
  - Progress so far: cron scheduler runs as a `Phase.CRON_DISPATCH` tick-hook
    component with a job-handler registry (TB-381, commit 29c3fe6).
  - Gaps: residual only — its bespoke `cron_job_handlers()` (registry.py:513)
    folds into the generic accessor (axis 5b, TB-387).

- **(2) Communication component (inbound + outbound)**
  - Progress so far: none against this axis.
  - Gaps: `channel_adapters()` still walked by core (daemon.py:2137,
    watchdog.py:96/125, smoke_runner.py:163, attention/impl.py:1155);
    `hook_points["inbound_poll"]` at daemon.py:2097; mattermost is a top-level
    component.
  - Status: `in-progress`
  - Reasoning: independent axis, untouched (TB-389).

- **(3) Decouple auto-approve from `board_edit`; remove POST_DISPATCH**
  - Progress so far: board_edit is policy-free; auto-approve runs as a
    PRE_DISPATCH pass (TB-383, components/auto_approve/manifest.py:204).
  - Gaps: `Phase.POST_DISPATCH` still defined (registry.py:178) AND still walked
    (daemon.py:2747) with zero registrants — the "dead phase walked every tick"
    delete-test FAILS. TB-383's briefing predated the reframe so it shipped
    without the POST_DISPATCH removal the operator's 23:31 note (d) assigned to it.
  - Status: `in-progress`
  - Reasoning: decouple done, dead-phase residual open (folded into TB-388).

- **(4) Ideation component**
  - Progress so far: `Phase.IDEATION` reserved + walked (empty) since TB-381.
  - Gaps: `ap2/ideation.py::_maybe_ideate` + `ideation_halt.py` still core; not
    behind the IDEATION tick hook.
  - Status: `in-progress`
  - Reasoning: now unblocked (axis-3 decouple landed) but goal L209-213 sequences
    it LAST (largest blast radius); deferred this cycle.

- **(5) Judges are adapters not components; one generic registry verb**
  - Progress so far: `select_adapter(kind, cfg)` already exists (adapters/select.py:88)
    and both judges already use it for backend resolution — so the adapter seam
    is present; only the redundant *component* wrappers remain.
  - Gaps: `verifier_judge` IS a component (ap2/components/verifier_judge/ +
    registry.verifier_judge() registry.py:453) — must revert (goal L188-190);
    `validator_judge` still a component (ap2/components/validator_judge/ +
    registry.briefing_validators() registry.py:413); registry still carries
    bespoke channel_adapters/briefing_validators/verifier_judge/cron_job_handlers;
    daemon.py:2185-2345 carries core→component hook_points symbol-pull alias
    blocks (auto_approve, attention).
  - Status: `in-progress`
  - Reasoning: largest cleanup, freshest (TB-382 just went wrong); goal says go
    early "removes the most clutter" — top of this cycle's ranking (TB-386/387/388).

## Non-goal risk check

none. All proposals are structural moves preserving env-knob names + event
payloads (L297-301 "Removing behavior during component extraction" non-goal
respected — net behavior unchanged; `AP2_VERIFY_JUDGE_DISABLED` /
`AP2_VALIDATOR_JUDGE_DISABLED` survive as plain knobs). No goal.md mutation, no
cron-schedule change, no push.

## Considered & deferred this cycle

- **Axis 4 — ideation component**: deferred. Goal L209-213 sequences it last; its
  briefing would reference the `contributions(point)` accessor that TB-387
  introduces, so it is premature until 5a/5b land. Re-propose next cycle once
  TB-386/387 are in.
- **Recurring operator-rejection pattern**: TB-384 (a separate cron task) was
  rejected 2026-06-08 once TB-381 became the cron canary (redundant); earlier
  vetoes (TB-231 retry-on-malformed-JSON, TB-240 speculative path validator)
  punished symptom-patches + speculative enumerated-case validators.
- **Folding POST_DISPATCH removal into TB-388**: the axis-3 residual is small;
  bundled with the symbol-pull-block removal (both are core→component / dead-walk
  cleanup of the same flavor) rather than spending a slot on it standalone.

## Cycle observations

- TB-382 passed verification THEN the reframe invalidated its direction — a
  structurally-valid completion that is goal-wrong; the revert is folded into
  TB-386 (5a) rather than surfaced as a separate abandon, since the operator
  already logged the revert decision (23:31Z).

## Decisions needed from operator

None this cycle — the reframe is fresh and unambiguous, the TB-382 revert is
already operator-decided, and all four proposals map to reframed axes with
explicit delete-tests.

## Proposals this cycle

- TB-386 — axis 5a: demote both LLM judges from components to `select_adapter`
  layers (revert TB-382 verifier_judge component; dissolve validator_judge
  component; delete registry.briefing_validators() + verifier_judge()).
- TB-387 — axis 5b(i): one generic `contributions(point)` registry accessor;
  delete the bespoke per-kind methods. `@blocked:review,TB-386`.
- TB-388 — axis 5b(ii) + axis-3 residual: delete the core→component hook_points
  symbol-pull alias blocks (auto_approve, attention) + remove the dead
  POST_DISPATCH phase. `@blocked:review`.
- TB-389 — axis 2: communication component (inbound + outbound) wrapping the
  channel adapters; mattermost demotes to a channel adapter; delete
  channel_adapters() + inbound_poll from core. `@blocked:review`.
# Ideation State

_Last updated: 2026-06-09T13:47Z by ideation cron_

## Mission alignment

5 most recent Completes — TB-389 (channel surface → always-on
`communication` component owning inbound (`poll_inbound`) + outbound
(`Phase.COMMUNICATION` `run_outbound_tick`); `channel_adapters()` / `_deliver`
/ `inbound_poll` removed from core, mattermost demoted to a channel adapter,
outbound event-driven via `ap2.notify`, c50c843); TB-388 (deleted the
core→component `hook_points[...]` symbol-pull blocks for auto_approve +
attention, removed the dead `POST_DISPATCH` phase, ddb375a); TB-387 (one
generic fan-out `contributions(point)` registry accessor; bespoke
`cron_job_handlers()` deleted, 085f83e); TB-386 (both LLM judges demoted from
components back to `select_adapter` / direct-call layers;
`registry.verifier_judge()` / `briefing_validators()` + component dirs
deleted, 6cab646); TB-383 (auto-approve decoupled from `board_edit` into a
`PRE_DISPATCH` loop pass, 563e9d0). Every one a structural move under the L100
component-boundary focus; behavior + env-knob names preserved per the
L297-301 non-goal.

## Current focus assessment

- **get the component boundary right — loop-level participants only** (goal.md L100)
  - Progress so far: 4 of 5 axes LANDED. Axis 1 cron component (TB-381 +
    `cron_job_handlers` fold TB-387); axis 2 communication component (TB-389);
    axis 3 auto-approve→`PRE_DISPATCH` pass + `POST_DISPATCH` removal (TB-383 +
    TB-388); axis 5 judges-as-adapters + generic `contributions(point)` verb +
    `hook_points` symbol-pull deletion (TB-386 + TB-387 + TB-388).
  - Gaps: axis 4 (ideation component) — `daemon.py`
    still imports `ideation` / `ideation_halt` and calls `_maybe_ideate(...)` /
    `maybe_halt_on_exhaustion(...)` inline; no `ap2/components/ideation/`
    exists. Already queued as TB-391 (`@blocked:review,TB-383,TB-387`); both
    code predecessors TB-383/TB-387 now Complete, so only the `review` token
    remains. The minimal-kernel dispatch→verify→report Progress signal
    (goal.md L235-236) is queued as TB-392
    (`@blocked:review,TB-391`).
  - Status: `in-progress`

## Non-goal risk check

TB-391 is a module-move behind the registry preserving `AP2_IDEATION_*` names
+ `ideation_*` event payloads (L297-301 respected). TB-392 is a test-only
regression-pin. No goal.md mutation, no cron-schedule change, no push. none.

## Considered & deferred this cycle

- **3rd proposal to fill the open slot (N=3, backlog=2)**: NOT proposed. The
  slot count is a ceiling, not a quota; no greenfield work exists inside this
  focus that TB-391/TB-392 don't already cover.
- **Communication hardening (notify-queue GC, delivery retry)**: deferred —
  a fresh follow-up off TB-389, but it's a behavior-add that edges the
  L297-301 non-goal ("Removing behavior during component extraction" / no
  feature-add during the refactor); revisit only on a delivery-loss signal.
- **Registry-contract regression-pin (grep-assert no per-kind registration
  method or `hook_points` symbol-pull block returns)**: NOT proposed — the
  import-direction CI gate + TB-392's all-components-disabled e2e already
  cover the structural guarantees end-to-end; a standalone grep-pin is the
  enumerated-case wack-a-mole shape vetoed in TB-172 / TB-240.
- **Recurring operator-rejection pattern**: vetoes punish out-of-sequence /
  duplicate axis work (TB-384) and speculative enumerated-case validators
  (TB-172, TB-240, TB-231). 0 proposals this cycle matches neither risk shape.

## Cycle observations

- Goal-extension to the downstream OSS-distribution focus is the natural
  post-axis-4 decision, but still premature: axis 4 is unlanded and the
  operator owns focus sequencing (L264-306 non-goals). Carried — this cycle it
  remains the reason for NOT writing an OSS Decisions-needed bullet; surfacing
  it now would pre-empt the operator's own sequencing.

## Decisions needed from operator

none this cycle.

## Proposals this cycle

None. Backlog already carries the only two workable items in this focus
(TB-391 axis 4, TB-392 minimal-kernel e2e), both awaiting operator review.
The N=3 slot ceiling is not a quota.
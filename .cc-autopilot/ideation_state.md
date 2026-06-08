# Ideation State

_Last updated: 2026-06-08T20:53Z by ideation cron_

## Mission alignment

The 5 most recent Completes — TB-379 (`ap2 status` reads the daemon's live
effective-config snapshot), TB-378 (control-agent parity real-SDK smokes both
backends), TB-377 (real-work task-parity smoke), TB-376 (judge-parity smokes),
TB-372 (codex repoint to the real `openai-codex` SDK) — are tracked in the
**codex-support** focus. Mission L11-20 frames ap2 as a small **core** plus opt-in
**components** behind a registry, the structural prerequisite for an OSS cut. The
operator armed a new `## Current focus` today (4 `update_goal` ops
2026-06-08T17:36–18:47Z): extract the last tick-resident subsystems (pipeline,
cron, ideation) into components, untangle auto-approve from `board_edit`, and
split the prose-judge out. No mission drift — this is the direct continuation of
the 2026-05-27 component refactor toward a minimal kernel.

## Current focus assessment

The focus is `Current focus: extract the remaining core subsystems into
components`, decomposed into 5 axes. No Complete TB-N exists against this focus
yet (armed today), so every axis is `in-progress`; "Progress so far" cites the
shipped registry/component seam each axis builds on.

- **(1) Extended phase/hook vocabulary + canary (pipeline)**
  - Progress so far: the registry + typed `Phase` enum + `tick_hooks(phase)`
    walk shipped (TB-309, TB-310); the `impl.py`/`manifest.py` subpackage shape
    is ratified across 6 migrations (TB-313/TB-314/TB-315/TB-318, TB-343).
    `ap2/pipeline_sweep.py::_sweep_pipeline_pending` (L39) is still a flat core
    module called inline by `daemon._tick`.
  - Gaps: `Phase` lacks the pipeline-sweep / cron-dispatch / ideation stages the
    remaining extractions need; the pipeline subsystem (`_sweep_pipeline_pending`
    + `pipeline_task_start` tool + `pipeline_*` events) is not yet a component.
  - Status: `in-progress`
  - Reasoning: prerequisite for the tick-stage shape; nothing converted yet.

- **(2) Cron component (scheduler + job-handler registry)**
  - Progress so far: janitor already dispatches via `registry.hook("tick_hook",
    component="janitor")` inside `run_cron` (TB-309); the rest of `run_cron`
    (daemon.py L1376) is a hardcoded `if job.name ==` switch (status-report
    L1387, janitor L1405, real-sdk-smoke L1440).
  - Gaps: the cron scheduler + `cron_*` events + `cron_propose`/`cron_edit`
    surface are still in core; the `if job.name` switch is not a registered
    job-handler protocol.
  - Status: `in-progress`
  - Reasoning: extraction against axis 1's new tick shape; depends on it.

- **(3) Decouple auto-approve from `board_edit`**
  - Progress so far: the `auto_approve` component subpackage exists (TB-318) but
    its `_tick_hook` is a no-op POST_DISPATCH placeholder (manifest.py L105); the
    gate is still evaluated inline in `board_edits.py`'s `add_backlog` branch via
    `evaluate_auto_approve_decision` (L201), which strips `@blocked:review`
    mid-agent-run.
  - Gaps: `board_edit` is not policy-free; the gate chain + `should_auto_approve`
    tags policy (ideation.py L798) haven't moved into the component as a loop pass.
  - Status: `in-progress`
  - Reasoning: cross-boundary knot that blocks axis 4.

- **(4) Ideation component**
  - Progress so far: `ap2/ideation.py::_maybe_ideate` (L1033) + `ap2/ideation_halt.py`
    are still core; no extraction.
  - Gaps: ideation + halt not behind a tick hook; `AP2_IDEATION_*` cluster +
    `ideation_*` events still core-owned.
  - Status: `in-progress`
  - Reasoning: largest blast radius; goal.md L189-192 sequences it last (after
    axis 1 proves the shape + axis 3 unties auto-approve).

- **(5) Extract the prose-judge into a `verifier_judge` component**
  - Progress so far: the `validator_judge` component (TB-316) is the mirror
    template; `verify._judge_prose_bullet` (verify.py L470) is still welded into
    the core verify runner.
  - Gaps: the optional LLM prose-judge can't be disabled independently of the
    gating shell-bullet path.
  - Status: `in-progress`
  - Reasoning: independent of the tick-phase work; ready to ship in parallel.

## Non-goal risk check

none. All four proposals are pure structural moves with env-knob names + event
payloads preserved bit-for-bit — squarely inside L273-277 ("Removing behavior
during component extraction" is the non-goal; these add no behavior and delete
none). No goal.md mutation, no cron-schedule change, no push (L300-316
operator-only surfaces untouched).

## Considered & deferred this cycle

- **Axis 4 — ideation component**: deferred. goal.md L189-192 sequences it last
  (largest blast radius), after axis 1 proves the tick-stage shape and axis 3
  unties the auto-approve coupling. Re-propose once TB-381 + TB-383 land so the
  briefing can ground in the real seam they produce.
- **Recurring operator-rejection pattern**: the last two vetoes (TB-231
  retry-on-malformed-JSON, TB-240 speculative file-path validator) both punish
  symptom-patches and speculative enumerated-case validators guarding unobserved
  failures. This cycle's 4 proposals are verbatim goal.md axes, each with an
  explicit delete-test — the opposite shape; low rejection risk.
- **TB-380 (env-staleness WARN bug)**: already in Backlog `@blocked:review` from
  the prior cycle; not re-proposed.

## Cycle observations

- TB-379 + TB-380 are observability/bug fixes (`ap2 status`, env-staleness WARN)
  landed with goal-alignment skipped — orthogonal to the extraction focus but
  operator-/prior-cycle-driven; flagging only so next cycle doesn't mistake them
  for focus progress.
- Insights index carries 2 files (validator-judge-timeout 2026-05-18,
  test-suite-slowness 2026-05-17), both ~21–22 days old (under the 30-day stale
  line) and neither bears on the extraction focus.

## Decisions needed from operator

None this cycle — the focus is freshly specified with per-axis delete-tests, and
the four proposals map onto unblocked axes (1, 5, 3) plus axis 2 chained behind
axis 1. No design fork requires operator narrative judgment to unblock the next
cycle.

## Proposals this cycle

- TB-381 — axis 1: pipeline component + extended `Phase` vocabulary (canary).
- TB-382 — axis 5: extract the prose-judge into a `verifier_judge` component.
- TB-383 — axis 3: decouple auto-approve from `board_edit` into a loop pass.
- TB-384 — axis 2: cron component + job-handler registry (`@blocked:review,TB-381`).
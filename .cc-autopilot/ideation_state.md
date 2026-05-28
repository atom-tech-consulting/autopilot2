# Ideation State

_Last updated: 2026-05-28T17:38:00Z by ideation cron_

## Mission alignment

Cycle entry: board 0A / 0R / 0B / 0P / 191C / 0F. No new operator
activity since prior cycle (operator_log last entry 2026-05-28T05:42:24Z;
the two `decisions needed` items from prior cycle remain unanswered per
live `ap2 status`). The recent task arc that finished today (3 most
recent Completes): TB-317 (axis-6 disabled-config suite, fix commit
244424b), TB-318 (axis-5 `auto_approve/` migration — final named
axis-5 task, commit 548e667), TB-319 (`ap2 status` enumerates components
from registry, commit ce55765). All directly serve goal.md's component-
refactor focus; observable behavior preserved end-to-end.

## Current focus assessment

- **Current focus: refactor features into opt-in components**
  - Progress so far:
    - Axis (1) registry + manifest LANDED (TB-309, cee1c73).
    - Axis (2) tick-hook protocol LANDED (TB-310, 5a755c9).
    - Axis (3) channel-adapter ABC + sibling defaults LANDED
      (TB-312, 860b68a).
    - Axis (4) validator pipeline-as-list LANDED (TB-316, 1af2400).
    - Axis (5) seven migrations LANDED: `janitor/` (TB-309),
      `mattermost/` (TB-312), `focus_advance/` (TB-313),
      `auto_unfreeze/` (TB-314), `attention/` (TB-315),
      `validator_judge/` (TB-316), `auto_approve/` (TB-318 — final).
    - Axis (6) import-direction CI gate LANDED (TB-311, bafc891);
      disabled-config test suite LANDED (TB-317, 244424b).
    - Progress signal L235-237 (`ap2 status` enumerates active
      components from the registry) LANDED (TB-319, ce55765); live
      `ap2 status` `## Components` block confirmed against current
      daemon.
  - Gaps:
    - **Operator-blocked**: Progress signal L238-239 ("full test
      suite passes in all-components-disabled configuration") is
      closed by TB-317 for the 3 env_flag-bearing manifests (janitor,
      mattermost, validator_judge); the stronger reading ("every
      component disabled") still requires the 4 `env_flag=None`
      manifests (attention, auto_approve, auto_unfreeze, focus_advance)
      to gain master kill-switch flags. Each subpackage carries an
      internal switch today (focus_advance: `AP2_FOCUS_AUTO_ADVANCE_
      DISABLED`; auto_unfreeze: `AP2_AUTO_UNFREEZE_FIX_SHAPES`
      unset early-return; auto_approve: `AP2_AUTO_APPROVE`;
      attention: no master kill, only tunables), so the design polarity
      is "internal sub-knobs at finer granularity" not "manifest-level
      master switch". The TB-317 test docstring (L29-30) explicitly
      reads this as "only knob-bearing components are toggled" per
      goal.md L267-271 conservative defaults. Whether to extend to
      master-switch parity is operator territory; surfaced last cycle,
      still pending.
    - **Roadmap-end**: The only structural follow-up
      surface that would pay focus rent today is extracting
      `auto_approve`'s per-task gate logic from `daemon._tick`
      (current manifest docstring at L11-16 explicitly notes
      "observable-behavior risk" and flags it as "separate follow-up
      refactor") — that decision is the operator's to make.
  - Reasoning: The one remaining Progress-signal
    gap is the env_flag-polarity decision already surfaced (and
    explicitly designed-out in goal.md L267-271); the one credible
    follow-up refactor is operator-gated by an observable-behavior-risk
    flag in the auto_approve manifest itself.

## Non-goal risk check

None. Holding 0-proposal posture explicitly respects goal.md L102-105
(OSS distribution is a SEPARATE downstream focus) and the operator-
rejection pattern that punishes meta-polish unconnected to named
axes (TB-185, TB-184, TB-175).

## Considered & deferred this cycle

- **Extract `auto_approve` per-task gate logic from `daemon._tick`
  into the component's `_tick_hook`** — would pay focus rent
  (delete-test L223-227: "move a previously-hardcoded behavior into
  a component without changing its observable behavior") but the
  auto_approve manifest explicitly flags it as carrying "observable-
  behavior risk (per-task event payloads)" and labels it a "separate
  follow-up refactor". Promoting without operator sign-off would
  bypass that authored guardrail. Carried to `## Decisions needed
  from operator` with explicit unblock-condition.
- **Re-propose env_flag additions to attention / auto_approve /
  auto_unfreeze / focus_advance manifests** — would close the
  literal reading of goal.md L60-62 ("every component independently
  disable-able") but conflicts with the L267-271 conservative-
  defaults framing (the 4 manifests intentionally have no master
  switch because each has finer-grained internal knobs). Prior-cycle
  surfacing still pending; unilateral re-proposal of new env knobs
  would violate the L64-67 "existing env-knob names preserved"
  framing. Carried to `## Decisions needed from operator` with re-
  articulation.
- **Web `/components` page mirroring `ap2 status`'s component block**
  — pure meta-polish unconnected to a named axis; below the operator-
  rejection threshold (TB-185 shape). Deferred indefinitely.
- **Documentation pass on the component model** — defer to the
  future OSS-distribution focus where it pays direct rent; landing
  now risks drifting before consumed (L102-105 + TB-175 pattern).
- **Rejection-pattern check (carried, re-justified)**: operator
  vetoes TB-185/184 (ap2-meta-polish unconnected to focus), TB-175
  (premature aggregation), TB-231 (symptom-patching), TB-240
  (validator whack-a-mole). All four deferred candidates above
  would risk one of these failure modes. Zero-proposal posture
  remains the goal-aligned move.

## Cycle observations

(none — cross-cycle pattern memory would
quickly become stale once the operator extends the roadmap; the
manifest-internal-switch design polarity is already pinned in
manifest docstrings on disk, so a future focus's ideation will
re-derive from there rather than carrying it across cycles)

## Decisions needed from operator

- Decision needed: should the 4 `env_flag=None` component manifests
  (attention, auto_approve, auto_unfreeze, focus_advance) gain
  explicit master kill-switch env flags to fully close goal.md
  L60-62's literal reading ("every component independently disable-
  able via env flag") and unblock the stronger reading of Progress
  signal L238-239 ("every component disabled")? The current design
  intentionally bypasses this — TB-317's test docstring (L29-30)
  pins the "only knob-bearing components are toggled" reading per
  L267-271 conservative defaults; each subpackage's existing
  internal switch (`AP2_AUTO_APPROVE`, `AP2_FOCUS_AUTO_ADVANCE_
  DISABLED`, `AP2_AUTO_UNFREEZE_FIX_SHAPES` early-return) is
  canonical. Unblock condition: a one-liner on which polarity to
  take (add master switches → ideation proposes 4 narrow TB-Ns
  next cycle; or reframe the Done-when bullet → ideation drops
  this gap from next-cycle tracking).

- Decision needed: with all six named axes + the L235-237 Progress
  signal landed, should the operator extend goal.md with the next
  focus (OSS distribution per L102-105, or something else), OR run
  `ap2 approve` on an explicit follow-up to extract `auto_approve`'s
  per-task gate logic out of `daemon._tick` into the component's
  `_tick_hook`? The auto_approve manifest docstring (L11-16) flags
  the extraction as "observable-behavior risk (per-task event
  payloads)" and a "separate follow-up refactor"; ideation reads
  that as operator-gated rather than auto-proposable. Unblock
  condition: extending the roadmap restores ideation's grounding
  surface; without it, this is the third consecutive empty cycle
  and the empty-cycles counter is accumulating toward auto-advance
  / `roadmap_complete` (the daemon will emit on its own once the
  threshold is hit, since this is the sole active focus).

## Proposals this cycle

0 proposals. Slot budget is 5; deliberately proposing 0 rather than
inventing meta-polish or pre-empting the two operator decisions
above.
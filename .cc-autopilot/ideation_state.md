# Ideation State

_Last updated: 2026-05-28T15:36:00Z by ideation cron_

## Mission alignment

Cycle entry: board 0A / 0R / 0B / 0P / 191C / 0F — no new operator
activity since the prior cycle (operator_log L274 is still the most
recent entry, dated 2026-05-28T05:42:24Z); the two `decisions
needed` items surfaced last cycle remain unanswered (verified
against live `ap2 status` output). 3 most recent Completes:
TB-317 (axis-6 disabled-config test suite, fix commit 244424b),
TB-318 (axis-5 `auto_approve/` migration — final named axis-5
task, commit 548e667), TB-319 (`ap2 status` enumerates components
from registry, commit ce55765). All directly serve goal.md's
component-refactor focus; observable behavior preserved.

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
      `validator_judge/` (TB-316), `auto_approve/` (TB-318,
      548e667 — final named axis-5 migration).
    - Axis (6) import-direction CI gate LANDED (TB-311, bafc891);
      disabled-config test suite LANDED (TB-317, 244424b).
    - Progress signal L235-237 (`ap2 status` enumerates active
      components from the registry) LANDED (TB-319, ce55765).
  - Gaps:
    - **Operator-blocked**: Progress signal L238-239 ("full test
      suite passes in all-components-disabled configuration")
      cannot be fully closed while 4 of 7 manifests still carry
      `env_flag=None` (attention, auto_approve, auto_unfreeze,
      focus_advance — confirmed from live `ap2 status` `##
      Components` block). TB-317 added smoke tests for the 3
      components that DO have env flags (janitor, mattermost,
      validator_judge); the stronger "every component disabled"
      claim requires the 4 holdouts to gain disable-able flags
      first. Surfaced last cycle; operator has not responded.
    - **Roadmap-end**: All six named axes + the L235-237 Progress
      signal are closed. With goal.md L102-105 framing OSS
      distribution as a SEPARATE downstream focus, this focus has
      no remaining greenfield surface that doesn't pre-empt the
      operator's design polarity on the env_flag question.

## Non-goal risk check

None. Holding 0-proposal posture explicitly respects goal.md L102-105
(OSS distribution is a SEPARATE downstream focus) and the operator-
rejection pattern that punishes meta-polish unconnected to named
axes (TB-185, TB-184, TB-175).

## Considered & deferred this cycle

- **Re-propose env_flag additions to attention / auto_approve /
  auto_unfreeze / focus_advance manifests** — would close goal.md
  L60-62 ("every component independently disable-able"). Deferred
  AGAIN: prior-cycle surfacing still pending operator input;
  unilateral re-proposal of NEW env knobs would violate the L64-67
  "existing env-knob names preserved" framing. Carried to
  `## Decisions needed from operator` with re-articulation.
- **Web `/components` page mirroring `ap2 status`'s component
  block** — parallel to TB-296's `/attention`. Deferred: pure
  meta-polish unconnected to a named axis, ranks below the
  operator-rejection threshold (TB-185 shape).
- **Documentation pass on the component model** (how to add a new
  component, manifest schema reference, hook protocol) — defer to
  the future OSS-distribution focus where it pays direct rent;
  landing now risks drifting before consumed (L102-105 framing
  + TB-175 "premature aggregation" rejection pattern).
- **Rejection-pattern check (carried, re-justified)**: operator
  vetoes TB-185/184 (ap2-meta-polish unconnected to focus), TB-175
  (premature aggregation), TB-231 (symptom-patching), TB-240
  (validator whack-a-mole). All three deferred candidates above
  would risk one of these failure modes. Zero-proposal posture
  remains the goal-aligned move.

## Cycle observations

(none — prior cycle's "axis-5 migration pattern groove codified"
observation is no longer informing current reasoning; the focus
itself is exhausted, so a future focus's ideation will re-derive
the pattern from the test_tb31N_*_migration pin set on disk
rather than carrying it across cycles)

## Decisions needed from operator

- Decision needed: should the 4 `env_flag=None` component manifests
  (attention, auto_approve, auto_unfreeze, focus_advance) gain
  explicit master kill-switch env flags to fully close goal.md
  L60-62 ("every component independently disable-able via env flag")
  and unblock the Progress signal L238-239 ("full test suite passes
  in all-components-disabled configuration")? The current design
  intentionally bypasses this — internal sub-knobs
  (`AP2_ATTENTION_IMMEDIATE_PUSH`, `AP2_FOCUS_AUTO_ADVANCE_DISABLED`,
  `AP2_AUTO_APPROVE`, `AP2_AUTO_UNFREEZE_DISABLED`) gate sub-
  behaviors at finer granularity, and adding manifest-level master
  switches would introduce NEW env knobs against the L64-67
  "existing env-knob names preserved" framing. Unblock condition:
  a one-liner from operator on which polarity to take (add master
  switches → ideation proposes 4 narrow TB-Ns next cycle; or
  reframe the Done-when bullet → ideation drops this gap from
  next-cycle tracking).

- Decision needed: with all six named axes + the L235-237 Progress
  signal landed and the env_flag=None question above as the only
  remaining gap, what is the next focus? Operator action: either
  `ap2 update-goal` to add the next focus (OSS distribution per
  L102-105, or something else), OR allow the empty-cycles counter
  to grouping-count consecutive 0-prop cycles toward focus-advance
  (the daemon will emit `roadmap_complete` on its own once the
  threshold is hit, since this is the sole active focus). Unblock
  condition: an extended roadmap restores ideation's grounding
  surface; without it, this is the second consecutive empty cycle
  and the counter is accumulating toward auto-advance.

## Proposals this cycle

0 proposals. Slot budget is 5; deliberately proposing 0 rather than
inventing meta-polish or pre-empting the two operator decisions
above.
# Ideation State

_Last updated: 2026-05-28T13:32:00Z by ideation cron_

## Mission alignment

Cycle entry: board 0A / 0R / 0B / 0P / 191C / 0F — prior cycle's two
proposals (TB-318 auto_approve migration, TB-319 ap2 status component
enumeration) both landed within ~75 min. The 3 most recent Completes:
TB-317 (axis-6 disabled-config test, fix commit 244424b), TB-318
(axis-5 `auto_approve/` migration — final named axis-5 task, commit
548e667), TB-319 (`ap2 status` enumerates components from registry,
commit ce55765) — each preserved observable behavior and shipped
regression pins.

## Current focus assessment

- **Current focus: refactor features into opt-in components**
  - Progress so far:
    - Axis (1) registry + manifest LANDED (TB-309, cee1c73).
    - Axis (2) tick-hook protocol LANDED (TB-310, 5a755c9).
    - Axis (3) channel-adapter ABC LANDED (TB-312, 860b68a).
    - Axis (4) validator pipeline-as-list LANDED (TB-316, 1af2400).
    - Axis (5) seven migrations LANDED: `janitor/` (TB-309),
      `mattermost/` (TB-312), `focus_advance/` (TB-313), `auto_unfreeze/`
      (TB-314), `attention/` (TB-315), `validator_judge/` (TB-316),
      `auto_approve/` (TB-318, 548e667).
    - Axis (6) import-direction gate LANDED (TB-311, bafc891);
      disabled-config test suite LANDED (TB-317, 244424b).
    - Progress signal L235-237 (`ap2 status` component enumeration)
      LANDED (TB-319, ce55765) — text + JSON list every manifest's
      name + on/off state + env-flag description, sourcing polarity
      from `Manifest.is_enabled(env)`.
  - Gaps:
    - **Operator-blocked**: Done-when bullet L60-62 ("every component
      can be independently disabled via its env flag; the full test
      suite passes in the default configuration AND in an 'every
      component disabled' configuration") NOT fully closed — 4 of 7
      manifests carry `env_flag=None` (attention, auto_approve,
      auto_unfreeze, focus_advance, per the live `ap2 status`
      `## Components` block). Without per-component master kill
      switches, the disabled-config Progress signal L238-239 cannot
      be fully closed for those 4 either (TB-317 added a smoke-test
      suite for components that DO have env flags; the goal's
      stronger "full test suite passes in all-components-disabled"
      requires the 4 holdouts to gain disable-able flags first).
      Surfaced to operator last cycle; decision still pending.
    - **Roadmap-end**: Goal.md L102-105 explicitly frames OSS distribution as a SEPARATE downstream focus — out of scope for this focus.
  - Reasoning: every named axis + Progress signal closeable without
    operator input is closed. The one remaining gap (env_flag=None
    for 4 manifests, which gates the disabled-config Progress
    signal) was surfaced last cycle as a design-call requiring
    operator polarity; the operator has not responded, and ideation
    should not unilaterally invent NEW env knobs (would violate the
    L64-67 "existing env-knob names preserved" framing).

## Non-goal risk check

None. The 0-proposal posture this cycle deliberately respects
goal.md L102-105 (OSS distribution is a SEPARATE downstream focus —
not for ideation to leak into) and the operator-rejection pattern
that punishes meta-polish unconnected to named axes.

## Considered & deferred this cycle

- **Re-propose env_flag additions to attention / auto_approve /
  auto_unfreeze / focus_advance manifests** — would close goal.md
  L60-62 ("every component independently disable-able"). Deferred
  because the prior cycle's surfacing is still pending operator
  input AND the current design intentionally chose `env_flag=None`
  for these 4 (their internal sub-knobs gate sub-behaviors at finer
  granularity; adding manifest-level master switches would introduce
  NEW env knobs against the L64-67 "existing env-knob names
  preserved" framing). Unilateral re-proposal would tip into
  meta-polish + design-call-without-operator territory; carry to
  `## Decisions needed from operator` instead.
- **Web `/components` page mirroring `ap2 status`'s component block**
  — parallel to TB-296's `/attention` page. Defer to next cycle if
  operator pulls; ranking against the operator-rejection pattern
  (meta-polish unconnected to named axes), no clear focus rent.
- **Documentation pass on the component model (how to add a new
  component, manifest schema reference, hook protocol)** — pure
  docs work, valuable for the eventual OSS focus but premature now;
  the L102-105 framing puts distribution prep in a downstream
  focus, and docs landed prematurely will drift before they're
  consumed.
- **Rejection-pattern check (carried, re-justified)**: operator
  vetoes TB-185/184 (ap2-meta-polish unconnected to focus), TB-231
  (symptom-patching), TB-175 (premature aggregation), TB-240
  (validator whack-a-mole). All three deferred candidates above
  would risk one of these failure modes (env_flag additions =
  design-call without operator; web page = meta-polish; docs =
  premature aggregation for a focus that doesn't exist yet).
  Zero-proposal posture is the goal-aligned move.

## Cycle observations

- The axis-5 migration pattern groove from TB-313/314/315/316/318
  is now codified and reusable. If a future focus needs to add a
  NEW component (e.g. a new channel adapter or detector), the
  TB-315 shape is the canonical reference — git-mv flat module to
  `__init__.py` of subpackage, manifest sources intra-package,
  daemon module-level aliases rebind through `default_registry()`,
  test fixups. Not actionable this cycle; noted for next focus's
  ideation grounding.

## Decisions needed from operator

- Decision needed: should the 4 `env_flag=None` component manifests
  (attention, auto_approve, auto_unfreeze, focus_advance) gain
  explicit master kill-switch env flags to fully close goal.md
  L60-62 ("every component independently disable-able via env flag")
  and unblock the Progress signal L238-239 ("full test suite passes
  in all-components-disabled configuration")? The current design
  intentionally bypasses this — internal sub-knobs
  (`AP2_ATTENTION_IMMEDIATE_PUSH`, `AP2_FOCUS_AUTO_ADVANCE_DISABLED`,
  `AP2_AUTO_APPROVE`, `AP2_AUTO_UNFREEZE_DISABLED`) gate sub-behaviors
  at finer granularity, and adding manifest-level master switches
  would introduce NEW env knobs against the L64-67 "existing
  env-knob names preserved" framing. Unblock condition: a one-liner
  from operator on which polarity to take (add master switches, or
  reframe the Done-when bullet to acknowledge sub-knob coverage is
  sufficient). If "yes, add switches", ideation will propose the
  additions next cycle as 4 narrow TB-Ns; if "no, sub-knobs are
  enough", ideation drops this gap from next-cycle tracking.

- Decision needed: with all six named axes + the L235-237 Progress
  signal landed and the env_flag=None question above as the only
  remaining gap, what is the next focus? Operator action: either
  `ap2 update-goal` to add the next focus (OSS distribution per
  L102-105, or something else), OR signal "wind down for now" via
  the empty-cycles counter (currently grouping consecutive 0-prop
  cycles toward auto-advance). Unblock condition: an extended
  roadmap restores ideation's grounding surface; without it,
  ideation will exhaust empty-cycles against this focus and
  trigger focus-advance with nowhere to advance to.

## Proposals this cycle

0 proposals. Slot budget is 5; deliberately proposing 0 rather than
inventing meta-polish or pre-empting the operator's roadmap-end
decision.
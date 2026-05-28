# Ideation State

_Last updated: 2026-05-28T19:53:10Z by ideation cron_

## Mission alignment

Cycle entry: board 0A / 0R / 1B / 0P / 191C / 0F (TB-320 still
in Backlog post verification_failed event). The operator engaged
since the prior cycle (2026-05-28T17:38:00Z): TB-320 was
add_backlog-queued at 19:35:48Z and auto-promoted within 12s —
direct action on the prior cycle's first surfaced decision (the
env_flag-polarity question for the 4 env_flag=None manifests).
TB-320's run landed substantive work in commit e61ecc9 (wired
env_flag on auto_approve / auto_unfreeze / focus_advance manifests
+ new AP2_AUTO_UNFREEZE_DISABLED kill switch with sticky-first-skip
audit event); 11 of 12 verification bullets passed; the lone fail
was a `kind=malformed` shell bullet — TB-207-shape literal backtick
inside a single-backtick codespan, with the verifier emitting its
own diagnostic (`Rewrite the bullet to either (a) use a
double-backtick wrapping…`). At 19:54:53Z the operator queued an
`update` op on TB-320 — the standard remediation path for a
shell-bullet typo, no ideation surface needed. Three most recent
Completes: TB-318 (axis-5 final auto_approve migration, 548e667),
TB-319 (ap2 status enumerates components, ce55765), TB-320 in
flight (e61ecc9, verification_failed pending operator update).

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
      `validator_judge/` (TB-316), `auto_approve/` (TB-318).
    - Axis (6) import-direction CI gate LANDED (TB-311, bafc891);
      disabled-config test suite LANDED (TB-317, 244424b).
    - Progress signal L235-237 (`ap2 status` enumerates components)
      LANDED (TB-319, ce55765); confirmed against live daemon.
    - Env_flag-polarity gap (prior-cycle decision #1) now in flight
      via operator-queued TB-320 (e61ecc9) — 3 of 4 env_flag=None
      manifests gain explicit knobs, attention/ stays env_flag=None
      per the operator's authored exception, AP2_AUTO_UNFREEZE_DISABLED
      added as a new HOT_RELOADABLE kill switch with subpackage
      self-gate. 11 of 12 verification bullets pass; pending the
      operator's queued 19:54:53Z update op to fix the malformed bullet.
  - Gaps:
    - **In-flight remediation**: TB-320's one failing bullet
      (`kind=malformed`, TB-207-shape literal backtick in
      single-backtick codespan); operator has already queued an
      update op, so the standard fix-and-retry loop closes this
      without ideation surface.
    - **Operator-blocked** (carried — re-articulated): extracting
      `auto_approve`'s per-task gate logic from `daemon._tick`
      into the component's `_tick_hook` is the remaining structural
      follow-up (auto_approve manifest docstring L11-16 explicitly
      flags "observable-behavior risk (per-task event payloads)"
      and labels it a "separate follow-up refactor"); ideation
      reads that as operator-gated.
  - Status: in operator-update flow and pending operator decision on roadmap extension.

## Non-goal risk check

None. Continuing 0-proposal posture respects goal.md L102-105
(OSS distribution is a SEPARATE downstream focus) and the
operator-rejection pattern that punishes meta-polish unconnected
to named axes (TB-185, TB-184, TB-175).

## Considered & deferred this cycle

- **`#fix-briefing` task for TB-320's malformed bullet** — TB-88
  Step 1.5 classification fits (edit-briefing: single bullet,
  known TB-207 shape, substantive commit landed). DEFERRED because
  the operator already queued an `update TB-320` op at 19:54:53Z;
  proposing a parallel fix-briefing task would duplicate the
  in-flight remediation. Re-evaluate only if the operator update
  goes stale.
- **Extract `auto_approve` per-task gate logic from `daemon._tick`
  into the component's `_tick_hook`** (carried) — would pay focus
  rent (L223-227 delete-test: "move a previously-hardcoded behavior
  into a component without changing its observable behavior") but
  the auto_approve manifest docstring (L11-16) explicitly flags
  "observable-behavior risk (per-task event payloads)" and labels
  it a "separate follow-up refactor". Promoting without operator
  sign-off would bypass an authored guardrail. Carried to
  `## Decisions needed from operator` with explicit unblock.
- **Add env_flag to `attention/` manifest** — operator decision
  pinned in TB-320's Out-of-scope (L192-194): "attention stays
  always-on as baseline operator-legible signal". Dropping this
  candidate from future-cycle consideration unless operator re-opens.
- **Web `/components` page mirroring `ap2 status`'s component
  block** — pure meta-polish unconnected to a named axis; below
  the operator-rejection threshold (TB-185 shape). Deferred
  indefinitely.
- **Rejection-pattern check (carried, re-justified)**: operator
  vetoes TB-185/184 (ap2-meta-polish unconnected to focus), TB-175
  (premature aggregation), TB-231 (symptom-patching), TB-240
  (validator whack-a-mole). All four deferred candidates above
  would risk one of these failure modes. Zero-proposal posture
  remains the goal-aligned move.

## Cycle observations

(none — prior cycle's observation that the manifest-internal-switch
design polarity is "pinned in manifest docstrings on disk" is now
also pinned in TB-320's authored Out-of-scope clause, so the rule
is double-anchored and doesn't need ideation working memory)

## Decisions needed from operator

- Decision needed: with all six named axes + the L235-237 Progress
  signal landed AND TB-320 closing the env_flag-polarity gap
  (operator-update remediation in flight), should the operator
  extend goal.md with the next focus (OSS distribution per
  L102-105, or something else), OR run `ap2 approve` on an
  explicit follow-up to extract `auto_approve`'s per-task gate
  logic out of `daemon._tick` into the component's `_tick_hook`?
  The auto_approve manifest docstring (L11-16) flags the
  extraction as "observable-behavior risk (per-task event
  payloads)" and a "separate follow-up refactor"; ideation reads
  that as operator-gated rather than auto-proposable. Unblock
  condition: extending the roadmap restores ideation's grounding
  surface; without it, this becomes the fourth consecutive empty
  cycle and the empty-cycles counter is accumulating toward
  auto-advance / `roadmap_complete` (the daemon will emit on its
  own once the threshold is hit, since this is the sole active
  focus).

## Proposals this cycle

0 proposals. Slot budget is 4; deliberately proposing 0 rather
than inventing meta-polish or pre-empting the auto_approve
extraction decision. TB-320's malformed-bullet fix is already in
operator-update flow; no parallel fix-briefing proposal needed.
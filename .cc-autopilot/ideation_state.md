# Ideation State

_Last updated: 2026-05-28T11:23:00Z by ideation cron_

## Mission alignment

Cycle entry: board 0A / 0R / 0B / 0P / 189C / 0F — last cycle's three
proposals (TB-315 attention, TB-316 validator pipeline-as-list +
validator_judge, TB-317 disabled-config test suite) all landed within
~90 min and the board fully drained. The 3 most recent Completes —
TB-315 (axis-5 `attention/` migration, commit 744f3d7), TB-316
(axis-4 validator pipeline-as-list bundled with axis-5
`validator_judge/` migration, commit 1af2400), and TB-317 (axis-6
disabled-config test suite, fix commit 244424b) — each preserved
observable behavior and shipped per-task regression pins (11 / 17 / 9
tests respectively). Six of the goal.md axes are now fully landed:
(1) registry+manifest (TB-309), (2) tick-hook protocol (TB-310), (3)
channel-adapter ABC (TB-312), (4) validator pipeline-as-list
(TB-316), (5) six of seven migrations (janitor/validator_judge/
mattermost/attention/focus_advance/auto_unfreeze), and (6) both
import-direction gate (TB-311) + disabled-config test (TB-317). The
sole remaining named axis work is the axis-5 `auto_approve/`
migration — sequenced LAST per goal.md L196-197.

## Current focus assessment

- **Current focus: refactor features into opt-in components**
  - Progress so far:
    - Axis (1) registry + manifest + `janitor/` canary LANDED
      (TB-309, cee1c73).
    - Axis (2) tick-hook protocol LANDED (TB-310, 5a755c9).
    - Axis (3) channel-adapter ABC LANDED (TB-312, 860b68a).
    - Axis (4) validator pipeline-as-list LANDED (TB-316, 1af2400) —
      bundled with `validator_judge/` migration per goal.md L218.
    - Axis (5) six migrations LANDED: `janitor/` (TB-309, cee1c73),
      `mattermost/` (TB-312, 860b68a), `focus_advance/` (TB-313,
      6b4fcea), `auto_unfreeze/` (TB-314, 73f5a52), `attention/`
      (TB-315, 744f3d7), `validator_judge/` (TB-316, 1af2400).
    - Axis (6) import-direction gate LANDED (TB-311, bafc891);
      disabled-config test suite LANDED (TB-317, 244424b).
  - Gaps (in sequenced order per goal.md L216-221):
    - Axis (5) `auto_approve/` migration NOT STARTED — flat
      `ap2/auto_approve.py` (743 lines) still exists; daemon.py L27
      keeps `from . import auto_approve` and rebinds 16 module-level
      aliases at L1760-1776. Stub manifest at
      `ap2/components/auto_approve/manifest.py` has a no-op tick hook
      (its docstring flags that axis-5 extraction is pending). Per
      goal.md L196-197 sequenced LAST ("largest blast radius —
      touches ideation, proposal labeling, retry semantics, cost
      guards"). Now unblocked and is the natural cycle slot.
    - Progress signal L235-237 ("`ap2 status` could in principle
      enumerate active components from it") NOT YET CLOSED — the
      registry walk exists (`default_registry().tick_hooks(phase)`)
      but no operator-facing surface enumerates which components are
      registered + on/off per env-flag. Without this, the value of
      the refactor (component-model legibility) is internal only.
    - **Observation**: 4 of 7 manifests carry `env_flag=None`
      (attention, auto_approve, auto_unfreeze, focus_advance) — they
      cannot be disabled at the manifest level; internal sub-knobs
      (`AP2_ATTENTION_IMMEDIATE_PUSH`, `AP2_FOCUS_AUTO_ADVANCE_DISABLED`,
      etc.) gate sub-behaviors only. Goal.md L62-63 says "Every
      component can be independently disabled via its env flag"; the
      design intentionally bypassed this for the four (docstrings
      explain), but the Done-when bullet remains partly open. Surfaced
      to operator below (not auto-proposed — borderline scope creep).
  - Status: `in-progress`
  - Reasoning: 1 unambiguous axis-5 migration remains; closing the
    Progress-signal `ap2 status` enumeration would also pay focus
    rent.

## Non-goal risk check

None. The proposed `auto_approve/` migration is the goal.md-named
final axis-5 task. The `ap2 status` component enumeration is a
goal.md L235-237 Progress signal closure, not meta-polish. No
env-knob renames (L64-67 constraint), no goal.md mutation (L272-277),
no behavior-removal during extraction (L278-282).

## Considered & deferred this cycle

- **Add explicit `env_flag` master kill switches to the 4
  `env_flag=None` manifests (attention, auto_approve, auto_unfreeze,
  focus_advance)** — would fully close goal.md L62-63 Done-when bullet
  ("every component independently disable-able via env flag"). The
  current design intentionally chose `None` because each module has
  internal sub-knobs that gate behavior at a finer granularity, and
  adding manifest-level master switches would introduce NEW env knobs
  (not preserving existing ones, but adding parallel master toggles).
  Surfaced to operator in `## Decisions needed from operator` — this
  is a scope/design call I shouldn't unilaterally make.
- **Web `/components` page** — parallel to TB-296's `/attention` pull
  page, would enumerate registered manifests + on/off via the web
  UI. TB-319 below puts the enumeration into `ap2 status` (CLI) as
  the primary surface; the web page can follow if needed. Defer to
  next cycle if operator wants the web parity after the CLI lands.
- **Rejection-pattern check (carried, re-justified)**: operator
  vetoes TB-185/184 (ap2-meta-polish unconnected to focus), TB-231
  (symptom-patching without root-cause), TB-175 (premature
  aggregation), TB-240 (validator whack-a-mole). Both proposals
  below map directly to a named axis line or Progress signal in
  goal.md — auto_approve is L196-197 (axis 5 sequenced last); status
  enumeration is L235-237 (Progress signal). Neither is meta-polish.

## Cycle observations

- The axis-5 migration pattern is now well-grooved across 5
  back-to-back canaries (TB-313/314/315/316 + earlier TB-309).
  Briefing for TB-318 can be shorter — cite TB-315 (attention) as
  the shape reference and have the agent mirror it. Pattern: git-mv
  flat module → `__init__.py` of subpackage; manifest sources
  intra-package via `from . import …` and exposes hook_points for
  every previously-direct-imported symbol; daemon module-level
  aliases rebind through `default_registry().get(...).hook_points[...]`;
  fix the handful of test files that imported the flat path.
- The `auto_approve` manifest stub docstring (lines 1-26 of
  `components/auto_approve/manifest.py`) explicitly flags that the
  inline gate logic in `daemon._tick` belongs to axis-5 extraction.
  TB-318 should ONLY do the file move + manifest hook_points + alias
  rebind for THIS cycle — extracting the inline gate is a deeper
  refactor that risks observable behavior changes (per-task
  `auto_approve_paused` / `auto_approve_skipped` / `auto_approve_halted`
  events have task-specific payload). Conservative scope first;
  inline-gate extraction can be a follow-up TB-N if the operator
  decides it's worth the blast radius.

## Decisions needed from operator

- Decision needed: should the 4 `env_flag=None` component manifests
  (attention, auto_approve, auto_unfreeze, focus_advance) gain
  explicit master kill-switch env flags to fully close goal.md
  L62-63 ("every component independently disable-able via env flag")?
  The current design intentionally bypasses this — internal sub-knobs
  gate sub-behaviors at finer granularity. If the answer is "yes",
  ideation will propose the additions next cycle; if "no, the
  sub-knob coverage is sufficient", ideation drops this from
  next-cycle gap-tracking. Unblock condition: a one-liner from
  operator on which polarity to take.
- Decision needed: after TB-318 (`auto_approve/` migration) + TB-319
  (`ap2 status` component enumeration) land, what comes next — OSS
  distribution preparation (goal.md L102-105), or a different axis?
  If ideation has no next focus to ground proposals against, it will
  exhaust the empty-cycles counter and trigger focus-advance. Unblock
  condition: operator extends the roadmap via `ap2 update-goal` or
  signals "wind down ap2 work for now".

## Proposals this cycle

2 proposals: TB-318 (axis-5 `auto_approve/` subpackage migration —
final axis-5 task), TB-319 (`ap2 status` enumerates active components
from the registry — closes Progress signal L235-237). Slot budget is
5; deliberately proposing 2 because (a) no other clear axis-named
work remains unblocked, and (b) the operator's veto pattern punishes
meta-polish unconnected to named axes.
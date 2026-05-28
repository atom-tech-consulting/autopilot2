# Ideation State

_Last updated: 2026-05-28T06:51:00Z by ideation cron_

## Mission alignment

Cycle entry: board 0A / 0R / 0B / 0P / 183C / 0F — last cycle's three
proposals (TB-309, TB-310, TB-311) all landed within ~80 min and the
board fully drained. The 3 most recent Completes — TB-309 (axis-1
registry + `janitor/` canary, commit cee1c73), TB-311 (axis-6 partial,
AST-based import-direction CI gate, commit bafc891), TB-310 (axis-2
tick-hook protocol + stub manifests for auto_approve / auto_unfreeze
/ attention / focus_advance, commit 5a755c9 — operator hand-edited one
Verification bullet per 2026-05-28T05:42Z log to clear a BSD `wc -l`
padding bug, work was substantively correct) — closed the axis (1)
prerequisite, opened the axis (2) walk path, and pinned axis (6)'s
import-direction half. Every commit shipped the structural cleavage goal.md requests without altering any
observable behavior (zero env-knob renames, zero feature deletions).

## Current focus assessment

- **Current focus: refactor features into opt-in components**
  - Progress so far:
    - Axis (1) registry + manifest schema + `janitor/` canary
      LANDED (TB-309, cee1c73) — `ap2/registry.py` with `Manifest`
      dataclass + filesystem-discovery `Registry` + cached
      `default_registry()`; `ap2/janitor.py` git-moved into
      `ap2/components/janitor/` with full `manifest.py`. This is the
      only component that's been migrated to subpackage shape (axis 5).
    - Axis (2) tick-hook protocol LANDED (TB-310, 5a755c9) — `Phase`
      enum, `Manifest.tick_hooks`, `Registry.tick_hooks(phase)`;
      `daemon._tick` walks the registry at PRE_DISPATCH /
      ATTENTION_EMISSION / POST_DISPATCH. Stub manifests added for
      auto_approve / auto_unfreeze / attention / focus_advance —
      these point at the FLAT `ap2/<name>.py` modules (axis 5
      subpackage move still pending).
    - Axis (6) import-direction CI gate LANDED (TB-311, bafc891) —
      AST-based pytest gate covering 4 static import forms + exempt
      set for the registry.
  - Gaps (in sequenced order per goal.md L216-221):
    - Axis (3) channel-adapter abstraction NOT STARTED — `_mm_post`
      call sites in `daemon.py:1919`, `watchdog.py:90,130` and the
      status-report digest delivery still hardcode Mattermost. Goal.md
      L184-186 explicitly bundles the `mattermost/` axis-5 migration
      with axis 3 (channel/team/bot env knobs + `mattermost_reply`
      MCP tool move together).
    - Axis (5) `focus_advance/` subpackage move NOT STARTED — the
      stub manifest at `ap2/components/focus_advance/manifest.py`
      still imports `from ap2 import focus_advance as
      _focus_advance_mod` (flat module). Per goal.md L189-193 the
      module body itself needs to move into the subpackage.
    - Axis (5) `auto_unfreeze/` subpackage move NOT STARTED — same
      shape as focus_advance gap; stub manifest exists.
    - Axis (5) `attention/` subpackage move BLOCKED on axis 3 — per
      goal.md L188 attention publishes via the channel adapter, so
      sequencing requires axis 3 first.
    - Axis (5) `auto_approve/` subpackage move — explicitly sequenced
      LAST per goal.md L196-197 (largest blast radius — touches
      ideation, proposal labeling, retry semantics, cost guards).
    - Axis (4) validator pipeline + `validator_judge/` migration —
      conjoined per goal.md L218. Independent of axes 3/2 but distinct
      slice; lower marginal value this cycle vs. axis 3.
    - Axis (6) disabled-config test suite — second half of axis 6
      (the half TB-311 didn't ship). Lands incrementally; cheaper
      after a couple more migrations than as the immediate next step
      (today only `janitor/` is a true subpackage component, so a
      "every component disabled" suite has minimal surface to assert
      against — wait until ≥3 subpackages exist).
  - Status: `in-progress`
  - Reasoning: 3 of 6 axes have shipped foundational work (1, 2,
    half of 6); 3 axes plus 5 component migrations remain. Plenty
    of unblocked structural work.

## Non-goal risk check

None. All proposed work is the structural refactor the focus asks
for. No new env-knob renames (goal.md L64-67 constraint), no goal.md
auto-mutation (L272-277), no behavior-removal during extraction
(L278-282). Each migration preserves the existing kill switch
(`AP2_FOCUS_AUTO_ADVANCE_DISABLED`, `AP2_AUTO_UNFREEZE_DISABLED`,
etc.).

## Considered & deferred this cycle

- **Axis (5) `attention/` migration proposal this cycle** — Goal.md
  L188 makes this depend on axis 3's channel-adapter abstraction.
  Premature until TB-312 lands; re-propose next cycle once axis 3
  is in. (Surfaces the channel-adapter integration point as the
  immediate gate.)
- **Axis (4) validator-pipeline-as-list + `validator_judge/`
  migration proposal this cycle** — Independent of axis 3 and could
  ship in parallel; deferred because three structural proposals
  already populate this cycle's slate and the validator_judge
  migration is largest of the remaining (manifest schema for the
  pipeline-as-list plus an SDK-call-bearing component). Re-propose
  next cycle.
- **Axis (5) `auto_approve/` migration** — Goal.md L196-197 places
  this LAST in migration order ("largest blast radius — touches
  ideation, proposal labeling, retry semantics, cost guards").
  Premature; wait until ≥4 prior migrations land and the migration
  shape is well-grooved.
- **Axis (6) disabled-config test suite proposal this cycle** —
  Today only `janitor/` is a real subpackage component; a
  "every component disabled" assertion has minimal surface area to
  bite on. Higher leverage after 2-3 more migrations land. Carry
  to a future cycle.
- **Rejection-pattern check (carried, re-justified)**: operator
  vetoes TB-185/184 (ap2-meta-polish unconnected to focus), TB-231
  (symptom-patching without root-cause), TB-175 (premature
  aggregation), TB-240 (validator whack-a-mole). New-focus
  proposals must clear "structural cleavage, not polish" — every
  ranked proposal below maps directly to a named axis line in
  goal.md (L116-214), not a meta-polish gap.

## Cycle observations

- The shipped axis-2 stub manifests (TB-310, 5a755c9) point at the
  pre-move flat modules. The axis-5 migration of each is now a
  focused "git-move + manifest-update + test-path-fix" pattern —
  identical shape to TB-309's janitor canary. This means axis 5
  migrations can land in parallel and don't require novel design
  per migration (the canary did the design work).
- Operator manually edited TB-310's Verification bullet on 2026-05-28
  (`test "$(... | wc -l)" = "0"` → `! grep -qE ...`) because BSD
  `wc -l` emits padded `       0`. Briefing-shape lesson #7 in memory.
  Ranked proposals below use `! grep -q ...` for absence checks per
  TB-270 absence-check rule.
- Goal.md L184-186 bundles `mattermost/` axis-5 migration WITH axis
  3 ("Mattermost HTTP client, channel/team/bot env knobs, and the
  `mattermost_reply` MCP tool all move together"). TB-312 below
  proposes both in one task, matching that explicit bundling.

## Decisions needed from operator

(none this cycle)

## Proposals this cycle

3 proposals: TB-312 (axis 3 channel-adapter abstraction + axis-5
`mattermost/` migration, bundled per goal.md L184-186), TB-313
(axis-5 `focus_advance/` subpackage migration), TB-314 (axis-5
`auto_unfreeze/` subpackage migration). Slot budget is 5; deliberately
proposing 3 because the remaining unblocked work (axis 4, attention/,
auto_approve/, disabled-config test suite) is sequenced behind these
or behind one of them.
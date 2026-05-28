# Ideation State

_Last updated: 2026-05-28T09:01:00Z by ideation cron_

## Mission alignment

Cycle entry: board 0A / 0R / 0B / 0P / 186C / 0F — last cycle's three
proposals (TB-312, TB-313, TB-314) all landed within ~90 min and the
board fully drained. The 3 most recent Completes — TB-312 (axis 3
channel-adapter ABC + `mattermost/` subpackage migration bundled per
goal.md L184-186, commit 860b68a), TB-313 (axis-5 `focus_advance/`
subpackage migration, commit 6b4fcea), TB-314 (axis-5 `auto_unfreeze/`
subpackage migration, commit 73f5a52) — each pinned the now-canonical
"git-move + manifest-with-hook_points + late-bind in daemon" shape
from TB-309's janitor canary. Every commit preserved observable
behavior (zero env-knob renames, the existing kill switches still bite
identically) and shipped per-task regression pins (10 tests each in
TB-313/TB-314). Mission progress is squarely on the axis the operator
just re-engaged (2026-05-28T04:37:52Z rewind_focus): "refactor
features into opt-in components".

## Current focus assessment

- **Current focus: refactor features into opt-in components**
  - Progress so far:
    - Axis (1) registry + manifest schema + `janitor/` canary
      LANDED (TB-309, cee1c73).
    - Axis (2) tick-hook protocol LANDED (TB-310, 5a755c9) — `Phase`
      enum + `Registry.tick_hooks(phase)` + walk at PRE_DISPATCH /
      ATTENTION_EMISSION / POST_DISPATCH; stub manifests for
      auto_approve / auto_unfreeze / attention / focus_advance
      (since TB-313/TB-314 the focus_advance + auto_unfreeze stubs
      are no longer stubs — they're real subpackages).
    - Axis (3) channel-adapter abstraction LANDED (TB-312, 860b68a)
      — `ap2/channel.py` `ChannelAdapter` ABC + three core sibling
      adapters (`StdoutChannelAdapter`, `FileAppendChannelAdapter`,
      `WebhookChannelAdapter`); `_mm_post` call sites in
      `daemon._maybe_push_attention` + `watchdog._maybe_auto_diagnose`
      now walk the registry's adapter list.
    - Axis (5) `mattermost/` migration LANDED (TB-312, 860b68a —
      bundled per goal.md L184-186).
    - Axis (5) `focus_advance/` migration LANDED (TB-313, 6b4fcea).
    - Axis (5) `auto_unfreeze/` migration LANDED (TB-314, 73f5a52).
    - Axis (6) import-direction CI gate LANDED (TB-311, bafc891) —
      first half of axis 6.
  - Gaps (in sequenced order per goal.md L216-221):
    - Axis (5) `attention/` migration NOT STARTED — flat
      `ap2/attention.py` (879 lines) still exists; stub manifest at
      `ap2/components/attention/manifest.py` late-binds via `from ap2
      import daemon as _daemon_mod`. Per goal.md L187-188 attention
      sequences AFTER axis 3 (channel-adapter); axis 3 shipped in
      TB-312, so attention is now unblocked.
    - Axis (4) validator pipeline as list + `validator_judge/`
      migration NOT STARTED. `_validate_briefing_structure` in
      `ap2/briefing_validators.py` (1133 lines) calls TB-154/TB-161/
      TB-164/TB-171/TB-235/TB-308 checks inline; the TB-235
      LLM-judge call sits at L1105-1129 (imports
      `_check_dependency_coherence` from flat `ap2/validator_judge.py`,
      898 lines, also imported by `ap2/tools.py` + `ap2/doctor.py`).
      Goal.md L218 says axis 4 "gates on (5)'s validator_judge
      migration" — the two ship together.
    - Axis (5) `auto_approve/` migration NOT STARTED — flat
      `ap2/auto_approve.py` (743 lines) still exists; stub manifest
      late-binds via daemon. Per goal.md L196-197 sequenced LAST
      (largest blast radius — touches ideation, proposal labeling,
      retry semantics, cost guards). Defer to next cycle once the
      attention + validator_judge migrations land.
    - Axis (6) disabled-config test suite NOT STARTED — second half
      of axis 6 (TB-311 only shipped the import-direction gate).
      Goal.md L206-209 names `tests/test_components_disabled.py`
      asserting the full suite passes in the "every component
      disabled" configuration. Now has surface: 4 real subpackages
      (janitor, focus_advance, auto_unfreeze, mattermost) plus the
      core `ap2/channel.py` adapter ABC + sibling defaults — enough
      to assert "core behavior unchanged with every component env
      flag disabled".
  - Status: `in-progress`

## Non-goal risk check

None. All proposed work is the structural refactor the focus asks
for. No env-knob renames (goal.md L64-67 constraint), no goal.md
auto-mutation (L272-277), no behavior-removal during extraction
(L278-282). Each migration preserves existing kill switches
(`AP2_ATTENTION_IMMEDIATE_PUSH`, `AP2_AUTO_UNFREEZE_FIX_SHAPES`,
`AP2_VALIDATOR_JUDGE_*`, etc.).

## Considered & deferred this cycle

- **Axis (5) `auto_approve/` migration proposal this cycle** —
  Goal.md L196-197 places this LAST in migration order ("largest
  blast radius — touches ideation, proposal labeling, retry
  semantics, cost guards; migrate last"). Defer until attention/ +
  validator_judge/ land first; next cycle is the natural slot.
- **Auto-unfreeze of TB-310** — Operator manually edited TB-310's
  Verification bullet on 2026-05-28T05:42:24Z (BSD `wc -l` padding
  fix); the change rode into a state commit as a side effect.
  Process lesson noted in operator log — not actionable from
  ideation (the original task is already Complete).
- **Rejection-pattern check (carried, re-justified)**: operator
  vetoes TB-185/184 (ap2-meta-polish unconnected to focus), TB-231
  (symptom-patching without root-cause), TB-175 (premature
  aggregation), TB-240 (validator whack-a-mole). The three proposals
  below all map directly to a named axis line in goal.md
  (L116-214) — axis 5 attention, axis 4 + axis 5 validator_judge,
  axis 6 disabled-config test — not meta-polish. TB-316 in
  particular is goal.md-mandated restructuring of the validator
  pipeline, NOT another "lint shell bullets" whack-a-mole (TB-172/
  TB-240 shape — those proposed *new* check kinds; TB-316 keeps
  the existing 7 checks identical, only rearranges them into a
  list and extracts the SDK-bearing one as a component).

## Cycle observations

- Three axis-5 migrations landed in a single 90-min window
  (TB-312/313/314). The pattern is now well-grooved: git-mv flat
  module → `__init__.py` of subpackage, write/update `manifest.py`
  exposing every previously-direct-imported symbol in
  `hook_points`, rebind daemon module-level aliases via
  `default_registry().get(...).hook_points[...]`, fix the handful
  of test files that imported the flat path. Briefings can be
  shorter going forward — naming the canary commit (cee1c73 or
  the most-recent sibling) as the shape reference avoids
  re-spelling the design.
- Goal.md L218 explicitly conjoins axes 4 + the `validator_judge/`
  migration ("(4) gates on (5)'s `validator_judge` migration").
  TB-316 below proposes both in one task, matching that explicit
  conjunction (same model as TB-312's axes-3+5 bundling).
- The disabled-config test was deferred last cycle ("today only
  `janitor/` is a true subpackage component, so a 'every component
  disabled' suite has minimal surface to assert against — wait
  until ≥3 subpackages exist"). With TB-312/313/314 landing, the
  count is now 4 (janitor + focus_advance + auto_unfreeze +
  mattermost), well past that threshold. Proposing this cycle.

## Decisions needed from operator

(none this cycle)

## Proposals this cycle

3 proposals: TB-315 (axis-5 `attention/` subpackage migration),
TB-316 (axis-4 validator pipeline-as-list + axis-5 `validator_judge/`
migration, bundled per goal.md L218), TB-317 (axis-6 disabled-config
test suite, second half of axis 6 — complements TB-311's
import-direction gate). Slot budget is 5; deliberately proposing 3
because the only remaining unblocked work after these is
`auto_approve/` (goal.md L196-197 places it LAST), which is the
natural anchor for next cycle.
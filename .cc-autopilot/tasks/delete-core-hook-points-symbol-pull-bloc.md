# TB-388: Delete core hook_points symbol-pull blocks; remove the dead POST_DISPATCH phase

## Goal

Advance the focus "get the component boundary right — loop-level participants
only" toward goal.md's Done-when bullet: "A CI gate fails the build if any core
module directly imports from ap2/components/<name>/. All cross-references flow
through the registry's single generic accessor — no per-kind registration methods,
and no core to component hook_points symbol lookups." Remove the two
wrong-direction couplings still in core: (a) the module-level `hook_points[...]`
symbol-pull alias blocks in `ap2/daemon.py` where core reaches backwards into
component internals (auto_approve, attention), and (b) the dead `POST_DISPATCH`
phase still walked every tick with zero registrants.

Why now: TB-383 moved auto-approve to a PRE_DISPATCH pass but left both the
daemon-level auto_approve symbol-pull block and the now-registrant-less
POST_DISPATCH walk in place — so a dead phase fires every tick and core still
imports component internals, the exact coupling that would force import-direction
CI violations when ideation (axis 4) is later extracted.

## Scope

- Replace the daemon's auto_approve and attention `hook_points[...]` alias blocks:
  the auto-approve gate already runs as a PRE_DISPATCH tick-hook (TB-383) and
  attention runs on its own phase, so relocate any logic still needing those
  symbols into the owning component (or reach it through the component's registered
  hook entry point) until `ap2/daemon.py` no longer reads
  `<component>_manifest.hook_points[...]` for auto_approve or attention.
- Remove `Phase.POST_DISPATCH` from the `Phase` enum (`ap2/registry.py`) and delete
  the `daemon._tick` walk over `tick_hooks(Phase.POST_DISPATCH)`; update the Phase
  docstring and any tests asserting POST_DISPATCH exists.
- Keep observable behavior identical: auto-approve and attention still fire on
  their phases; all env knobs unchanged.

## Design

- `ap2/daemon.py` ~L2185-2230 aliases 18+ auto_approve symbols via
  `_auto_approve_manifest.hook_points[...]`; ~L2311-2345 aliases attention symbols
  the same way — these are core to component symbol pulls (wrong-direction imports,
  not extension points). The validator_judge symbol-pull block is handled by TB-386;
  auto_unfreeze's block is NOT in scope (not named in goal.md's axis-5 list).
- `Phase.POST_DISPATCH` is defined (`ap2/registry.py:178`) and walked
  (`ap2/daemon.py:2747` `for hook in default_registry().tick_hooks(Phase.POST_DISPATCH)`)
  but has zero registrants since `ap2/components/auto_approve/manifest.py:204` moved
  auto_approve's hook to `Phase.PRE_DISPATCH`.

## Verification

- `! grep -rn 'POST_DISPATCH = ' ap2/registry.py` — the dead phase member is removed from the Phase enum.
- `! grep -rn 'tick_hooks(Phase.POST_DISPATCH)' ap2/daemon.py` — the dead-phase walk is gone.
- `! grep -rn '_auto_approve_manifest.hook_points' ap2/daemon.py` — core no longer symbol-pulls auto_approve internals.
- `! grep -rn '_attention_manifest.hook_points' ap2/daemon.py` — core no longer symbol-pulls attention internals.
- `uv run pytest -q ap2/tests/` — the full suite passes.
- `ap2/daemon.py` Prose: auto-approve and attention behavior is reached through their registered tick-hooks / component entry points rather than module-level hook_points alias blocks, with observable behavior unchanged; judge confirms via Read.

## Out of scope

- The auto_unfreeze hook_points block (not named in goal.md's axis-5 list) and the
  validator_judge block (TB-386).
- The generic `contributions(point)` accessor (TB-387).
- The communication component (TB-389) and the ideation component.
# Ideation component (axis 4) — extract the proposal engine behind `Phase.IDEATION`

## Goal

Finish the **Current focus: get the component boundary right — loop-level participants only** by extracting the last genuine loop subsystem still welded into core — the ideation proposal engine. Today `ap2/daemon.py` imports `ideation_halt` (L31) and calls `ideation._maybe_ideate(...)` (L2789) and `ideation_halt.maybe_halt_on_exhaustion(...)` (L2573) inline, so the kernel hard-depends on the proposal engine even though `Phase.IDEATION` has been reserved (and walked-empty) since TB-381. Move ideation into `ap2/components/ideation/` so that — per the Done-when criterion — every loop-level autonomous behavior "lives under `ap2/components/<name>/` and is loaded via the component registry, not via direct import from `ap2/daemon.py`". This axis was sequenced last (largest blast radius); the prerequisites already shipped (the auto-approve decouple and the generic `contributions(point)` accessor are recorded as blockers on this task's codespan), so it is now unblocked.

Why now: cron (TB-381) and communication (TB-389) already proved the tick-phase extraction shape, and the recent boundary work removed the cross-boundary knots (auto-approve in board_edit, judge components, symbol-pull blocks) that previously made ideation inextractable — this is the cheapest moment to finish the boundary before new features recouple the kernel to the proposal engine.

## Scope

- Create `ap2/components/ideation/` (`__init__.py`, `impl.py`, `manifest.py`) and move the ideation trigger gate (`_maybe_ideate`, `force_ideate`) and the roadmap-exhaustion halt (`ideation_halt.maybe_halt_on_exhaustion` + the empty-cycles accounting) into it, registered on the reserved `Phase.IDEATION` tick hook plus the halt hook.
- The component owns the `AP2_IDEATION_*` knob cluster, all `ideation_*` events, and declares `AP2_IDEATION_DISABLED` as its `env_flag` (default-on kill switch, mirroring janitor).
- Remove the inline `ideation` / `ideation_halt` imports and direct calls from `ap2/daemon.py`; the tick drives ideation purely through the registry walk (`default_registry().tick_hooks(Phase.IDEATION)` + the halt hook).
- Preserve behavior bit-for-bit: same trigger conditions (Active empty, Ready+Backlog below threshold, cooldown), same focus-status skip (TB-174), same proposal-record writes (TB-188) and `ideation_proposal_recorded`/`_reconciled` events (TB-196), same forced-ideate bypass.
- Keep the deterministic baseline runners and the operator-queue path in core (per goal L122-126); only the ideation loop participant moves.

## Design

Mirror the cron-canary (TB-381) and communication (TB-389) extraction shape: the new `manifest.py` registers an `IDEATION`-phase tick hook (driving the natural empty-board `_maybe_ideate` path) and the exhaustion-halt hook, with `env_flag="AP2_IDEATION_DISABLED"` / `default_enabled=True`. `impl.py` holds the moved `_maybe_ideate` / `force_ideate` / `maybe_halt_on_exhaustion` bodies and the empty-cycles accounting; the `AP2_IDEATION_*` reads stay value-identical. `daemon._tick` replaces its inline `ideation._maybe_ideate(...)` call and the `ideation_halt.maybe_halt_on_exhaustion(...)` call with the registry walks already stubbed at daemon.py L2756, and drops the top-level `ideation_halt` import (L31). The core→component import-direction gate (TB-311) stays green because core only touches the registry. A back-compat module `__getattr__` on `ap2/ideation.py` may re-export moved symbols if any non-core caller still imports them, following the TB-382/TB-386 pattern.

## Verification

- `uv run pytest -q` — full suite passes.
- `uv run pytest -q ap2/tests/test_core_import_direction.py` — the CI import-direction gate still passes (core never statically imports `ap2/components/ideation/`).
- `test -f ap2/components/ideation/manifest.py` — the ideation component subpackage exists.
- `uv run python -c "from ap2.registry import default_registry as d; m=d().get('ideation'); assert m is not None and m.env_flag=='AP2_IDEATION_DISABLED'"` — the registry exposes an `ideation` component whose `env_flag` is `AP2_IDEATION_DISABLED`.
- `grep -q "AP2_IDEATION_DISABLED" ap2/tests/test_components_disabled.py` — the every-component-disabled enumeration test now includes the ideation kill switch.
- `ap2/daemon.py` Prose: the file no longer calls `ideation._maybe_ideate(...)` or `ideation_halt.maybe_halt_on_exhaustion(...)` inline and no longer imports those modules — the trigger gate and roadmap-exhaustion halt run via the `Phase.IDEATION` tick hook + the registry halt hook; judge confirms via Read of `ap2/daemon.py`.

## Out of scope

- Any change to ideation behavior, prompt text, trigger thresholds, or event payloads — this is a pure structural move (goal L297-301 non-goal).
- The minimal-kernel dispatch→verify→report e2e regression-pin (covered by its own follow-up task).
- Collapsing `registry.hook(...)` into `contributions(...)` — out of focus.
"""focus_advance component manifest (TB-313 axis 5 + TB-329 read-site migration).

Registers the focus-list pointer advance pass as the `PRE_DISPATCH`
tick hook so `daemon._tick` walks
`registry.tick_hooks(Phase.PRE_DISPATCH)` instead of importing the
flat module directly. The implementation lives intra-package at
`ap2/components/focus_advance/__init__.py` (relocated from
`ap2/focus_advance.py` by TB-313); the manifest references the
runtime symbols via `from . import …`.

TB-329 axis-5 read-site migration — chosen resolved-config access shape
=========================================================================
The two operator-tunable knobs the component logically owns
(`auto_advance_disabled`, `empty_cycles`) are now read via the
**`cfg.get_component_value(component, key)`** helper on `Config`
(option 2 of the TB-326 pilot's three candidate shapes — see
`ap2/components/auto_approve/manifest.py` and `ap2/config.py`'s
docstring for the helper). The two legacy flat env names
(`AP2_FOCUS_AUTO_ADVANCE_DISABLED`, `AP2_FOCUS_ADVANCE_EMPTY_CYCLES`)
are no longer read directly inside the component body — the
`_maybe_advance_focus` call sites previously routed through
`goal.auto_advance_disabled()` / `goal.advance_empty_cycles_threshold()`
(env-only helpers in `ap2/goal.py`); they now route through the new
intra-package `_focus_auto_advance_disabled(cfg)` /
`_advance_empty_cycles_threshold(cfg)` helpers, which themselves call
`Config.get_component_value("focus_advance", <key>)`. The env-only
`goal.*` helpers are retained as-is so the `test_tb226_focus_rotation.py`
unit pins (env-knob parser shape) pass without modification per the
briefing's behavior-preservation contract — only the read-site
consumer swapped.

Why option 2 (helper) and not 1 (raw dict) or 3 (per-component
dataclass): TB-326's pilot (b3eba54) ratified the helper as the
lightest-touch incremental shape every remaining cluster reuses
verbatim. Option 1 (`cfg.components_config["focus_advance"][<key>]`)
loses env-only-mode back-compat without an additional wrapper — the
env-only resolution branch (`_load_env_path`) doesn't invoke
`apply_env_overrides`, so `components_config` stays empty and a raw
dict read would skip the operator's shell-exported value. Option 3
(per-component dataclass synthesis) requires a code-gen pass on
every `Manifest.config_schema`. The TB-329 regression-pin at
`ap2/tests/test_tb329_focus_advance_cfg_reads.py` mirrors the
TB-326 / TB-327 / TB-328 five cleavages (grep-absence, TOML-first
read precedence, flat-env back-compat parity, parser default-on-bad-
value semantics preservation including the [1, 20] clamp on
`empty_cycles`, and the manifest's documented access shape).

TB-329 latent-bug fix in `ap2/config_compat.py`: the original
`FLAT_TO_SECTIONED` map (TB-323 a50e686) wrote
`AP2_FOCUS_AUTO_ADVANCE_DISABLED` to
`components.focus_advance.disabled`, but the TB-322 schema (e38bb38)
named the key `auto_advance_disabled` — and `ap2/howto.md` documents
`components.focus_advance.auto_advance_disabled` to the operator. The
bare `disabled` form was a latent bug: under it the flat env value
would silently disappear once the read site swapped (the reverse-
`FLAT_TO_SECTIONED` lookup inside `Config.get_component_value` walks
for `components.focus_advance.auto_advance_disabled` and would miss
the `disabled`-keyed map entry; the cfg-snapshot fallback would
likewise miss the wrongly-keyed write). The TB-329 migration aligns
the back-compat map's sectioned target with the schema + docs so the
three surfaces (TB-322 schema, TB-323 back-compat map, TB-329 read
site) agree end-to-end.

Observable-behavior preservation: the wrapping `_tick_hook` function
self-handles the original stderr-print error surface that
daemon._tick previously emitted via an outer try/except (no
dedicated event type — focus_advance's only briefing-promised events
are `focus_advanced` and `roadmap_complete`; a hook-body exception
surfaces as a stderr diagnostic). The async signature matches the
existing `await _maybe_advance_focus(cfg, sdk)` call shape; the
daemon-side walk checks `asyncio.iscoroutine` on each return value
and awaits when present.

`hook_points` exposure (TB-313): the manifest publishes
`maybe_advance_focus`, `ideation_empty_against_focus`, and
`focus_recent_tail_n` so `ap2/daemon.py`'s module-level alias
rebinds (the pre-TB-313 `daemon._maybe_advance_focus = focus_advance.
_maybe_advance_focus` triad) resolve via
`default_registry().get("focus_advance").hook_points[…]` rather
than a direct `from ap2.components.focus_advance import …`. Core
must not statically import from `ap2/components/` per the TB-311
import-direction gate; the registry's hook-point dict is the
declared cross-reference path (goal.md L57-59). Constants vs.
functions both live in `hook_points`; the dict's value is just a
callable-or-value.
"""
from __future__ import annotations

import sys

from ap2.config_loader import ConfigKey
from ap2.registry import Manifest, Phase

from . import (
    _FOCUS_RECENT_TAIL_N,
    _ideation_empty_against_focus,
    _maybe_advance_focus,
)


async def _tick_hook(cfg, sdk) -> None:
    """Wrap `_maybe_advance_focus(cfg, sdk)` with its original stderr
    error surface (the `[ap2] _maybe_advance_focus error: ...` line
    the pre-TB-310 outer try/except printed in `daemon._tick`).
    """
    try:
        await _maybe_advance_focus(cfg, sdk)
    except Exception as e:  # noqa: BLE001
        print(
            f"[ap2] _maybe_advance_focus error: {type(e).__name__}: {e}",
            file=sys.stderr,
        )


MANIFEST = Manifest(
    name="focus_advance",
    # TB-320: surface TB-226's existing kill switch
    # `AP2_FOCUS_AUTO_ADVANCE_DISABLED` on the manifest so the
    # registry / `ap2 status` render the on/off state correctly and
    # the registry-level enabled filter picks it up. Post-TB-329 the
    # subpackage's internal `_maybe_advance_focus` self-gate routes
    # through `cfg.get_component_value("focus_advance",
    # "auto_advance_disabled")` instead of `goal.auto_advance_disabled()`
    # — the env_flag wiring here remains the registry-facing surface;
    # the cfg-routed call inside the hook body is the runtime surface.
    # `default_enabled=True` → suppress-polarity per
    # `registry.Manifest.is_enabled`'s "env_flag set + default_enabled
    # True → truthy disables" branch (`ap2/registry.py:189-194`).
    env_flag="AP2_FOCUS_AUTO_ADVANCE_DISABLED",
    default_enabled=True,
    hook_points={
        "tick_hook": _tick_hook,
        # TB-313: expose the three symbols `daemon.py` used to alias
        # from the flat module. The daemon resolves them through this
        # dict at module-load time so core never statically imports
        # from `ap2/components/focus_advance/`. Tests that import
        # these directly are exempt per the TB-311 gate's
        # `_iter_core_py_files` skip of `ap2/tests/`.
        "maybe_advance_focus": _maybe_advance_focus,
        "ideation_empty_against_focus": _ideation_empty_against_focus,
        "focus_recent_tail_n": _FOCUS_RECENT_TAIL_N,
    },
    tick_hooks=[(Phase.PRE_DISPATCH, _tick_hook)],
    dependencies=[],
    # TB-322 (axis 3): per-component `config_schema` declarations for
    # the knobs the focus_advance component logically owns. Post-TB-329
    # the subpackage reads both knobs via
    # `cfg.get_component_value("focus_advance", <key>)` from
    # `_maybe_advance_focus` (the new intra-package
    # `_focus_auto_advance_disabled(cfg)` /
    # `_advance_empty_cycles_threshold(cfg)` helpers route through
    # `Config.get_component_value`). The env-only `ap2/goal.py`
    # helpers (`goal.auto_advance_disabled()` /
    # `goal.advance_empty_cycles_threshold()`) are retained for the
    # existing env-only unit pins in `test_tb226_focus_rotation.py`
    # but no longer drive `_maybe_advance_focus`. Both knobs are in
    # `env_reload.HOT_RELOADABLE_KNOBS`, so `hot_reloadable=True`.
    config_schema={
        "auto_advance_disabled": ConfigKey(
            name="auto_advance_disabled",
            type=bool,
            default=False,
            description=(
                "Kill switch for focus-pointer auto-advance (TB-226). "
                "True short-circuits `_maybe_advance_focus` even when "
                "the empty-cycles heuristic would otherwise fire. "
                "Mirrors `AP2_FOCUS_AUTO_ADVANCE_DISABLED`; in "
                "`HOT_RELOADABLE_KNOBS`."
            ),
            hot_reloadable=True,
        ),
        "empty_cycles": ConfigKey(
            name="empty_cycles",
            type=int,
            default=3,
            description=(
                "Number of consecutive empty ideation cycles before "
                "the focus pointer auto-advances (TB-292). Mirrors "
                "`AP2_FOCUS_ADVANCE_EMPTY_CYCLES`; in "
                "`HOT_RELOADABLE_KNOBS`."
            ),
            hot_reloadable=True,
        ),
    },
)

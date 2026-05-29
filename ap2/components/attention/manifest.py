"""attention component manifest (TB-315 axis 5 + TB-328 read-site migration).

Registers the proactive attention-detector → `attention_raised` event
wire-up as the `ATTENTION_EMISSION` tick hook so `daemon._tick` walks
`registry.tick_hooks(Phase.ATTENTION_EMISSION)` instead of importing
the flat module directly. The implementation lives intra-package at
`ap2/components/attention/__init__.py` (relocated from
`ap2/attention.py` by TB-315); the manifest references the runtime
symbols via `from . import …` rather than the pre-TB-315
late-binding `from ap2 import daemon as _daemon_mod` shape.

TB-328 axis-5 read-site migration — chosen resolved-config access shape
=========================================================================
The four operator-tunable knobs the component logically owns
(`task_stuck_threshold_s`, `task_frozen_recency_s`, `debounce_s`,
`immediate_push`) are now read via the **`cfg.get_component_value(component,
key)`** helper on `Config` (option 2 of the TB-326 pilot's three
candidate shapes — see `ap2/components/auto_approve/manifest.py` and
`ap2/config.py`'s docstring for the helper). The four legacy flat env
names (`AP2_TASK_STUCK_THRESHOLD_S`, `AP2_TASK_FROZEN_RECENCY_S`,
`AP2_ATTENTION_DEBOUNCE_S`, `AP2_ATTENTION_IMMEDIATE_PUSH`) are no
longer read directly via the `os.environ` mapping inside the component
body; the back-compat path flows through `Config.get_component_value`'s
reverse-`FLAT_TO_SECTIONED` lookup so a shell-export operator who never
migrated their `.cc-autopilot/env` keeps today's behavior bit-for-bit,
while a TOML-opted operator's `[components.attention]` values win
transparently once env-side overrides are unset.

Why option 2 (helper) and not 1 (raw dict) or 3 (per-component
dataclass): TB-326's pilot (b3eba54) and TB-327's sibling (48ab4a8)
ratified the helper as the lightest-touch incremental shape every
remaining cluster reuses verbatim — option 1 loses env-only-mode
back-compat without an extra wrapper (the env-only resolution branch
doesn't invoke `apply_env_overrides`), and option 3 requires a
code-gen pass on every `Manifest.config_schema`. The TB-328
regression-pin at `ap2/tests/test_tb328_attention_cfg_reads.py`
mirrors the TB-326/TB-327 cleavages (grep-absence, TOML-first read
precedence, flat-env back-compat parity, parser default-on-bad-value
semantics preservation, and the manifest's documented access shape).

Hook-points contract under TB-328: the helpers exposed in
`hook_points` (`task_stuck_threshold_s`, `task_frozen_recency_s`,
`attention_debounce_s`, `is_attention_immediate_push_enabled`) all
acquired a `cfg: Config` argument as part of the migration. The
daemon's module-level alias block at L1959-2003 still resolves the
same callable identities — only the signature changed. The
`AP2_AUTO_APPROVE_COST_APPROACH_PCT` knob lives logically in the
auto_approve cluster (per `FLAT_TO_SECTIONED`) and stays on the
direct env-read path inside `_cost_approach_pct`; its migration is
covered by a separate auto_approve-cluster sweep, not TB-328.

Observable-behavior preservation: the wrapping `_tick_hook` function
preserves the same `[ap2] _maybe_emit_attention_events error: ...`
stderr-print error surface the pre-TB-310 outer try/except emitted in
`daemon._tick`. Since attention emits no dedicated `*_error` event
type today, a hook-body exception surfaces as a stderr diagnostic
line — bit-for-bit pin against the briefing's "purely structural
refactor, zero behavior change" promise.

`hook_points` exposure (TB-315): the manifest publishes every symbol
the daemon previously imported from `ap2/attention.py` (the flat
module path that's gone now) PLUS the daemon-side wire-up helpers
that moved into the subpackage in the same task
(`_maybe_emit_attention_events`, `_maybe_push_attention`, and the
push-state file helpers). The daemon's module-level alias block
resolves each via
`default_registry().get("attention").hook_points[…]` rather than a
direct `from ap2.components.attention import …`. Core must not
statically import from `ap2/components/` per the TB-311 import-
direction gate; the registry's hook-point dict is the declared
cross-reference path (goal.md L57-59). Constants vs. functions /
classes both live in `hook_points`; the dict's value is just a
callable-or-value.
"""
from __future__ import annotations

import sys

from ap2.config_loader import ConfigKey
from ap2.registry import Manifest, Phase

from . import (
    AttentionCondition,
    _attention_debounce_s,
    _attention_push_state_path,
    _cost_approach_pct,
    _is_attention_immediate_push_enabled,
    _load_attention_push_state,
    _maybe_emit_attention_events,
    _maybe_push_attention,
    _parse_ts,
    _save_attention_push_state,
    _task_frozen_recency_s,
    _task_stuck_threshold_s,
    detect_attention_conditions,
    find_last_attention_fire,
    should_suppress,
)


def _tick_hook(cfg, sdk) -> None:
    """Wrap `_maybe_emit_attention_events(cfg)` with its original
    stderr error surface (the `[ap2] _maybe_emit_attention_events
    error: ...` line the pre-TB-310 outer try/except printed in
    `daemon._tick`).

    TB-315: body-local call into the subpackage's
    `_maybe_emit_attention_events`. Pre-TB-315 this function
    late-bound via `from ap2 import daemon as _daemon_mod`; the body
    now lives intra-package so the late-binding shim is gone.
    """
    try:
        _maybe_emit_attention_events(cfg)
    except Exception as e:  # noqa: BLE001
        print(
            f"[ap2] _maybe_emit_attention_events error: "
            f"{type(e).__name__}: {e}",
            file=sys.stderr,
        )


MANIFEST = Manifest(
    name="attention",
    # The TB-282 attention-debounce knob (`AP2_ATTENTION_DEBOUNCE_S`)
    # tunes per-(type, key) suppression inside `should_suppress`; the
    # immediate-push knob (`AP2_ATTENTION_IMMEDIATE_PUSH`) tunes the
    # push branch of `_maybe_push_attention`. Neither is a component
    # enable/disable. Default-on with `env_flag=None` per the
    # registry's polarity rule (goal.md L121-125): always-enabled
    # unless the manifest declares otherwise.
    env_flag=None,
    default_enabled=True,
    hook_points={
        "tick_hook": _tick_hook,
        # TB-315: expose the full daemon-alias surface — every symbol
        # the daemon's pre-TB-315 `attention.<sym>` direct call sites
        # AND the daemon-side wire-up helpers (`_maybe_emit_attention_events`
        # and friends) that moved into the subpackage in this task. The
        # daemon's module-level rebind block resolves each via
        # `default_registry().get("attention").hook_points[…]` rather
        # than a direct `from ap2.components.attention import …`.
        # Tests that import these symbols directly are exempt per the
        # TB-311 gate's `_iter_core_py_files` skip of `ap2/tests/`.
        "AttentionCondition": AttentionCondition,
        "detect_attention_conditions": detect_attention_conditions,
        "find_last_attention_fire": find_last_attention_fire,
        "should_suppress": should_suppress,
        "parse_ts": _parse_ts,
        "task_stuck_threshold_s": _task_stuck_threshold_s,
        "task_frozen_recency_s": _task_frozen_recency_s,
        "cost_approach_pct": _cost_approach_pct,
        "attention_debounce_s": _attention_debounce_s,
        # Daemon-side wire-up helpers (relocated from daemon.py by
        # TB-315; the daemon's module-level alias block re-exposes
        # them as `daemon._maybe_emit_attention_events` etc. so test
        # paths that monkey-patch via `from ap2.daemon import …` stay
        # working).
        "maybe_emit_attention_events": _maybe_emit_attention_events,
        "maybe_push_attention": _maybe_push_attention,
        "attention_push_state_path": _attention_push_state_path,
        "load_attention_push_state": _load_attention_push_state,
        "save_attention_push_state": _save_attention_push_state,
        "is_attention_immediate_push_enabled": _is_attention_immediate_push_enabled,
    },
    tick_hooks=[(Phase.ATTENTION_EMISSION, _tick_hook)],
    dependencies=[],
    # TB-322 (axis 3): per-component `config_schema` declarations for
    # every `AP2_*` knob the subpackage reads via `os.environ.get` in
    # `ap2/components/attention/__init__.py`. Defaults mirror the
    # in-source `DEFAULT_*` constants in `ap2/config.py` (L66-132);
    # every one of these knobs is in `env_reload.HOT_RELOADABLE_KNOBS`
    # (detector sensitivity / push toggle — tune-without-restart per
    # TB-282 / TB-287 / TB-290 / TB-297), so `hot_reloadable=True`
    # across the board.
    config_schema={
        "task_stuck_threshold_s": ConfigKey(
            name="task_stuck_threshold_s",
            type=int,
            default=14400,
            description=(
                "Seconds an Active task may sit without progress "
                "before a `task_stuck` attention condition fires "
                "(TB-282). Mirrors `AP2_TASK_STUCK_THRESHOLD_S`; "
                "in `HOT_RELOADABLE_KNOBS`, so an operator "
                "tightening the floor takes effect on the next tick."
            ),
            hot_reloadable=True,
        ),
        "task_frozen_recency_s": ConfigKey(
            name="task_frozen_recency_s",
            type=int,
            default=86400,
            description=(
                "Recency window (seconds) for `task_frozen` "
                "attention emission — a Frozen task whose most-recent "
                "`retry_exhausted` / `task_failed` event is within "
                "this window surfaces as a fresh attention condition "
                "(TB-287). Mirrors `AP2_TASK_FROZEN_RECENCY_S`."
            ),
            hot_reloadable=True,
        ),
        "cost_approach_pct": ConfigKey(
            name="cost_approach_pct",
            type=int,
            default=75,
            description=(
                "Pre-trip `cost_cap_approach` detector threshold as "
                "percent of `AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP` "
                "(TB-290); fires when the rolling 24h auto-approved "
                "token sum is >= this percent of the cap. Mirrors "
                "`AP2_AUTO_APPROVE_COST_APPROACH_PCT`."
            ),
            hot_reloadable=True,
        ),
        "debounce_s": ConfigKey(
            name="debounce_s",
            type=int,
            default=21600,
            description=(
                "Per-(type, key) debounce window (seconds) for "
                "repeated `attention_raised` emissions (TB-282). "
                "Default ~6h so a still-stuck task re-fires roughly "
                "once per operator workday. Mirrors "
                "`AP2_ATTENTION_DEBOUNCE_S`."
            ),
            hot_reloadable=True,
        ),
        "immediate_push": ConfigKey(
            name="immediate_push",
            type=bool,
            default=False,
            description=(
                "Opt-in: post an immediate Mattermost message on "
                "each `attention_raised` event (TB-297). Default "
                "off so the status-report cron stays the routine "
                "push surface. Mirrors `AP2_ATTENTION_IMMEDIATE_PUSH`."
            ),
            hot_reloadable=True,
        ),
    },
)

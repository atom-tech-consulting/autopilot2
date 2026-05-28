"""auto_approve component manifest stub (TB-310 axis 2).

The auto_approve module's per-task gate logic
(`_was_auto_approved` / `_validator_judge_noisy_paused` /
`_auto_approve_paused` / `_auto_approve_check_violations`) remains
inline inside `daemon._tick`'s task-dispatch block because each
gate evaluates per-task state and emits its own
`auto_approve_paused` / `auto_approve_skipped` / `auto_approve_halted`
event with task-specific payload. Extracting that block into a
single tick-callable belongs to axis (5) of the components focus
(goal.md L116-201).

For axis (2)'s daemon-walks-registry contract, this manifest
registers a no-op `tick_hook` on `POST_DISPATCH`. The daemon's
`_tick` walks `POST_DISPATCH` after task dispatch; today's walk
finds only this no-op so observable behavior is bit-for-bit
unchanged. When axis (5) extracts the gate-application helper, the
no-op stub becomes the real callable and the daemon's inline gate
block goes away — daemon-side code does not change at that point
because it already walks the registry.

The Manifest's `hook_points["tick_hook"]` exposes the same no-op
so a TB-309-pattern lookup
(`default_registry().hook("tick_hook", component="auto_approve")`)
also returns a valid callable for discoverability tests.
"""
from __future__ import annotations

from ap2.registry import Manifest, Phase


def _tick_hook(cfg, sdk) -> None:
    """No-op POST_DISPATCH placeholder.

    auto_approve's gate logic remains inline in daemon._tick until
    axis (5) extracts it. This hook is a registration placeholder
    that satisfies the daemon's walk-every-phase contract uniformly;
    it intentionally does nothing at tick time. See module docstring
    for the rationale.
    """
    return None


MANIFEST = Manifest(
    name="auto_approve",
    # TB-232's `AP2_AUTO_APPROVE_DRY_RUN` is the operator-facing
    # gate-on/gate-off knob, but it lives inside the flat module's
    # `evaluate_auto_approve_decision` because the four gates run
    # in order regardless of the dry-run setting (the knob only
    # affects the terminal `strip` vs `dry_run` branch). No
    # manifest-level enable/disable knob today. Default-on with
    # `env_flag=None`.
    env_flag=None,
    default_enabled=True,
    hook_points={"tick_hook": _tick_hook},
    tick_hooks=[(Phase.POST_DISPATCH, _tick_hook)],
    dependencies=[],
)

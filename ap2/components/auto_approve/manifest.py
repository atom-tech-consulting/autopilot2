"""auto_approve component manifest (TB-318 axis 5).

The gate-evaluation surface lives intra-package at
`ap2/components/auto_approve/__init__.py` (relocated from
`ap2/auto_approve.py` by TB-318); the manifest references the runtime
symbols via `from . import …`.

The per-task gate logic (`_was_auto_approved` /
`_validator_judge_noisy_paused` / `_auto_approve_paused` /
`_auto_approve_check_violations`) remains inline inside `daemon._tick`'s
task-dispatch block because each gate evaluates per-task state and emits
its own `auto_approve_paused` / `auto_approve_skipped` /
`auto_approve_halted` event with task-specific payload. Extracting that
block into a single tick-callable carries observable-behavior risk
(per-task event payloads) and is a separate follow-up refactor; the
canonical TB-318 scope is the structural relocation only.

For axis (2)'s daemon-walks-registry contract, this manifest registers a
no-op `_tick_hook` on `POST_DISPATCH`. The daemon's `_tick` walks
`POST_DISPATCH` after task dispatch; today's walk finds only this no-op
so observable behavior is bit-for-bit unchanged. When a follow-up
extracts the gate-application helper, the no-op stub becomes the real
callable and the daemon's inline gate block goes away — daemon-side
code does not change at that point because it already walks the
registry.

`hook_points` exposure (TB-318): the manifest publishes every symbol
the daemon's pre-TB-318 module-level alias block at L1760-1777 sourced
from the flat module (18 entries — 17 alias lines L1760-1776 plus
`evaluate_auto_approve_decision` at L1777) so the rebinds in
`ap2/daemon.py` resolve via
`default_registry().get("auto_approve").hook_points[…]` rather than a
direct `from ap2.components.auto_approve import …`. Core must not
statically import from `ap2/components/` per the TB-311
import-direction gate; the registry's hook-point dict is the declared
cross-reference path (goal.md L57-59). Constants vs. functions both
live in `hook_points`; the dict's value is just a callable-or-value.
"""
from __future__ import annotations

from ap2.registry import Manifest, Phase

from . import (
    _AUTO_APPROVE_FAILURE_STATUSES,
    _AUTO_APPROVE_UNFREEZE_TOKEN,
    _AUTO_APPROVE_WINDOW_RESUME_TOKEN,
    _AUTO_APPROVE_WINDOW_S,
    _append_decisions_needed_bullet,
    _auto_approve_already_halted,
    _auto_approve_check_violations,
    _auto_approve_freeze_threshold,
    _auto_approve_paused,
    _auto_approve_window_resume_idx,
    _auto_approved_task_ids,
    _event_combined_tokens,
    _parse_event_ts,
    _per_task_token_cap,
    _validator_judge_noisy_paused,
    _was_auto_approved,
    _window_token_cap,
    evaluate_auto_approve_decision,
)


def _tick_hook(cfg, sdk) -> None:
    """No-op POST_DISPATCH placeholder.

    auto_approve's per-task gate logic remains inline in `daemon._tick`
    because each gate emits a task-specific event with observable
    payload (see module docstring). This hook is a registration
    placeholder that satisfies the daemon's walk-every-phase contract
    uniformly; it intentionally does nothing at tick time. When a
    follow-up extracts the gate-application helper, this becomes the
    real callable and the daemon's inline block goes away.
    """
    return None


MANIFEST = Manifest(
    name="auto_approve",
    # TB-232's `AP2_AUTO_APPROVE_DRY_RUN` is the operator-facing
    # gate-on/gate-off knob, but it lives inside the subpackage's
    # `evaluate_auto_approve_decision` because the four gates run
    # in order regardless of the dry-run setting (the knob only
    # affects the terminal `strip` vs `dry_run` branch). No
    # manifest-level enable/disable knob today. Default-on with
    # `env_flag=None`; whether to add a master switch is an open
    # operator question surfaced in
    # `.cc-autopilot/ideation_state.md`.
    env_flag=None,
    default_enabled=True,
    hook_points={
        "tick_hook": _tick_hook,
        # TB-318: expose every symbol `daemon.py`'s pre-migration alias
        # block at L1760-1777 sourced from the flat module so core
        # resolves the rebinds via the registry rather than statically
        # importing from `ap2/components/auto_approve/`. Tests that
        # import these symbols directly are exempt per the TB-311
        # gate's `_iter_core_py_files` skip of `ap2/tests/`.
        "_AUTO_APPROVE_FAILURE_STATUSES": _AUTO_APPROVE_FAILURE_STATUSES,
        "_AUTO_APPROVE_UNFREEZE_TOKEN": _AUTO_APPROVE_UNFREEZE_TOKEN,
        "_AUTO_APPROVE_WINDOW_RESUME_TOKEN": _AUTO_APPROVE_WINDOW_RESUME_TOKEN,
        "_AUTO_APPROVE_WINDOW_S": _AUTO_APPROVE_WINDOW_S,
        "_append_decisions_needed_bullet": _append_decisions_needed_bullet,
        "_auto_approve_already_halted": _auto_approve_already_halted,
        "_auto_approve_check_violations": _auto_approve_check_violations,
        "_auto_approve_freeze_threshold": _auto_approve_freeze_threshold,
        "_auto_approve_paused": _auto_approve_paused,
        "_auto_approve_window_resume_idx": _auto_approve_window_resume_idx,
        "_auto_approved_task_ids": _auto_approved_task_ids,
        "_event_combined_tokens": _event_combined_tokens,
        "_parse_event_ts": _parse_event_ts,
        "_per_task_token_cap": _per_task_token_cap,
        "_validator_judge_noisy_paused": _validator_judge_noisy_paused,
        "_was_auto_approved": _was_auto_approved,
        "_window_token_cap": _window_token_cap,
        "evaluate_auto_approve_decision": evaluate_auto_approve_decision,
    },
    tick_hooks=[(Phase.POST_DISPATCH, _tick_hook)],
    dependencies=[],
)

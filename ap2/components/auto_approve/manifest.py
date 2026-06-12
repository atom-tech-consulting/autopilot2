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

TB-383 (axis 3): the `_tick_hook` is now a REAL loop pass registered on
`Phase.PRE_DISPATCH` (it was a `POST_DISPATCH` no-op placeholder). The
auto-approve decision is decoupled from `board_edit`'s mutation-time
`add_backlog` branch: proposals are born `@blocked:review` (policy-free
`board_edit`) and this between-runs pass evaluates approval via
`run_auto_approve_pass` — walking Backlog `@blocked:review` tasks and
stripping the token for those that clear the existing
`evaluate_auto_approve_decision` gate chain. PRE_DISPATCH placement is
load-bearing: the daemon walks it before the dispatch stage promotes
Ready tasks, so a proposal added on the previous tick is auto-approved
here and dispatched on this tick exactly as the pre-TB-383 proposal-time
strip produced. The per-task DISPATCH-time gate block in `daemon._tick`
(`_auto_promote_gate_halts`, which emits per-task `auto_approve_paused` /
`auto_approve_skipped` / `auto_approve_halted` events) is unchanged — it
stays the canonical safety check at promote time. The `should_auto_approve`
tags policy + the `AP2_AUTO_APPROVE` / `AP2_AUTO_APPROVE_GATE_TAGS` readers
also relocated from `ideation.py` into this component (TB-383) so the gate
chain is self-contained and the ideation extraction (axis 4) no longer
trips a core→component import.

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

TB-326 axis-5 read-site migration — chosen resolved-config access shape
=========================================================================
The three operator-tunable knobs the component logically owns
(`freeze_threshold`, `per_task_token_cap`, `window_token_cap`) are now
read via the **`cfg.get_component_value(component, key)`** helper on
`Config` (option 2 of the briefing's three candidate shapes — see
`ap2/config.py`'s docstring for the helper). The three legacy flat env
names (`AP2_AUTO_APPROVE_FREEZE_THRESHOLD`,
`AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP`,
`AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP`) are no longer read directly via
the `os.environ` mapping inside the component body; the back-compat
path flows through `Config.get_component_value`'s reverse-
`FLAT_TO_SECTIONED` lookup so a shell-export operator who never
migrated their `.cc-autopilot/env` keeps today's behavior bit-for-bit,
while a TOML-opted operator's `[components.auto_approve]` values win
transparently.

Why option 2 (helper) and not 1 (raw dict) or 3 (per-component
dataclass): option 1 (`cfg.components_config["auto_approve"][<key>]`)
loses the env-only-mode back-compat without an additional wrapper —
the env-only resolution branch (`_load_env_path`) doesn't invoke
`apply_env_overrides`, so `components_config` stays empty and a raw
dict read would skip the operator's shell-exported value. Option 3
(per-component dataclass synthesis from `Manifest.config_schema`) is
the long-term ergonomic shape but requires a code-gen pass at load
time + per-component constructors, deferred to a post-pilot follow-up.
Option 2 is the lightest-touch incremental shape every remaining
cluster (attention, focus_advance, auto_unfreeze, mattermost,
validator_judge, janitor, core) reuses verbatim — see the same helper
used uniformly across the call sites for each migrated knob.

The TB-326 regression-pin
`ap2/tests/test_tb326_auto_approve_cfg_reads.py` checks (1) the
grep-absence of any direct flat-env read inside the component body
(canary anchor pinned to the briefing's grep shape), (2) the
TOML-first read precedence, (3) the flat-env back-compat parity, (4)
the parser default-on-bad-value semantics preservation, and (5) this
docstring's documentation contract for the follow-up clusters.
"""
from __future__ import annotations

from ap2.config_loader import ConfigKey
from ap2.registry import Manifest, Phase

from . import (
    AUTO_APPROVE_DEFAULT_GATE_TAGS,
    _AUTO_APPROVE_FAILURE_STATUSES,
    _AUTO_APPROVE_UNFREEZE_TOKEN,
    _AUTO_APPROVE_WINDOW_RESUME_TOKEN,
    _AUTO_APPROVE_WINDOW_S,
    _append_decisions_needed_bullet,
    _auto_approve_already_halted,
    _auto_approve_check_violations,
    _auto_approve_freeze_threshold,
    _auto_approve_gate_tags,
    _auto_approve_paused,
    _auto_approve_window_resume_idx,
    _auto_approved_task_ids,
    _event_combined_tokens,
    _is_auto_approve_enabled,
    _parse_event_ts,
    _per_task_token_cap,
    _validator_judge_noisy_paused,
    _was_auto_approved,
    _window_token_cap,
    evaluate_auto_approve_decision,
    run_auto_approve_pass,
    should_auto_approve,
)


def _tick_hook(cfg, sdk) -> None:
    """PRE_DISPATCH loop pass: strip `@blocked:review` from gate-clearing
    Backlog tasks (TB-383 axis 3).

    No longer the pre-TB-383 no-op placeholder. `board_edit` is now
    policy-free (every proposal is born `@blocked:review`); this hook is
    the discrete loop pass that evaluates approval BETWEEN agent runs,
    reusing the `evaluate_auto_approve_decision` gate chain verbatim. It
    walks Backlog `@blocked:review` tasks, strips the token for tasks that
    clear the gates (master knob + tags + freeze/violation/window), and
    emits the existing `auto_approved` / `would_auto_approve` events with
    unchanged payloads. Registered on `Phase.PRE_DISPATCH` so the daemon
    runs it before the dispatch stage promotes Ready tasks — a proposal
    added on the previous tick is auto-approved here and dispatched on this
    tick exactly as the pre-TB-383 proposal-time strip produced.

    Delegates to `run_auto_approve_pass(cfg)`; the per-task DISPATCH-time
    gate block (`_auto_promote_gate_halts` in `daemon._tick`) stays where
    it is — it emits per-task `auto_approve_paused` / `auto_approve_skipped`
    / `auto_approve_halted` events at promote time and is the canonical
    safety check, unchanged by this extraction.
    """
    run_auto_approve_pass(cfg)


MANIFEST = Manifest(
    name="auto_approve",
    # TB-320: wire the existing opt-in master knob `AP2_AUTO_APPROVE`
    # (TB-223's require-polarity gate the daemon's tick-hook code at
    # `daemon._tick` self-gates on via `os.environ.get` reads inside
    # `operator_queue.py` / `board_edits.py` / `ideation.py`) onto the
    # manifest so the registry / `ap2 status` render the on/off state
    # correctly and the registry-level briefing-validator filter picks
    # it up. `default_enabled=False` → require-polarity per
    # `registry.Manifest.is_enabled`'s "env_flag set + default_enabled
    # False → truthy enables" branch (`ap2/registry.py:189-194`); the
    # operator opts into the autonomous-approve behavior by setting
    # `AP2_AUTO_APPROVE=1` (the existing semantics). Internal
    # self-gating stays in place — manifest wiring is informational
    # at the registry layer, not a replacement for the per-call-site
    # truthy parse the existing code performs.
    env_flag="AP2_AUTO_APPROVE",
    default_enabled=False,
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
        # TB-383: the auto-approve tags policy (relocated from `ideation.py`)
        # + the loop-pass entry point are owned by this component now. Exposed
        # here so the gate-chain surface is fully registry-visible (a future
        # ideation component resolves the tags policy via the registry rather
        # than reaching back into core).
        "AUTO_APPROVE_DEFAULT_GATE_TAGS": AUTO_APPROVE_DEFAULT_GATE_TAGS,
        "_is_auto_approve_enabled": _is_auto_approve_enabled,
        "_auto_approve_gate_tags": _auto_approve_gate_tags,
        "should_auto_approve": should_auto_approve,
        "run_auto_approve_pass": run_auto_approve_pass,
    },
    # TB-383: the loop pass runs at PRE_DISPATCH (was a POST_DISPATCH no-op)
    # so the daemon strips `@blocked:review` from gate-clearing Backlog tasks
    # BEFORE the dispatch stage promotes Ready tasks on the same tick.
    tick_hooks=[(Phase.PRE_DISPATCH, _tick_hook)],
    dependencies=[],
    # TB-322 (axis 3): per-component `config_schema` declarations for
    # the auto-approve knobs the component logically owns
    # (`AP2_AUTO_APPROVE`, `AP2_AUTO_APPROVE_DRY_RUN`,
    # `AP2_AUTO_APPROVE_FREEZE_THRESHOLD`,
    # `AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP`,
    # `AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP`).
    # The three threshold/cap knobs (`FREEZE_THRESHOLD`,
    # `PER_TASK_TOKEN_CAP`, `WINDOW_TOKEN_CAP`) are read via
    # `os.environ.get` in `ap2/components/auto_approve/__init__.py`
    # (call sites L85 / L269 / L287); the master switch
    # `AP2_AUTO_APPROVE` and the dry-run knob `AP2_AUTO_APPROVE_DRY_RUN`
    # are read elsewhere today (daemon.py / operator_queue.py /
    # board_edits.py / ideation.py) but are listed here because the
    # briefing's per-component-ownership contract puts them on this
    # manifest (axis-5 read-site migrations will move the
    # `os.environ.get` calls themselves intra-package). Every knob is
    # in `env_reload.HOT_RELOADABLE_KNOBS`, so `hot_reloadable=True`
    # across the board.
    config_schema={
        "enabled": ConfigKey(
            name="enabled",
            type=bool,
            default=False,
            description=(
                "Opt-in master switch for autonomous board-edit "
                "auto-approval (TB-223). Default off so a fresh "
                "install keeps operator-in-the-loop semantics. "
                "Mirrors `AP2_AUTO_APPROVE`; in `HOT_RELOADABLE_KNOBS`."
            ),
            hot_reloadable=True,
        ),
        "dry_run": ConfigKey(
            name="dry_run",
            type=bool,
            default=False,
            description=(
                "Monitor-only mode (TB-232): runs the gate-evaluation "
                "path and emits `would_auto_approve` instead of "
                "applying the queued board-edit. Mirrors "
                "`AP2_AUTO_APPROVE_DRY_RUN`."
            ),
            hot_reloadable=True,
        ),
        "freeze_threshold": ConfigKey(
            name="freeze_threshold",
            type=int,
            default=3,
            description=(
                "Number of consecutive failed `task_complete` events "
                "that trips the auto-approve circuit-breaker "
                "(TB-223). 0 or negative disables the circuit "
                "breaker. Mirrors `AP2_AUTO_APPROVE_FREEZE_THRESHOLD`."
            ),
            hot_reloadable=True,
        ),
        "per_task_token_cap": ConfigKey(
            name="per_task_token_cap",
            type=int,
            default=0,
            description=(
                "Per-task token cap for auto-approved tasks (TB-224). "
                "0 (default) disables the cap; positive values trip "
                "the per-task halt path. Mirrors "
                "`AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP`."
            ),
            hot_reloadable=True,
        ),
        "window_token_cap": ConfigKey(
            name="window_token_cap",
            type=int,
            default=0,
            description=(
                "24h rolling-window token cap across all auto-approved "
                "tasks (TB-224). 0 (default) disables the cap; "
                "positive values trip the window halt path and "
                "require `ap2 ack auto_approve_window_resume`. "
                "Mirrors `AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP`."
            ),
            hot_reloadable=True,
        ),
        # TB-336 axis-5 (TB-330 precedent): the attention component's
        # `_detect_cost_cap_approach` reads this cross-component knob
        # via `cfg.get_component_value("auto_approve", "cost_approach_pct")`
        # (the migration moved the read from `os.environ.get` at the
        # detector to the cfg helper). Declared here so the schema +
        # the `ap2-config` skill's `## Config keys (TOML)` reference +
        # the `test_every_config_key_documented` gate all agree,
        # mirroring how `freeze_threshold` / `per_task_token_cap` /
        # `window_token_cap` are owned by this manifest while their
        # actual reads happen in callsites that already have `cfg` in
        # scope.
        "cost_approach_pct": ConfigKey(
            name="cost_approach_pct",
            type=int,
            default=75,
            description=(
                "Pre-trip approach percentage for the rolling-24h "
                "auto-approved token window cap (TB-290). When the "
                "rolling-window sum reaches "
                "`cost_approach_pct / 100 * window_token_cap` (and "
                "`window_token_cap > 0`), the attention detector "
                "raises a `cost_cap_approach` bullet so the "
                "walk-away operator can react before the post-trip "
                "`auto_approve_paused` surface fires. Values >= 100 "
                "are clamped to 99 (the trip line is owned by the "
                "post-trip detector). Mirrors "
                "`AP2_AUTO_APPROVE_COST_APPROACH_PCT`."
            ),
            hot_reloadable=True,
        ),
    },
)

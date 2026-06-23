"""Ideation component implementation (TB-391 axis 4 — proposal engine).

The ideation proposal engine — the last genuine loop subsystem still
welded into core — relocated behind the registry's `Phase.IDEATION`
tick hook + a `Phase.PRE_DISPATCH` roadmap-exhaustion halt hook. Mirrors
the cron-canary (TB-381) and communication (TB-389) extraction shape:

  - The natural empty-board trigger gate (`_maybe_ideate`), the shared
    SDK-invocation helper (`_run_ideation`), the per-cycle slot budget
    (`_compute_slots`), the manual operator-forced run (`force_ideate`),
    the post-write exhaustion-language scrub (`_maybe_scrub_ideation_state`),
    and the ideation knob readers (`_cooldown_s` /
    `_trigger_task_count` / `_ideation_disabled`) all moved here verbatim
    from `ap2/ideation.py`.
  - The roadmap-exhaustion detector (`maybe_halt_on_exhaustion` + the
    empty-cycles accounting `_consecutive_empty_ideation_cycles` + the two
    halt-knob readers) moved here verbatim from `ap2/ideation_halt.py`.

`ap2/ideation.py` and `ap2/ideation_halt.py` survive as back-compat
`__getattr__` shims (the TB-382 / TB-386 pattern) that re-export the moved
symbols, so every non-core caller (tests, the web-home gate mirror via
`ideation._cooldown_s`, etc.) keeps resolving. The read-layer parsers
(`parse_operator_decisions` / `parse_focus_statuses`), the prompt loader
(`load_prompt`, whose `__file__`-relative path resolves `ideation.default.md`
in `ap2/`), and the shared constants (`IDEATION_NAME`,
`IDEATION_RELEVANT_EVENT_TYPES`, the `*_DEFAULT` knobs,
`AUTO_APPROVE_FREEZE_THRESHOLD_DEFAULT`) stay in core `ap2/ideation.py`
— they are read-layer / shared data, not loop participants (cf. cron's
interval-engine primitives staying in `ap2/cron.py`).

Import-direction: `daemon._tick` resolves this component purely via the
registry (`default_registry().tick_hooks(Phase.IDEATION)` for the natural
path, the PRE_DISPATCH walk for the halt, and the `force_ideate`
hook-point for the operator-forced run). It never statically imports
`ap2/components/ideation/`; the TB-311 CI import-direction gate stays
green. A component may import core freely (component → core); only
core → component is gated.

The tick-hook wrappers route the natural / forced / halt calls back
through the core `ap2.ideation` / `ap2.ideation_halt` module namespaces
(`_ideation_core._maybe_ideate`, etc.) rather than calling the local
bodies directly, so the long-standing test contract
`monkeypatch.setattr(ideation, "_maybe_ideate", noop)` (used by the
daemon-tick tests to silence ideation while exercising another stage)
keeps controlling what the daemon runs.
"""
from __future__ import annotations

import os
import sys
import time

from ap2 import _shared, events, goal
from ap2 import ideation as _ideation_core
from ap2 import ideation_halt as _ideation_halt_core
from ap2.config import Config
from ap2.cron import load_state, mark_run
from ap2.registry import default_registry


# Module-level component name so the self-gate + registry lookup read
# from one source (mirrors the cron component's `COMPONENT_NAME`).
COMPONENT_NAME = "ideation"

_RECENT_TAIL_N = 200


# ============================================================================
# Ideation knob readers (moved verbatim from `ap2/ideation.py`).
# ============================================================================


def _cooldown_s(cfg: "Config | None" = None) -> int:
    """Effective cooldown (seconds), env-overridable.

    TB-335 (axis-5 core-cluster migration): resolves through
    ``cfg.get_core_value("ideation_cooldown_s", default=None)`` —
    the sectioned-env > flat-env > TOML-snapshot > default precedence
    chain `Config.get_core_value` defines (TB-334). Default ``cfg=None``
    preserves the legacy env-read fallback so test callers that
    `monkeypatch.setenv("AP2_IDEATION_COOLDOWN_S", ...)` without
    threading a Config keep working bit-for-bit.

    Cfg-kwarg-+-TypeError-guard shape per TB-327 (the sibling cross-
    package migrations TB-332 / TB-333 adopted the same template) —
    a positional non-Config (e.g. ``cfg="42"``) trips the guard so a
    miswired call surfaces at the boundary instead of getting silently
    coerced.
    """
    if cfg is not None and not isinstance(cfg, Config):
        raise TypeError(
            "_cooldown_s(cfg=...) expects a Config instance; "
            f"got {type(cfg).__name__}",
        )
    if cfg is not None:
        v = cfg.get_core_value("ideation_cooldown_s", default=None)
    else:
        # Legacy fallback (TB-335 back-compat shape — `os.getenv` for
        # cross-package grep-gate hygiene; the canonical NEW-read path
        # is `cfg.get_core_value`).
        v = os.getenv("AP2_IDEATION_COOLDOWN_S")
    if v is None or v == "":
        return _ideation_core.IDEATION_COOLDOWN_DEFAULT_S
    try:
        return int(v)
    except (TypeError, ValueError):
        return _ideation_core.IDEATION_COOLDOWN_DEFAULT_S


def _trigger_task_count(cfg: "Config | None" = None) -> int:
    """Effective Ready+Backlog trigger threshold, env-overridable.

    Reads ``AP2_IDEATION_TRIGGER_TASK_COUNT`` (flat) /
    ``[core.ideation_trigger_task_count]`` (TOML) /
    the sectioned-env equivalent (``AP2_CORE_`` prefix per the TB-323
    regime) via
    ``cfg.get_core_value("ideation_trigger_task_count", default=None)``
    (TB-335 axis-5 core-cluster migration). Same permissive parsing
    style as ``_cooldown_s``: invalid (non-int, non-positive, empty)
    values fall back to the module default silently. A value <= 0
    would make the gate impossible to clear (every count >= 0
    satisfies ``count >= 0``), so we treat that as invalid too.

    Default ``cfg=None`` preserves the legacy env-read fallback so
    test callers (`test_ideation_trigger.py`) that
    ``monkeypatch.setenv(...)`` without threading a Config keep
    working bit-for-bit. Cfg-kwarg-+-TypeError-guard shape per TB-327.
    """
    if cfg is not None and not isinstance(cfg, Config):
        raise TypeError(
            "_trigger_task_count(cfg=...) expects a Config instance; "
            f"got {type(cfg).__name__}",
        )
    if cfg is not None:
        v = cfg.get_core_value("ideation_trigger_task_count", default=None)
    else:
        # Legacy fallback (TB-335 back-compat shape — `os.getenv` for
        # cross-package grep-gate hygiene; see `_cooldown_s`).
        v = os.getenv("AP2_IDEATION_TRIGGER_TASK_COUNT")
    if v is None or v == "":
        return _ideation_core.IDEATION_TRIGGER_TASK_COUNT_DEFAULT
    try:
        parsed = int(v)
    except (TypeError, ValueError):
        return _ideation_core.IDEATION_TRIGGER_TASK_COUNT_DEFAULT
    if parsed > 0:
        return parsed
    return _ideation_core.IDEATION_TRIGGER_TASK_COUNT_DEFAULT


def _ideation_disabled(cfg: "Config | None" = None) -> bool:
    """True iff `AP2_IDEATION_DISABLED` resolves to a truthy value.

    TB-335 (axis-5 core-cluster migration): resolves through
    ``cfg.get_core_value("ideation_disabled")`` — the sectioned-env >
    flat-env > TOML-snapshot > schema-default precedence chain. TB-346
    dropped the redundant inline ``default=""`` so the resolver's
    schema-default backstop (``CORE_CONFIG_SCHEMA["ideation_disabled"]``
    → ``False``) is the single source of truth.

    TB-428: the truthy parse now routes through the canonical
    ``ap2._shared.is_truthy`` (bool-safe + case-insensitive) instead of
    the pre-TB-428 ``str(... or "").strip() in ("1", "true", "yes")``
    shape. That old shape stringified BEFORE a lowercase-only membership
    test, so a TOML ``[core] ideation_disabled = true`` — which
    ``get_core_value`` returns as the Python bool ``True`` — became
    ``str(True)`` → ``"True"`` and silently failed the lowercase set,
    reading the gate as NOT disabled even though the operator set the
    documented key. The shared helper short-circuits a real bool and
    lowercases string forms, so both ``True`` and ``"True"`` now engage
    the gate. The truthy vocabulary and unset→False default are unchanged.

    Default ``cfg=None`` preserves the legacy env-read fallback so
    test callers that ``monkeypatch.setenv("AP2_IDEATION_DISABLED",
    "1")`` without threading a Config keep working bit-for-bit.
    Cfg-kwarg-+-TypeError-guard shape per TB-327.
    """
    if cfg is not None and not isinstance(cfg, Config):
        raise TypeError(
            "_ideation_disabled(cfg=...) expects a Config instance; "
            f"got {type(cfg).__name__}",
        )
    if cfg is not None:
        raw = cfg.get_core_value("ideation_disabled")
    else:
        # Legacy fallback (TB-335 back-compat shape — `os.getenv` for
        # cross-package grep-gate hygiene; see `_cooldown_s`).
        raw = os.getenv("AP2_IDEATION_DISABLED")
    return _shared.is_truthy(raw)


# ============================================================================
# Post-write exhaustion-language scrub (moved from `ap2/ideation.py`).
# ============================================================================


def _maybe_scrub_ideation_state(cfg: Config, sdk) -> None:
    """TB-284 / TB-294: scrub exhaustion language from ``ideation_state.md``.

    Reads the file the ideation control-agent just wrote, runs it
    through ``ideation_scrub.scrub_exhaustion_language``, and overwrites
    if the scrubbed text differs. Emits ``ideation_state_scrubbed``
    with ``removed_chars=<N>`` when the scrub actually modified the
    file; silent no-op when the scrubbed text matches byte-for-byte
    (an already-clean file is the steady-state happy path once the
    scrub has trained the file's content shape).

    TB-294: catches the typed scrub failure exceptions
    (``ScrubTimeoutError`` / ``ScrubSDKError`` /
    ``ScrubEmptyOutputError``) and emits ``ideation_state_scrub_error``
    so the operator sees a broken scrub instead of the pre-TB-294
    silent fail-open. The file is NOT overwritten on the error path —
    fail-safe semantics from TB-284 are preserved at this layer.

    Fail-safe at every layer:
      - File missing (first-ever cycle / agent skipped the write) →
        silent return.
      - File unreadable / unwritable → silent return (the next cycle
        will retry against the next agent write).
      - Typed scrub exception (``ScrubError`` subclass) → emit
        ``ideation_state_scrub_error`` with the matching ``reason``
        (``timeout`` / ``sdk_error`` / ``empty_output``) + wall-clock
        ``duration_s`` and (for the non-timeout paths) ``error``
        carrying the exception type name. Original file preserved on
        disk.

    Lazy import of ``ideation_scrub`` mirrors the lazy-import pattern
    used elsewhere in this module and avoids surfacing
    ``claude_agent_sdk`` at component import time on test paths
    that mock the SDK out.
    """
    target = cfg.project_root / ".cc-autopilot" / "ideation_state.md"
    if not target.is_file():
        return
    try:
        original = target.read_text()
    except OSError:
        return
    if not original.strip():
        return
    from ap2 import ideation_scrub
    started = time.monotonic()
    try:
        scrubbed = ideation_scrub.scrub_exhaustion_language(
            original, sdk=sdk, cfg=cfg,
        )
    except ideation_scrub.ScrubTimeoutError as exc:
        events.append(
            cfg.events_file,
            "ideation_state_scrub_error",
            reason="timeout",
            duration_s=round(time.monotonic() - started, 3),
            error=str(exc),
        )
        return
    except ideation_scrub.ScrubSDKError as exc:
        events.append(
            cfg.events_file,
            "ideation_state_scrub_error",
            reason="sdk_error",
            duration_s=round(time.monotonic() - started, 3),
            error=str(exc),
        )
        return
    except ideation_scrub.ScrubEmptyOutputError as exc:
        events.append(
            cfg.events_file,
            "ideation_state_scrub_error",
            reason="empty_output",
            duration_s=round(time.monotonic() - started, 3),
            error=str(exc),
        )
        return
    if scrubbed == original:
        # Steady-state happy path: the file was already clean (or the
        # SDK returned the input verbatim because no sentence matched
        # the delete criteria). No event, no rewrite.
        return
    try:
        # Atomic write (tmpfile + rename) so a concurrent reader (the
        # web home page's `parse_focus_statuses` call, `ap2 status`'s
        # `parse_operator_decisions` call) can't observe a partial
        # file. Mirrors `do_ideation_state_write`'s shape.
        tmp = target.with_suffix(".md.tmp")
        tmp.write_text(scrubbed)
        tmp.replace(target)
    except OSError:
        # Write failed — leave the original on disk. The next cycle's
        # scrub pass will try again.
        return
    removed = len(original) - len(scrubbed)
    events.append(
        cfg.events_file,
        "ideation_state_scrubbed",
        removed_chars=removed,
    )


# ============================================================================
# Shared SDK-invocation helper + the natural / forced trigger gates.
# ============================================================================


async def _run_ideation(cfg: Config, sdk, mcp_server, *, slots: int) -> None:
    """Run the ideation control-agent unconditionally.

    All gating (disable knob, cooldown, queue-depth, Active hard gate)
    is the caller's responsibility — this helper is the actual SDK
    invocation, prompt-dump, event emission, cooldown bookkeeping, and
    state-file commit. Both `_maybe_ideate` (natural cron-driven path)
    and `force_ideate` (TB-159 manual operator trigger) reuse this
    helper so they emit the same `ideation_empty_board` /
    `ideation_timeout` / `ideation_error` event vocabulary, advance the
    same cooldown clock, and produce the same state-file commit.

    `slots` is the per-cycle proposal-slot budget computed by the caller
    (TB-183) — `max(0, AP2_IDEATION_TRIGGER_TASK_COUNT - workable_count)`.
    It's appended into the `## Current state` snapshot block via the
    `state_extras` mechanism (TB-151) so the agent can read it as a
    single line: `- proposal slots this cycle: N`. The prompt body's
    "propose at most N" instruction reads N from the same line, replacing
    the hardcoded magic-3 that drifted out of sync with the env knob
    (TB-160 introduced the env knob; the prompt body kept "fewer than 3"
    until TB-183 closed the gap).

    Note: `ideation_empty_board` is the historical entry-marker name —
    kept for backward compatibility even though forced runs may fire
    on a non-empty board. Callers distinguish forced from natural via
    the separate `ideation_forced` event the operator-queue drain
    emits at queue-application time (TB-159).
    """
    state = load_state(cfg.cron_state_file)
    last = state.get(_ideation_core.IDEATION_NAME, 0.0)
    cooldown = _cooldown_s(cfg)
    now = time.time()
    events.append(
        cfg.events_file,
        "ideation_empty_board",
        cooldown_s=cooldown,
        seconds_since_last=int(now - last) if last else None,
    )
    # Lazy imports to avoid daemon ↔ ideation circular dependency.
    from ap2 import daemon as _daemon
    from ap2 import prompts
    from ap2.tools import IDEATION_TOOLS

    # TB-168: ideation opts out of the board-counts and recent-commits
    # sub-blocks of `_current_state_block`. The board snapshot is
    # redundant — ideation reads `TASKS.md` directly per its read-order
    # and gets per-section detail with full task titles. The 10 recent
    # commits are ~60% `state:` daemon meta-commits with no signal, and
    # the remaining shipped-feature lines are subsumed by `progress.md`
    # (Step 5 of `ap2/ideation.default.md`). `now:` survives — it's
    # ideation's only deterministic clock for the `_Last updated:` line
    # in the `ideation_state.md` schema.
    #
    # TB-169: ideation also opts in to event-type filtering — the
    # rendered `## Recent events` tail keeps only the kinds ideation
    # actually keys off (lifecycle, operator decisions, cron
    # proposals). `judge_call` / `task_run_usage` / `control_run_usage`
    # / cron-lifecycle / mattermost / daemon-plumbing events are
    # dropped before the 6KB `format_for_prompt` byte cap, so the
    # signal density of the prompt doesn't degrade as observability
    # event volume grows. See `IDEATION_RELEVANT_EVENT_TYPES` for the
    # full list and rationale.
    # TB-183: pre-computed proposal-slot count flows into the snapshot
    # block as a single bulleted line the agent reads near the top of
    # the prompt. Joined to any other state_extras consumers in the
    # future via the same `## Current state` mechanism (TB-151 /
    # TB-163). The prompt body's "propose at most N" instruction reads
    # N from this line — single source of truth, no hardcoded magic
    # number drifting out of sync with `AP2_IDEATION_TRIGGER_TASK_COUNT`.
    state_extras = [f"- proposal slots this cycle: {slots}"]
    full_prompt = prompts.build_control_prompt(
        cfg, _ideation_core.IDEATION_NAME, _ideation_core.load_prompt(cfg),
        state_extras=state_extras,
        include_board=False, include_commits=False,
        include_types=_ideation_core.IDEATION_RELEVANT_EVENT_TYPES,
    )
    # TB-336 axis-5 straggler (TB-334 core-cluster tail): the read routes
    # through `cfg.get_core_value("ideation_max_turns", default=…)`
    # which evaluates sectioned env (`AP2_CORE_<KEY>`) > flat env
    # (`AP2_IDEATION_MAX_TURNS` via reverse-`FLAT_TO_SECTIONED` lookup)
    # > `cfg.core_config` snapshot > default at call time. Same
    # call-time env-first contract as the TB-334 agent-runtime cluster.
    max_turns = int(
        cfg.get_core_value(
            "ideation_max_turns",
            default=_ideation_core.IDEATION_MAX_TURNS_DEFAULT,
        )
    )
    # TB-126: snapshot the state surface before ideation runs so the post-
    # run state commit only stages paths ideation actually touched (new
    # briefings, ideation_state.md, TASKS.md / CLAUDE.md from add_backlog,
    # any insights). Briefings already in the working tree from a prior op
    # do NOT ride along.
    pre_snapshot = _daemon._snapshot_state_paths(cfg)
    # TB-89 / TB-192: refresh the insights index. Ideation's Step 0.5 reads
    # `.cc-autopilot/insights/_index.md` for grounding, so the regen must
    # happen BEFORE the control agent starts. It must also happen AFTER
    # `pre_snapshot` so any rewrite to `_index.md` shows up in the post-run
    # diff (`_changed_state_paths`) and rides along in the `state: ideation`
    # commit — TB-192 caught the pre-fix ordering: regen ran before the
    # snapshot, so a rewritten `_index.md` was part of the snapshot baseline,
    # silently sat dirty in the working tree, and broke linear-rollback
    # cohesion (TB-111/TB-112). Lazy: no-op when nothing changed. A failure
    # here must NOT block the run.
    try:
        from ap2 import insights

        insights.maybe_regenerate_index(cfg)
    except Exception:  # noqa: BLE001
        pass
    timed_out, error, stderr_tail, prompt_dump = await _daemon._run_control_agent(
        cfg,
        sdk,
        mcp_server,
        label="ideation",
        prompt=full_prompt,
        allowed_tools=IDEATION_TOOLS,
        max_turns=max_turns,
    )
    if timed_out:
        events.append(
            cfg.events_file,
            "ideation_timeout",
            timeout_s=cfg.control_timeout_s,
            stderr_tail=stderr_tail,
            prompt_dump=str(prompt_dump),
        )
    elif error is not None:
        events.append(
            cfg.events_file,
            "ideation_error",
            error=error,
            stderr_tail=stderr_tail,
            prompt_dump=str(prompt_dump),
        )
    # TB-284: scrub exhaustion language from ideation_state.md AFTER the
    # control agent has finished writing (the agent's `ideation_state_write`
    # tool call lands its content during `_run_control_agent`). Removes
    # any sentence claiming a focus / axis is exhausted, near-exhausted, or
    # naming conditions of exhaustion before the state file's text becomes
    # the next cycle's authoritative context. Runs BEFORE the post-snapshot
    # diff so the scrubbed bytes ride along in the same `state: ideation`
    # commit instead of getting committed in a follow-up tick. Fail-safe:
    # `scrub_exhaustion_language` swallows SDK errors and returns the
    # input unchanged, and the file-IO wrapper here swallows OSErrors so
    # a transient FS hiccup can't break the rest of the cycle bookkeeping.
    _maybe_scrub_ideation_state(cfg, sdk)
    # Always advance the cooldown — even on failure — so a broken
    # ideation agent doesn't get hammered every tick. For forced runs
    # this is what makes back-to-back `ap2 ideate` calls still subject
    # to the natural cooldown for the NEXT cron-driven fire (TB-159).
    mark_run(cfg.cron_state_file, _ideation_core.IDEATION_NAME)
    touched = _daemon._changed_state_paths(
        pre_snapshot, _daemon._snapshot_state_paths(cfg)
    )
    if touched:
        _daemon._commit_state_files(cfg, "state: ideation", paths=touched)


def _compute_slots(cfg: Config) -> tuple[int, int, int]:
    """Return `(slots, queued, threshold)` for the current board.

    TB-183: shared helper so `_maybe_ideate` (natural path) and
    `force_ideate` (operator-forced path) compute the same per-cycle
    proposal-slot budget. `slots = max(0, threshold - queued)` —
    `queued` counts Ready+Backlog only (Pipeline Pending and Frozen do
    not count, matching the existing trigger-gate semantics from
    TB-160). The `max(0, ...)` clamp prevents negative slot counts
    when `queued > threshold`.
    """
    from ap2.board import Board

    board = Board.load(cfg.tasks_file)
    queued = sum(
        sum(1 for _ in board.iter_tasks(section=s))
        for s in ("Ready", "Backlog")
    )
    threshold = _trigger_task_count(cfg)
    slots = max(0, threshold - queued)
    return slots, queued, threshold


async def _maybe_ideate(cfg: Config, sdk, mcp_server) -> None:
    """Fire ideation when the working queue is shallow and the cooldown elapsed.

    Gates (in order):
    1. `AP2_IDEATION_DISABLED` opt-out (tests + manual-only projects).
    2. Active hard gate — non-empty Active means a task is in flight and
       sharing the SDK slot with a control agent is unsafe.
    3. Cooldown — `AP2_IDEATION_COOLDOWN_S` since the last fire. This
       gate is positioned ABOVE every emit-and-`mark_run` branch below
       (TB-186) so that those branches' `mark_run` writes actually
       suppress re-emission on the next tick — pre-TB-186 the slot-skip
       branch was positioned BEFORE the cooldown check, so the early
       return short-circuited before the cooldown clock could gate the
       skip event, and `ideation_skipped_no_slots` fired once per ~30s
       tick instead of once per cooldown window.
    4. Per-cycle proposal-slot budget (TB-183) —
       `slots = max(0, AP2_IDEATION_TRIGGER_TASK_COUNT - (Ready+Backlog))`.
       When `slots <= 0` the queue is already at the operator's
       configured threshold, so there's nothing for the agent to fill;
       we emit `ideation_skipped_no_slots` (so the no-op is visible in
       events.jsonl) and advance the cooldown via `mark_run` (so a
       broken board state can't hammer the gate every tick). This
       subsumes the pre-TB-183 `queued >= threshold` silent-return
       check — same trigger condition, but with explicit event +
       cooldown advancement.
    TB-284 removed a fifth gate that previously read
    ``parse_focus_statuses(ideation_state.md)`` and skipped when every
    focus item self-reported ``Status: exhausted-needs-operator``.
    The empty-cycles focus-advance signal (TB-283) is now the
    authority on exhaustion, and TB-284's post-write scrub strips the
    verdict language that was the only thing keeping
    ``exhausted-needs-operator`` values in the cache for that
    predicate to read — the gate became dead code in lockstep with
    the scrub landing.

    Delegates the actual SDK invocation + bookkeeping to `_run_ideation`
    so the forced-run path (`force_ideate`, TB-159) shares the same
    event vocabulary, cooldown writeback, and state-file commit. The
    computed `slots` value flows into `_run_ideation` so the prompt's
    `## Current state` block carries `- proposal slots this cycle: N`
    (TB-183) — the agent reads N from there instead of the
    pre-TB-183 hardcoded magic-3 in the prompt body.

    Set `AP2_IDEATION_DISABLED=1` to opt out entirely (the tests use this
    by default; it's also useful for projects that want to drive ideation
    manually rather than on the natural gate).
    """
    from ap2.board import Board

    if _ideation_disabled(cfg):
        return
    board = Board.load(cfg.tasks_file)
    # Active is a HARD gate independent of the threshold: a task agent and
    # a control agent cannot share the SDK slot safely (TB-159 background).
    # Skip whenever Active is non-empty regardless of how many Ready/Backlog
    # items there are.
    if next(board.iter_tasks(section="Active"), None) is not None:
        return
    state = load_state(cfg.cron_state_file)
    last = state.get(_ideation_core.IDEATION_NAME, 0.0)
    cooldown = _cooldown_s(cfg)
    now = time.time()
    if now - last < cooldown:
        return
    slots, queued, threshold = _compute_slots(cfg)
    if slots <= 0:
        # TB-183: queue at-or-above threshold → no slots to fill. Emit
        # the explicit skip event (so the no-op shows up in events.jsonl
        # rather than vanishing into a silent return) and advance the
        # cooldown so a wedged-at-threshold board doesn't hammer the
        # gate on every tick.
        #
        # TB-186: this branch must run AFTER the cooldown gate above —
        # `mark_run` here only suppresses re-emission on subsequent ticks
        # if the cooldown check actually reads `last_run` before reaching
        # this branch. (The pre-TB-186 ordering placed this branch first,
        # so the early-return short-circuited before the cooldown check
        # ever ran, and the gate fired once per ~30s tick instead of once
        # per cooldown window.)
        events.append(
            cfg.events_file,
            "ideation_skipped_no_slots",
            queued=queued,
            threshold=threshold,
        )
        mark_run(cfg.cron_state_file, _ideation_core.IDEATION_NAME)
        return
    # TB-246: roadmap-complete gate — when the ideation-exhaustion
    # detector (`maybe_halt_on_exhaustion`) has emitted `roadmap_complete`
    # after the empty-cycles threshold tripped AND the operator has
    # not yet edited goal.md (which would clear the halt via
    # `reset_pointer_on_goal_updated`), ideation is parked. Without a
    # matching ideation gate, ideation keeps firing every cooldown
    # window during a walk-away weekend and proposals pile up as
    # `@blocked:review` against an already-exhausted goal (up to ~48
    # wasted SDK calls per 48h × 60-min cooldown). Uses the canonical
    # `goal.roadmap_exhausted` predicate — single source of truth, no
    # new state file. `force_ideate` bypasses this gate so the
    # operator's recovery path (`ap2 update-goal && ap2 ideate
    # --force`) still works before the goal-updated reset has landed
    # at a tick boundary. TB-340: the dismiss verb is NOT part of
    # resume — `ap2 ack roadmap_complete` only quiets the operator
    # nag; ideation stays parked until the operator extends goal.md.
    #
    # TB-342: the collapse from multi-focus rotation to a single
    # ideation-exhaustion detector means resume is now goal.md edit
    # only — the pre-TB-342 `ap2 rewind-focus` recovery verb went
    # away with the rotation theatre, and the
    # `_consecutive_empty_ideation_cycles` counter resets at the most
    # recent `goal_updated` event (the operator's resume signal)
    # instead of the deleted `focus_advanced` rotation event.
    #
    # TB-284 deleted the predecessor focus-exhausted gate that read
    # `parse_focus_statuses(ideation_state.md)` and skipped when
    # every focus item self-reported `exhausted-needs-operator`.
    # The empty-cycles heuristic (TB-283 / TB-342) is now the
    # authority on exhaustion; the post-write scrub
    # (`ideation_scrub.scrub_exhaustion_language`) strips the verdict
    # language that was the only thing producing the cached statuses
    # the deleted gate read.
    if goal.roadmap_exhausted(cfg):
        events.append(
            cfg.events_file,
            "ideation_skipped",
            reason="roadmap_complete",
        )
        mark_run(cfg.cron_state_file, _ideation_core.IDEATION_NAME)
        return
    await _run_ideation(cfg, sdk, mcp_server, slots=slots)


async def force_ideate(cfg: Config, sdk, mcp_server) -> None:
    """Run ideation unconditionally — manual operator trigger (TB-159).

    Bypasses the `AP2_IDEATION_DISABLED` opt-out, the cooldown, the
    Ready+Backlog queue-depth gate, the TB-174 focus-exhausted gate
    (i.e. fires even when every focus item in `ideation_state.md`
    self-reports `Status: exhausted-needs-operator` — that's the
    precise scenario where the operator triggers a forced run after
    refreshing goal.md so the fresh focus has somewhere to land its
    first proposals), AND the TB-246 roadmap-complete gate (i.e.
    fires even when `goal.roadmap_exhausted(cfg)` is True — the
    operator's standard recovery path is `ap2 update-goal && ap2
    ideate --force`, and the forced run must work even before the
    goal_updated reset has landed at a tick boundary. TB-340: `ap2
    ack roadmap_complete` is NOT part of resume — it only dismisses
    the operator nag; resume is editing goal.md (which the
    `update_goal` drain handler turns into a
    `reset_pointer_on_goal_updated` call). TB-342: the pre-existing
    `ap2 rewind-focus` recovery verb went away with the multi-focus
    rotation; resume is goal.md edit only.). Does NOT bypass the
    Active hard gate — that check lives
    at queue-append time in `do_operator_queue_append({"op":
    "ideate", ...})` and at drain time is implicit (the daemon won't
    dispatch the forced run while a task agent is sharing the SDK
    slot).

    Still calls `mark_run` (via `_run_ideation`) after the run so the
    NEXT natural cooldown clock resets — i.e. running `ap2 ideate` ten
    times in a row would still hit a real `AP2_IDEATION_COOLDOWN_S` gap
    before the next cron-driven fire. The `ideation_forced`
    audit event is emitted by the queue-drain side, not here, so this
    helper stays the single SDK-invocation path shared with `_maybe_ideate`.

    TB-183: the per-cycle slot count flows through unchanged — forced
    runs compute the same `max(0, threshold - workable)` against the
    current board so the agent's `## Current state` snapshot still
    carries `- proposal slots this cycle: N`. A forced run with
    `slots=0` is intentional (the operator triggered the run knowing
    the board was full); the prompt body's "if N is 0, do not propose"
    rule still applies, so the agent does the assessment without
    adding tasks.
    """
    slots, _, _ = _compute_slots(cfg)
    await _run_ideation(cfg, sdk, mcp_server, slots=slots)


# ============================================================================
# Roadmap-exhaustion halt (moved verbatim from `ap2/ideation_halt.py`).
# ============================================================================


def _ideation_halt_disabled(cfg: Config) -> bool:
    """True iff the ideation-halt kill switch is set to a truthy value.

    Routes through `cfg.get_core_value("ideation_halt_disabled")`,
    which evaluates the sectioned-env form
    > flat env (`AP2_IDEATION_HALT_DISABLED`, plus the deprecated
    focus-era alias via the
    `FLAT_TO_SECTIONED` reverse lookup) > `cfg.core_config` snapshot >
    the schema default (False) at call time. Call-time env-first
    precedence preserves the lazy-read pattern — `monkeypatch.setenv`
    plus a subsequent helper call picks up the new value without
    rebuilding cfg.

    Truthy enumeration: `"1"` / `"true"` / `"yes"` / `"on"`
    (case-insensitive). The TOML layer's typed `True` / `False` is
    honored directly. Default unset → False (auto-halt enabled).
    """
    raw = cfg.get_core_value("ideation_halt_disabled")
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return False
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _ideation_halt_empty_cycles_threshold(cfg: Config) -> int:
    """Effective threshold for the empty-cycles exhaustion heuristic.

    Routes through `cfg.get_core_value("ideation_halt_empty_cycles")`,
    which evaluates the sectioned-env form
    > flat env
    (`AP2_IDEATION_HALT_EMPTY_CYCLES`, plus the deprecated
    focus-era alias via the `FLAT_TO_SECTIONED`
    reverse lookup) > `cfg.core_config` snapshot > the schema default
    (3) at call time.

    Permissive parse: empty / non-int / whitespace-only values fall
    back to the default (`goal.ADVANCE_EMPTY_CYCLES_DEFAULT` = 3);
    out-of-range values clamp to [`goal.ADVANCE_EMPTY_CYCLES_MIN`,
    `goal.ADVANCE_EMPTY_CYCLES_MAX`] (= [1, 20]) so an operator typo
    (e.g. `0` or `999999`) doesn't disable the halt path or wedge it
    permanently. The clamp constants live in `ap2/goal.py` and are
    referenced here so the parser bounds stay single-source-of-truth.
    """
    raw = cfg.get_core_value("ideation_halt_empty_cycles")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return goal.ADVANCE_EMPTY_CYCLES_DEFAULT
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return goal.ADVANCE_EMPTY_CYCLES_DEFAULT
    if v < goal.ADVANCE_EMPTY_CYCLES_MIN:
        return goal.ADVANCE_EMPTY_CYCLES_MIN
    if v > goal.ADVANCE_EMPTY_CYCLES_MAX:
        return goal.ADVANCE_EMPTY_CYCLES_MAX
    return v


def _consecutive_empty_ideation_cycles(tail: list[dict]) -> int:
    """Count consecutive recent ideation cycles that exited without
    recording a proposal. Reset cutoff: the most recent `goal_updated`
    event (operator edited goal.md via `ap2 update-goal` → fresh
    runway). Cycle-grouped: each ideation cycle is bounded by
    `ideation_empty_board` (daemon-emitted entry marker at
    `ideation._run_ideation`) and one of `ideation_complete` /
    `ideation_cycle_summary` / `ideation_timeout` / `ideation_error`
    (exit). The agent's two-event vocabulary is intentional:
    `ideation_complete` carries a proposal summary (used when ≥1
    proposal landed this cycle), `ideation_cycle_summary` carries a
    no-proposal-reasoning summary (used when 0 proposals landed).
    Either name closes the cycle the same way from the counter's
    perspective.

    Per cycle:

      - Exited via `ideation_complete` OR `ideation_cycle_summary`
        AND no `ideation_proposal_recorded` fired within the cycle →
        increment count by 1.
      - Any `ideation_proposal_recorded` fired within the cycle → on
        either exit marker, reset count to 0 (a fresh proposal landed;
        ideation is still productive).
      - Exited via `ideation_timeout` / `ideation_error` → leave count
        unchanged. These are infrastructure failures (SDK budget
        exhausted, agent crash) — not "ideation reasoned and found
        nothing." Treating them as empty would let transient SDK
        slowness or a network blip falsely trip the halt.

    Events older than the most recent `goal_updated` are ignored (the
    operator extended/edited goal.md → empty cycles before that edit
    don't count against the post-edit runway). Truncated cycles
    (events appearing after the cutoff without their matching
    `ideation_empty_board` entry marker, or a cycle whose exit marker
    fell off the tail) are handled cleanly via the `in_cycle` flag —
    orphan proposal/exit events outside any cycle are ignored, and a
    fresh `ideation_empty_board` resets the flags without spurious
    increments.

    TB-292 restructured this from the prior event-walking flat-
    increment counter to the cycle-grouped semantic that the
    `AP2_IDEATION_HALT_EMPTY_CYCLES` env-knob name advertises ("3
    consecutive empty cycles to trip"). TB-300 then extended the
    exit-marker set from `ideation_complete` alone to also include
    `ideation_cycle_summary`: the agent emits the latter (not the
    former) on every 0-proposal cycle, so under the prior single-name
    predicate every natural empty cycle was invisible to the counter
    and the threshold was structurally unreachable. TB-342 changed the
    reset cutoff to `goal_updated` — the counter no longer scopes to a
    focus title because there is no active focus (the pointer doesn't
    walk anymore); the natural reset signal for the global empty-cycles
    run is "operator extended / edited the goal," which `update_goal`
    emits as `goal_updated`.
    """
    # Reset cutoff: the most recent `goal_updated` event marks the
    # start of the post-edit runway.
    cutoff_idx = -1
    for i, e in enumerate(tail):
        if e.get("type") == "goal_updated":
            cutoff_idx = i
    relevant = tail[cutoff_idx + 1:]

    count = 0
    in_cycle = False
    cycle_had_proposal = False
    for e in relevant:
        typ = e.get("type")
        if typ == "ideation_empty_board":
            # Entry marker: open a fresh cycle. If a prior cycle's exit
            # marker fell off the tail, this implicitly closes it
            # without counting (defensive shape for truncated tails).
            in_cycle = True
            cycle_had_proposal = False
        elif typ == "ideation_proposal_recorded" and in_cycle:
            cycle_had_proposal = True
        elif typ in ("ideation_complete", "ideation_cycle_summary") and in_cycle:
            # TB-300: either name closes the cycle. The agent emits
            # `ideation_complete` when proposals landed and
            # `ideation_cycle_summary` when none did; both arrive via
            # the same `log_event` MCP path at end-of-cycle.
            count = 0 if cycle_had_proposal else count + 1
            in_cycle = False
        elif typ in ("ideation_timeout", "ideation_error") and in_cycle:
            # Infrastructure failure: don't count, don't reset.
            in_cycle = False
    return count


def _append_decisions_needed_bullet(cfg: Config, text: str) -> None:
    """Resolve the auto_approve component's decisions-needed bullet
    writer through the registry hook-point protocol and call it.

    This component must NOT statically import from another component's
    package; the registry's `hook_points` dict is the sanctioned
    cross-reference path (goal.md L57-59: "All cross-references flow
    through the registry's hook protocol"). `ap2.registry` is core, so
    importing it here is allowed; the registry then dynamically loads
    the `auto_approve` component.
    """
    from ap2.registry import default_registry

    writer = default_registry().get("auto_approve").hook_points[
        "_append_decisions_needed_bullet"
    ]
    writer(cfg, text)


def maybe_halt_on_exhaustion(cfg: Config) -> None:
    """Ideation-exhaustion detector (TB-345, merged from the
    `focus_advance` component's residual detector; TB-391 relocated it
    here behind the ideation component's PRE_DISPATCH halt hook).

    Reads goal.md's focus list + the pointer state file. Counts the
    consecutive recent ideation cycles that produced 0 proposals since
    the most recent `goal_updated`. When the count reaches
    `AP2_IDEATION_HALT_EMPTY_CYCLES`, emits `roadmap_complete` once
    (and sets the pointer's `roadmap_complete_emitted` flag), parking
    the ideation trigger until the operator extends goal.md (via
    `ap2 update-goal` — the operator-queue drain handler calls
    `goal.reset_pointer_on_goal_updated` to clear the halt) or fires
    `ap2 ideate --force`. TB-275: task dispatch is NOT affected — only
    the ideation trigger.

    The kill-switch (`AP2_IDEATION_HALT_DISABLED=1`) disables the
    auto-halt: the detector still counts but does not emit
    `roadmap_complete`; instead surfaces the existing decisions-needed
    bullet so the operator can halt manually (e.g. by editing goal.md).
    The detector is NOT gated by the component's `AP2_IDEATION_DISABLED`
    kill switch — it always runs (only `AP2_IDEATION_HALT_DISABLED`
    suppresses the auto-halt), preserving the pre-TB-391 behavior where
    the halt was core ideation lifecycle that fires independently of the
    empty-board trigger gate.

    Pure / side-effect-bounded: writes events + the pointer file +
    (rarely, only on the kill-switch path) one decisions-needed
    bullet. Does NOT mutate goal.md itself. Tolerates a missing
    goal.md / empty focus list gracefully (early return).

    TB-302: the roadmap-complete branch does not append a
    `Roadmap complete: ...` bullet to `.cc-autopilot/ideation_state.md`
    — the pointer-driven `ap2 status` focus line is the canonical
    operator-facing surface. The kill-switch branch further down still
    writes a decisions-needed bullet because operator-killed-but-
    criteria-met has no equivalent naturally-observable focus-line
    surface.
    """
    foci = goal.read_focus_list(cfg)
    if not foci:
        # Pre-pivot goal.md with no `## Current focus:` headings, or
        # missing goal.md entirely. Nothing to halt against.
        return

    pointer = goal.load_pointer(cfg)

    threshold = _ideation_halt_empty_cycles_threshold(cfg)
    tail = events.tail(cfg.events_file, _RECENT_TAIL_N)
    empty_cycles = _consecutive_empty_ideation_cycles(tail)
    # Keep the pointer's empty_cycles field in sync (forensic /
    # observability surface for `ap2 status` / web UI).
    if pointer.get("empty_cycles") != empty_cycles:
        pointer["empty_cycles"] = empty_cycles
        try:
            goal.save_pointer(cfg, pointer)
        except OSError:
            pass

    if empty_cycles < threshold:
        return

    halt_disabled = _ideation_halt_disabled(cfg)
    if halt_disabled:
        # Detector tripped but the operator killed auto-halt. Surface
        # as a decisions-needed bullet (one per tick attempt —
        # acceptable noise floor; the operator is expected to respond
        # promptly to a kill-switched halt criteria).
        try:
            _append_decisions_needed_bullet(
                cfg,
                (
                    f"Ideation-halt auto-park is disabled "
                    f"(`AP2_IDEATION_HALT_DISABLED=1`) but ideation "
                    f"has run {empty_cycles} consecutive empty cycles "
                    f"(threshold {threshold}). Halt ideation manually "
                    f"by editing `goal.md` via `ap2 update-goal` (the "
                    f"goal_updated event resets the counter), or unset "
                    f"the kill-switch to let the daemon halt "
                    f"automatically."
                ),
            )
        except OSError:
            pass
        return

    # Threshold reached: emit `roadmap_complete` once. Suppress
    # re-emission via the pointer's `roadmap_complete_emitted` flag.
    # The drain-side `update_goal` handler clears the flag via
    # `goal.reset_pointer_on_goal_updated` so a future re-exhaustion
    # (operator extends goal.md → fresh cycles → counter retrips) emits
    # cleanly.
    if pointer.get("roadmap_complete_emitted"):
        return

    # TB-275: ideation is parked (`_maybe_ideate` skips with
    # `reason=roadmap_complete`) but task dispatch is NOT affected.
    # Already-queued Backlog tasks continue to auto-promote and
    # dispatch normally.
    #
    # TB-302: the daemon no longer appends a `Roadmap complete: ...`
    # bullet to `.cc-autopilot/ideation_state.md` here. The signal is
    # already surfaced redundantly via (a) this `roadmap_complete`
    # event in `events.jsonl`, (b) `focus_pointer.json`
    # (`roadmap_complete_emitted=true`), (c) `ap2 status`'s focus line
    # (`focus: parked — ideation exhausted; ...`), and (d) the TB-244
    # status-report digest. Post-fix `ideation_state.md` is
    # single-writer (only the ideation agent writes it via
    # `ideation_state_write`).
    events.append(
        cfg.events_file,
        "roadmap_complete",
        exhausted_count=len(foci),
        trigger="empty_cycles_heuristic",
    )
    pointer["roadmap_complete_emitted"] = True
    # TB-340 (core stale-state fix): clear the dismissal marker so each
    # fresh exhaustion episode re-arms the operator nag exactly once —
    # even if a PRIOR episode at the same foci count was dismissed via
    # `ap2 ack roadmap_complete`. The marker (`roadmap_complete_ack_idx`)
    # is read only by `goal.roadmap_complete_notice_dismissed`; resetting
    # it here makes that single field authoritative and removes the
    # stale-ack ambiguity that let ideation auto-resume after an
    # extend→re-exhaust cycle with no operator action.
    pointer["roadmap_complete_ack_idx"] = None
    try:
        goal.save_pointer(cfg, pointer)
    except OSError:
        pass


# ============================================================================
# Tick-hook wrappers (TB-391) — the registry-walked entry points.
# ============================================================================


def _resolve_mcp_server():
    """Best-effort fetch of the daemon's MCP server reference.

    The `Phase.IDEATION` tick hook signature is the uniform `(cfg, sdk)`
    every registry tick hook uses, but `_run_ideation` needs the
    daemon's `mcp_server` to dispatch the control sub-agent. The daemon
    stashes `(sdk, mcp_server)` on `status_report._SDK_REF` at startup
    (`status_report.configure(...)`, called from `main_loop`); we read
    `mcp_server` from there — the same process-wide singleton the cron
    scheduler's `_resolve_mcp_server` consumes. Returns `None` when
    `configure(...)` hasn't run (no daemon — e.g. a unit test driving
    the gate directly), which is the same null `mcp_server` the gate
    tolerates (tests pass `mcp_server=None` explicitly).
    """
    from ap2 import status_report as _status_report_mod

    return _status_report_mod._SDK_REF.get("mcp_server")


async def run_ideation_tick(cfg: Config, sdk) -> None:
    """Natural empty-board ideation tick hook (TB-391 — `Phase.IDEATION`).

    The behavior-preserving replacement for `daemon._tick`'s inline
    `ideation._maybe_ideate(...)` call. Self-gates on the
    `AP2_IDEATION_DISABLED` kill switch (resolved via
    `Manifest.is_enabled()` in `ap2/registry.py`, matching the cron
    family convention where the daemon walks every tick hook regardless
    of env_flag and the hook self-gates). Resolves the daemon's
    `mcp_server` from the `status_report.configure(...)` singleton (it
    can't ride the uniform `(cfg, sdk)` tick-hook signature).

    Routes the call back through the core `ap2.ideation._maybe_ideate`
    module attribute (not the local body) so the daemon-tick test
    contract `monkeypatch.setattr(ideation, "_maybe_ideate", noop)`
    keeps silencing ideation while another stage is exercised.
    """
    if not default_registry().get(COMPONENT_NAME).is_enabled():
        return
    mcp_server = _resolve_mcp_server()
    await _ideation_core._maybe_ideate(cfg, sdk, mcp_server)


async def run_force_ideate(cfg: Config, sdk, mcp_server) -> None:
    """Operator-forced ideation hook-point (TB-159 / TB-391).

    Resolved by `daemon._tick` via
    `default_registry().get("ideation").hook_points["force_ideate"]`
    when the operator-queue drain sets `force_ideate=True`. Runs FIRST
    (before the natural `Phase.IDEATION` walk) so the natural path's
    `mark_run` doesn't reset the cooldown out from under it. Routes
    through `ap2.ideation.force_ideate` so the monkeypatch test contract
    holds.
    """
    await _ideation_core.force_ideate(cfg, sdk, mcp_server)


def run_ideation_halt(cfg: Config, sdk) -> None:
    """Roadmap-exhaustion halt tick hook (TB-391 — `Phase.PRE_DISPATCH`).

    The behavior-preserving replacement for `daemon._tick`'s inline
    step-0.6 `ideation_halt.maybe_halt_on_exhaustion(cfg)` call. Runs in
    the PRE_DISPATCH walk; name-sorted component order puts it after
    `auto_approve` / `auto_unfreeze` and before the cron stage — exactly
    where the inline call fired (after the PRE_DISPATCH walk, before
    cron) so the freshly-set pointer / `roadmap_complete` event is
    visible to every later stage (incl. a status-report cron) on this
    tick.

    Does NOT self-gate on `AP2_IDEATION_DISABLED` — the halt always runs
    (only `AP2_IDEATION_HALT_DISABLED` suppresses the auto-halt), so a
    `AP2_IDEATION_DISABLED=1` operator who silenced empty-board ideation
    still gets the roadmap-exhaustion detector, preserving the pre-TB-391
    core-lifecycle semantics bit-for-bit. Self-handles its own exception
    surface with the same stderr line the inline step-0.6 try/except
    emitted (the PRE_DISPATCH walk has no outer try/except per the
    tick-hook protocol). Routes through `ap2.ideation_halt` so the
    daemon-tick test contract `monkeypatch.setattr(ideation_halt,
    "maybe_halt_on_exhaustion", ...)` keeps controlling it.
    """
    try:
        _ideation_halt_core.maybe_halt_on_exhaustion(cfg)
    except Exception as e:  # noqa: BLE001
        print(
            f"[ap2] maybe_halt_on_exhaustion error: {type(e).__name__}: {e}",
            file=sys.stderr,
        )

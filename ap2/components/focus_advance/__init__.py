"""Ideation-exhaustion detector (collapsed from focus-list rotation).

TB-342 collapsed the multi-focus rotation state machine down to a
single ideation-exhaustion detector. The directory name
(`focus_advance`), the env knob names
(`AP2_FOCUS_ADVANCE_EMPTY_CYCLES`, `AP2_FOCUS_AUTO_ADVANCE_DISABLED`),
the `roadmap_complete` event name, and the component manifest's
`env_flag` wiring are preserved verbatim to bound blast radius
(rename to `ideation_halt` is a cosmetic follow-up — see the TB-342
Out-of-scope list). What changed: the focus-index advance loop, the
active-title sync, the `focus_advanced` rotation emission, and the
pointer-past-last branch are gone. `_maybe_advance_focus` now counts
consecutive empty ideation cycles since the most recent `goal_updated`
event and emits `roadmap_complete` once when the count reaches
`AP2_FOCUS_ADVANCE_EMPTY_CYCLES`.

Why the collapse landed: ideation never scoped itself to the
"active" focus (`ap2/ideation.py` never read the pre-TB-342 rotation
pointer fields, the prompt was never told which focus was active,
and the goal-anchor validator accepts ANY `## Current focus:`
heading). Advancing the pointer from focus 1 → focus 2 changed
nothing about what ideation proposed — the rotation was theatre.
The one load-bearing piece of the old `focus_advance` was the
empty-cycles → exhaustion detector, which now stands on its own
without the rotation scaffolding. The observable delta: exhaustion
is detected in `threshold` empty cycles instead of
`num_foci × threshold` — strictly less wasted spend, same end state.

Multi-focus headings in `goal.md` remain expressive but unmechanized:
the operator can still list several `## Current focus:` headings in
priority order as prose/intent for the ideation agent (which reads the
whole goal file every cycle); the goal-anchor validator accepts any
heading. The daemon does not sequence them.

The exit cycle counter is reset by:
  - `goal_updated` (operator extended/edited goal.md → fresh runway).
  - `ideation_proposal_recorded` inside a cycle → cycle's exit zeroes
    the counter (a fresh proposal landed; ideation is still productive).

Lifecycle:
  - `_maybe_advance_focus(cfg, sdk)` is called once per tick from the
    PRE_DISPATCH tick hook. It reads goal.md's focus list, the recent
    events tail, the pointer state, and either: (a) no-op when below
    threshold; (b) emits `roadmap_complete` once + sets
    `roadmap_complete_emitted=True` + clears the dismissal marker; or
    (c) when the `AP2_FOCUS_AUTO_ADVANCE_DISABLED` kill-switch is set,
    surfaces a decisions-needed bullet instead of auto-halting.
  - `goal.reset_pointer_on_goal_updated(cfg, foci)` is called by the
    operator-queue `update_goal` drain handler; it resets the empty-
    cycles counter, clears `roadmap_complete_emitted`, and clears the
    dismissal marker so a goal-file edit resumes ideation cleanly.

Goal.md itself is NEVER mutated (goal.md L187-191 Non-goal). The
pointer file lives at `.cc-autopilot/focus_pointer.json`; it's both
fenced from task agents (`TASK_AGENT_FENCED_PATHS`) and gitignored so
rollbacks don't re-fire stale `roadmap_complete` events.

TB-302: the daemon does not append a `Roadmap complete: ...` bullet
to `.cc-autopilot/ideation_state.md` on the halt branch — that
side-channel was the priming-leak + uncommitted-state-drift surface
TB-302 closed. The kill-switch branch further down still writes a
decisions-needed bullet because operator-killed-but-criteria-met has
no naturally-observable focus-line surface; that's the one remaining
caller of `_append_decisions_needed_bullet` in this module.

Kill-switch: `AP2_FOCUS_AUTO_ADVANCE_DISABLED=1` (or
`[components.focus_advance] auto_advance_disabled = true` post-TB-329
TOML overlay) disables the auto-halt: when set, the detector still
counts but does not emit the halt; the operator halts manually after
reviewing the decisions-needed bullet. The env-knob name + the
component manifest's `env_flag` wiring (TB-320) are preserved
verbatim across TB-342.

TB-329 axis-5: the two operator-tunable knobs route through
`cfg.get_component_value("focus_advance", "auto_advance_disabled")` /
`cfg.get_component_value("focus_advance", "empty_cycles")` (via the
intra-package `_focus_auto_advance_disabled(cfg)` /
`_advance_empty_cycles_threshold(cfg)` helpers below). See the
manifest's TB-329 doc block for the chosen access-shape rationale and
the FLAT_TO_SECTIONED latent-bug fix that landed alongside.
"""
from __future__ import annotations

from ap2 import events, goal
from ap2.components.auto_approve import _append_decisions_needed_bullet
from ap2.config import Config


_FOCUS_RECENT_TAIL_N = 200


def _focus_auto_advance_disabled(cfg: Config) -> bool:
    """TB-329 axis-5: True iff the focus auto-advance kill switch is
    set to a truthy value.

    Resolution shape (mirrors the TB-326 pilot template + TB-327 /
    TB-328 cluster siblings): routes through
    `cfg.get_component_value("focus_advance", "auto_advance_disabled")`,
    which evaluates sectioned env (the
    `f"AP2_COMPONENTS_{component.upper()}_{key.upper()}"` shape built
    inside the helper) > flat env (`AP2_FOCUS_AUTO_ADVANCE_DISABLED`
    via the `FLAT_TO_SECTIONED` reverse-lookup) > `cfg.components_config`
    snapshot > default at call time. Call-time env-first precedence
    preserves the pre-TB-329 `goal.auto_advance_disabled()` lazy-read
    pattern — `monkeypatch.setenv(...)` plus a subsequent helper call
    picks up the new value without rebuilding cfg.

    Same truthy enumeration as `goal.auto_advance_disabled()`
    (`"1"` / `"true"` / `"yes"` / `"on"`, case-insensitive). The TOML
    layer's typed `True` / `False` is also honored so an operator who
    opts into `[components.focus_advance] auto_advance_disabled = true`
    gets the same behavior the shell-export operator does.

    Default unset → False (auto-halt enabled), bit-for-bit identical
    to the pre-TB-329 env-only behavior.
    """
    raw = cfg.get_component_value("focus_advance", "auto_advance_disabled")
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return False
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _advance_empty_cycles_threshold(cfg: Config) -> int:
    """TB-329 axis-5: effective threshold for the empty-cycles
    heuristic (TB-283 / TB-292 / TB-342 — the sole signal now drives
    the ideation-exhaustion halt rather than the deleted multi-focus
    advance loop).

    Resolution shape (mirrors the TB-326 pilot template + TB-327 /
    TB-328 cluster siblings): routes through
    `cfg.get_component_value("focus_advance", "empty_cycles")`, which
    evaluates sectioned env (the
    `f"AP2_COMPONENTS_{component.upper()}_{key.upper()}"` shape built
    inside the helper) > flat env (`AP2_FOCUS_ADVANCE_EMPTY_CYCLES`
    via the `FLAT_TO_SECTIONED` reverse-lookup) > `cfg.components_config`
    snapshot > default at call time. The flat env name keeps today's
    behavior bit-for-bit for the shell-export operator who never
    migrated their `.cc-autopilot/env`.

    Permissive parse (`goal.advance_empty_cycles_threshold()` parity):
    empty / non-int / whitespace-only values fall back to the default
    (`goal.ADVANCE_EMPTY_CYCLES_DEFAULT` = 3); out-of-range values clamp
    to [`goal.ADVANCE_EMPTY_CYCLES_MIN`, `goal.ADVANCE_EMPTY_CYCLES_MAX`]
    (= [1, 20]) so an operator typo (e.g. `0` or `999999`) doesn't
    disable the halt path or wedge it permanently. The clamp constants
    live in `ap2/goal.py` and are referenced here via late import so
    the parser bounds stay single-source-of-truth.
    """
    raw = cfg.get_component_value("focus_advance", "empty_cycles")
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


def _ideation_empty_against_focus(tail: list[dict]) -> int:
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
    increment counter (one cycle = +2 because both `ideation_empty_board`
    and `ideation_complete` counted independently; one productive
    cycle netted +1 because the reset only zeroed between increments)
    to the cycle-grouped semantic that the `AP2_FOCUS_ADVANCE_EMPTY_CYCLES`
    env-knob name advertises ("3 consecutive empty cycles to trip").
    TB-300 then extended the exit-marker set from `ideation_complete`
    alone to also include `ideation_cycle_summary`: the agent emits
    the latter (not the former) on every 0-proposal cycle, so under
    the prior single-name predicate every natural empty cycle was
    invisible to the counter and the threshold was structurally
    unreachable. TB-342 changed the reset cutoff from the (now-deleted)
    `focus_advanced to=<focus_title>` event to `goal_updated` — the
    counter no longer scopes to a focus title because there is no
    active focus (the pointer doesn't walk anymore); the natural reset
    signal for the global empty-cycles run is "operator extended /
    edited the goal," which `update_goal` emits as `goal_updated`.
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


async def _maybe_advance_focus(cfg: Config, sdk) -> None:
    """Ideation-exhaustion detector (TB-342, collapsed from the
    pre-existing focus-list rotation pass).

    Reads goal.md's focus list + the pointer state file. Counts the
    consecutive recent ideation cycles that produced 0 proposals since
    the most recent `goal_updated`. When the count reaches
    `AP2_FOCUS_ADVANCE_EMPTY_CYCLES`, emits `roadmap_complete` once
    (and sets the pointer's `roadmap_complete_emitted` flag), parking
    the ideation trigger until the operator extends goal.md (via
    `ap2 update-goal` — the operator-queue drain handler calls
    `goal.reset_pointer_on_goal_updated` to clear the halt) or fires
    `ap2 ideate --force`. TB-275: task dispatch is NOT affected — only
    the ideation trigger.

    The kill-switch (`AP2_FOCUS_AUTO_ADVANCE_DISABLED=1`) disables the
    auto-halt: the detector still counts but does not emit
    `roadmap_complete`; instead surfaces the existing
    decisions-needed bullet so the operator can halt manually (e.g.
    by editing goal.md). Preserves the pre-TB-342 env-knob name + the
    component manifest's `env_flag` wiring verbatim (per the briefing's
    blast-radius bound — a rename of the knob namespace is a
    follow-up).

    Pure / side-effect-bounded: writes events + the pointer file +
    (rarely, only on the kill-switch path) one decisions-needed
    bullet. Does NOT mutate goal.md itself. Tolerates a missing
    goal.md / empty focus list gracefully (early return).

    The `sdk` parameter is vestigial (no SDK calls remain inside this
    pass — the pre-TB-283 LLM-judge advance path was deleted) but is
    retained so callers and the test harness can keep passing it
    without ceremony.

    TB-302: the roadmap-complete branch does not append a
    `Roadmap complete: ...` bullet to `.cc-autopilot/ideation_state.md`
    — the pointer-driven `ap2 status` focus line is the canonical
    operator-facing surface (see TB-302 for the redundant-signal audit
    and the two bugs the bullet write caused; this module no longer
    appends `Roadmap complete:` bullets). The kill-switch branch
    further down still writes a decisions-needed bullet because
    operator-killed-but-criteria-met has no equivalent naturally-
    observable focus-line surface.
    """
    foci = goal.read_focus_list(cfg)
    if not foci:
        # Pre-pivot goal.md with no `## Current focus:` headings, or
        # missing goal.md entirely. Nothing to halt against.
        return

    pointer = goal.load_pointer(cfg)

    threshold = _advance_empty_cycles_threshold(cfg)
    tail = events.tail(cfg.events_file, _FOCUS_RECENT_TAIL_N)
    empty_cycles = _ideation_empty_against_focus(tail)
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

    # TB-329 axis-5: cfg-routed reads. See `_focus_auto_advance_disabled`
    # docstring above for the back-compat / TOML-overlay precedence.
    advance_disabled = _focus_auto_advance_disabled(cfg)
    if advance_disabled:
        # Detector tripped but the operator killed auto-halt. Surface
        # as a decisions-needed bullet (one per tick attempt —
        # acceptable noise floor; the operator is expected to respond
        # promptly to a kill-switched halt criteria).
        try:
            _append_decisions_needed_bullet(
                cfg,
                (
                    f"Focus auto-advance is disabled "
                    f"(`AP2_FOCUS_AUTO_ADVANCE_DISABLED=1`) but ideation "
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
    # Already-queued Backlog tasks (operator-added via `ap2 add`,
    # operator-approved via `ap2 approve`, or previously auto-approved
    # by ideation) continue to auto-promote and dispatch normally.
    #
    # TB-302: the daemon no longer appends a `Roadmap complete: ...`
    # bullet to `.cc-autopilot/ideation_state.md` here. The signal is
    # already surfaced redundantly via (a) this `roadmap_complete`
    # event in `events.jsonl`, (b) `focus_pointer.json`
    # (`roadmap_complete_emitted=true`), (c) `ap2 status`'s focus line
    # (`focus: parked — ideation exhausted; ...`), and (d) the TB-244
    # focus-rotation digest in the cron status-report. The bullet was a
    # fifth, daemon-written surface that (1) bypassed the
    # ideation-cycle scrub of exhaustion-asserting sentences and (2)
    # wrote to `ideation_state.md` outside the `_run_ideation` snapshot
    # window. Post-fix `ideation_state.md` is single-writer (only the
    # ideation agent writes it via `ideation_state_write`).
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

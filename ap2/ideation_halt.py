"""Ideation-exhaustion halt — core ideation lifecycle (TB-345).

TB-342 collapsed the multi-focus rotation state machine down to a
single ideation-exhaustion detector; TB-345 then merged that residual
detector out of the `focus_advance` component and into this core
module, renaming the entry point + the two operator knobs to the
`ideation_halt` namespace. The detector is **core ideation
lifecycle**, not an optional component: it always runs, it's essential
(without it ideation burns SDK cycles forever against an exhausted
goal), and its output (`roadmap_complete`) is consumed by the ideation
trigger / `ap2 status` / web surfaces via `goal.roadmap_exhausted`.

What the detector does: `maybe_halt_on_exhaustion(cfg)` counts
consecutive empty ideation cycles since the most recent `goal_updated`
event and emits `roadmap_complete` once when the count reaches
`AP2_IDEATION_HALT_EMPTY_CYCLES` (default 3), parking the ideation
trigger until the operator extends goal.md (via `ap2 update-goal` —
the operator-queue drain handler calls
`goal.reset_pointer_on_goal_updated` to clear the halt) or fires
`ap2 ideate --force`. When the `AP2_IDEATION_HALT_DISABLED` kill switch
is set, the detector still counts but does not emit the halt; instead
it surfaces a decisions-needed bullet so the operator halts manually.

Why the collapse landed (TB-342): ideation never scoped itself to the
"active" focus — the prompt was never told which focus was active and
the goal-anchor validator accepts ANY `## Current focus:` heading.
Advancing the pointer from focus 1 → focus 2 changed nothing about
what ideation proposed; the rotation was theatre. The one load-bearing
piece was the empty-cycles → exhaustion detector, which now stands on
its own. The observable delta vs. the old rotation: exhaustion is
detected in `threshold` empty cycles instead of `num_foci × threshold`
— strictly less wasted spend, same end state.

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
  - `maybe_halt_on_exhaustion(cfg)` is called once per tick from the
    daemon's PRE_DISPATCH phase (directly, not via a registry hook).
    It reads goal.md's focus list, the recent events tail, the pointer
    state, and either: (a) no-op when below threshold; (b) emits
    `roadmap_complete` once + sets `roadmap_complete_emitted=True` +
    clears the dismissal marker; or (c) when the
    `AP2_IDEATION_HALT_DISABLED` kill switch is set, surfaces a
    decisions-needed bullet instead of auto-halting.
  - `goal.reset_pointer_on_goal_updated(cfg, foci)` is called by the
    operator-queue `update_goal` drain handler; it resets the empty-
    cycles counter, clears `roadmap_complete_emitted`, and clears the
    dismissal marker so a goal-file edit resumes ideation cleanly.

Goal.md itself is NEVER mutated (goal.md Non-goal). The pointer file
lives at `.cc-autopilot/focus_pointer.json`; it's both fenced from
task agents (`TASK_AGENT_FENCED_PATHS`) and gitignored so rollbacks
don't re-fire stale `roadmap_complete` events.

TB-302: the daemon does not append a `Roadmap complete: ...` bullet to
`.cc-autopilot/ideation_state.md` on the halt branch — that
side-channel was the priming-leak + uncommitted-state-drift surface
TB-302 closed. The kill-switch branch further down still writes a
decisions-needed bullet because operator-killed-but-criteria-met has
no naturally-observable focus-line surface; that's the one remaining
caller of `_append_decisions_needed_bullet` in this module.

Knob resolution (TB-345): the two operator-tunable knobs are read via
`cfg.get_core_value("ideation_halt_empty_cycles")` /
`cfg.get_core_value("ideation_halt_disabled")` — the core-cluster
sibling of the per-component `get_component_value` helper. Resolution
is call-time env-first: the sectioned-env form >
flat env (`AP2_IDEATION_HALT_EMPTY_CYCLES` /
`AP2_IDEATION_HALT_DISABLED`, plus the deprecated focus-era
aliases that `config_compat.FLAT_TO_SECTIONED` still maps to the same
`core.ideation_halt_*` paths) > `cfg.core_config` snapshot > the
`core_config_schema.CORE_CONFIG_SCHEMA` default. The deprecated
aliases keep a stale operator env working for one release.
"""
from __future__ import annotations

from ap2 import events, goal
from ap2.config import Config


_RECENT_TAIL_N = 200


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

    This core module must NOT statically import from `ap2.components`
    (the TB-311 import-direction gate AST-walks every import node,
    including late imports inside a function body). The registry's
    `hook_points` dict is the sanctioned cross-reference path
    (goal.md L57-59: "All cross-references flow through the registry's
    hook protocol"). `ap2.registry` is core, so importing it here is
    allowed; the registry then dynamically loads the component.
    """
    from ap2.registry import default_registry

    writer = default_registry().get("auto_approve").hook_points[
        "_append_decisions_needed_bullet"
    ]
    writer(cfg, text)


def maybe_halt_on_exhaustion(cfg: Config) -> None:
    """Ideation-exhaustion detector (TB-345, merged from the
    `focus_advance` component's residual detector).

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

"""Focus-list pointer advance (TB-226 axis 4).

Lifted from `ap2/daemon.py` as part of TB-263's responsibility split. The
orchestrator (`_tick`) calls `_maybe_advance_focus` once per tick; this
module owns the pointer-advance policy itself:

  - `_ideation_empty_against_focus`: cycle-grouped counter for the
    heuristic "N consecutive 0-proposal cycles against the active
    focus" path. Each ideation cycle is bounded by
    `ideation_empty_board` (entry) and one of `ideation_complete` /
    `ideation_timeout` / `ideation_error` (exit); per-cycle accounting
    avoids the pre-TB-292 double-count where one cycle bumped the
    counter by 2 (entry + exit events both counted) and one productive
    cycle netted +1 (reset zeroed only between the two increments).
  - `_maybe_advance_focus`: the orchestrator entry point. Reads goal.md's
    focus list + `focus_pointer.json`, advances the in-memory pointer
    when criteria are met, emits `roadmap_complete` when all foci are
    exhausted.

Reads goal.md's multi-`## Current focus:` heading list + the runtime
pointer (`focus_pointer.json`). TB-283: the empty-cycles heuristic is
the sole advance signal — a focus advances after
`AP2_FOCUS_ADVANCE_EMPTY_CYCLES` (default 3) consecutive ideation
cycles produce zero proposals against it (the empty-board signal).
The prior LLM-judge path against operator-authored bullets was
deleted because the judge ruled on commit diffs of code the running
daemon had never executed, collapsing multi-week foci into ~3-task
cycles whenever each task commit-satisfied one shape-shaped bullet.
TB-285 renamed the per-focus sub-block from `Done when:` to
`Progress signals:` to reflect the new advisory semantics — the
bullets remain in goal.md as ideation-prompt context but no longer
gate advancement.

When all foci exhaust, emit `roadmap_complete` (once) + a
`## Decisions needed from operator` bullet so `ap2 status` and the web
home page surface the parked-ideation state. TB-275: this is an
ideation-trigger gate only — `_maybe_ideate` skips with
`reason=roadmap_complete` until the operator extends the roadmap
(`ap2 update-goal`) or dismisses the notice (`ap2 ack
roadmap_complete`). Task dispatch is NOT affected; already-queued
Backlog tasks continue to drain. Use `ap2 pause` for an explicit
full-stop.

Goal.md itself is NEVER mutated (goal.md L187-191 Non-goal). The
pointer file lives at `.cc-autopilot/focus_pointer.json`; it's both
fenced from task agents (TASK_AGENT_FENCED_PATHS) and gitignored so
rollbacks don't re-fire stale `focus_advanced` events.

Kill-switch: `AP2_FOCUS_AUTO_ADVANCE_DISABLED=1` short-circuits the
advance attempt even when criteria are met. The daemon surfaces a
`## Decisions needed from operator` bullet instead so the operator
can advance manually via `ap2 update-goal`.
"""
from __future__ import annotations

from . import events, goal
from .auto_approve import _append_decisions_needed_bullet
from .config import Config


_FOCUS_RECENT_TAIL_N = 200


def _ideation_empty_against_focus(tail: list[dict], focus_title: str) -> int:
    """Count consecutive recent ideation cycles that exited without
    recording a proposal against `focus_title`. Cycle-grouped: each
    ideation cycle is bounded by `ideation_empty_board` (daemon-emitted
    entry marker at `ideation._run_ideation`) and one of
    `ideation_complete` / `ideation_timeout` / `ideation_error` (exit).
    Per cycle:

      - Exited via `ideation_complete` AND no `ideation_proposal_recorded`
        fired within the cycle → increment count by 1.
      - Any `ideation_proposal_recorded` fired within the cycle → on
        `ideation_complete`, reset count to 0 (a fresh proposal landed
        against the active focus; the focus isn't exhausted).
      - Exited via `ideation_timeout` / `ideation_error` → leave count
        unchanged. These are infrastructure failures (SDK budget
        exhausted, agent crash) — not "ideation reasoned and found
        nothing." Treating them as empty would let transient SDK
        slowness or a network blip falsely trip focus advance.

    Events older than the most recent `focus_advanced to=<focus_title>`
    are ignored (the prior focus's cycles don't count against the new
    active focus's freshness). Truncated cycles (events appearing
    after the cutoff without their matching `ideation_empty_board`
    entry marker, or a cycle whose exit marker fell off the tail) are
    handled cleanly via the `in_cycle` flag — orphan proposal/exit
    events outside any cycle are ignored, and a fresh `ideation_empty_board`
    resets the flags without spurious increments.

    TB-292 restructured this from the prior event-walking flat-
    increment counter (one cycle = +2 because both `ideation_empty_board`
    and `ideation_complete` counted independently; one productive
    cycle netted +1 because the reset only zeroed between increments)
    to the cycle-grouped semantic that the `AP2_FOCUS_ADVANCE_EMPTY_CYCLES`
    env-knob name advertises ("3 consecutive empty cycles to trip").
    """
    # Reset cutoff: the most recent `focus_advanced to=<focus_title>`
    # event marks the start of the current focus's window.
    cutoff_idx = -1
    for i, e in enumerate(tail):
        if e.get("type") == "focus_advanced" and str(e.get("to") or "") == focus_title:
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
        elif typ == "ideation_complete" and in_cycle:
            count = 0 if cycle_had_proposal else count + 1
            in_cycle = False
        elif typ in ("ideation_timeout", "ideation_error") and in_cycle:
            # Infrastructure failure: don't count, don't reset.
            in_cycle = False
    return count


async def _maybe_advance_focus(cfg: Config, sdk) -> None:
    """Focus-list advance pass (TB-226 axis 4).

    Reads goal.md's focus list + the pointer state file. If the active
    focus is exhausted, advance to the next; if all foci are exhausted,
    emit `roadmap_complete` + a decisions-needed bullet (once) so the
    ideation-trigger gate (`_maybe_ideate` in `ap2/ideation.py`) parks
    on subsequent ticks until the operator extends the roadmap + acks.
    TB-275: task dispatch is NOT affected — only the ideation trigger.

    Pure / side-effect-bounded: writes events + the pointer file +
    (rarely) one decisions-needed bullet. Does NOT mutate goal.md
    itself. Tolerates a missing goal.md / empty focus list gracefully
    (early return; the daemon's other gates handle the pre-focus-list
    state).

    TB-283: the empty-cycles heuristic is the sole advance signal —
    used for every focus regardless of whether it carries a
    `Progress signals:` sub-block (TB-285 rename of the prior
    `Done when:` block). The prior LLM-judge path that ruled on
    operator-authored bullets was deleted because it collapsed to
    "did the last N task commits look goal-shaped?", a diff-reading
    proxy the running daemon could not verify behaviorally; foci kept
    collapsing into ~3-task cycles whenever each task commit-
    satisfied one shape-shaped bullet. The `sdk` parameter is now
    vestigial (no SDK calls remain inside the advance pass) but is
    retained so callers and the test harness can keep passing it
    without ceremony.
    """
    foci = goal.read_focus_list(cfg)
    if not foci:
        # Pre-pivot goal.md with no `## Current focus:` headings, or
        # missing goal.md entirely. Nothing to advance against.
        return

    pointer = goal.load_pointer(cfg)
    active_idx = pointer["active_index"]

    if active_idx >= len(foci):
        # Pointer already past the last focus.
        if not pointer.get("roadmap_complete_emitted"):
            # First detection of exhaustion → emit the audit event +
            # decisions-needed bullet. Subsequent ticks short-circuit
            # here. TB-275: the bullet is purely informational — the
            # ideation trigger is parked (`_maybe_ideate` skips with
            # `reason=roadmap_complete`) but task dispatch is NOT
            # affected. Already-queued Backlog tasks (operator-added
            # via `ap2 add`, operator-approved via `ap2 approve`, or
            # previously auto-approved by ideation) continue to
            # auto-promote and dispatch normally.
            events.append(
                cfg.events_file,
                "roadmap_complete",
                exhausted_count=len(foci),
                trigger="pointer_past_last",
            )
            try:
                _append_decisions_needed_bullet(
                    cfg,
                    (
                        f"Roadmap complete: all {len(foci)} `## Current "
                        f"focus:` heading(s) in `goal.md` are exhausted. "
                        f"Ideation is parked (no active focus); extend "
                        f"the roadmap (add new `## Current focus:` "
                        f"headings via `ap2 update-goal`) to resume "
                        f"ideation, or `ap2 ack roadmap_complete` to "
                        f"dismiss this notice. Task dispatch is NOT "
                        f"affected — already-queued Backlog tasks "
                        f"continue to drain. Use `ap2 pause` for a "
                        f"full stop."
                    ),
                )
            except OSError:
                pass
            pointer["roadmap_complete_emitted"] = True
            try:
                goal.save_pointer(cfg, pointer)
            except OSError:
                pass
        return

    # Active focus is in-bounds. Sync `active_title` (cheap forward-
    # compat: a hand-edited pointer with a stale title gets corrected
    # without bouncing the pointer).
    active = foci[active_idx]
    if pointer.get("active_title") != active.title:
        pointer["active_title"] = active.title
        try:
            goal.save_pointer(cfg, pointer)
        except OSError:
            pass

    # Kill-switch: even if criteria would advance, do NOT advance —
    # surface a decisions-needed bullet so the operator advances
    # manually. Idempotent via the bullet's prefix (we don't dedup;
    # the operator-decisions reader handles repeated bullets fine —
    # same shape TB-225 uses for per_day_cap halts).
    advance_disabled = goal.auto_advance_disabled()

    advance_trigger: str | None = None

    # TB-283: empty-cycles is the sole advance signal — runs for every
    # focus regardless of whether it carries a `Progress signals:`
    # sub-block (TB-285 rename of the prior `Done when:` block).
    # Count consecutive ideation cycles that produced 0 proposals
    # against the active focus.
    threshold = goal.advance_empty_cycles_threshold()
    tail = events.tail(cfg.events_file, _FOCUS_RECENT_TAIL_N)
    empty_cycles = _ideation_empty_against_focus(tail, active.title)
    # Keep the pointer's empty_cycles field in sync (forensic /
    # observability surface for `ap2 status` / web UI).
    if pointer.get("empty_cycles") != empty_cycles:
        pointer["empty_cycles"] = empty_cycles
        try:
            goal.save_pointer(cfg, pointer)
        except OSError:
            pass
    if empty_cycles >= threshold:
        advance_trigger = "empty_cycles_heuristic"

    if advance_trigger is None:
        return

    if advance_disabled:
        # Criteria are met but the operator killed auto-advance.
        # Surface as a decisions-needed bullet (one per tick attempt
        # — acceptable noise floor; the operator is expected to
        # respond promptly to a kill-switched advance).
        try:
            _append_decisions_needed_bullet(
                cfg,
                (
                    f"Focus auto-advance is disabled "
                    f"(`AP2_FOCUS_AUTO_ADVANCE_DISABLED=1`) but the "
                    f"active focus `{active.title}` would advance via "
                    f"`{advance_trigger}`. Advance manually by editing "
                    f"`goal.md` via `ap2 update-goal`, or unset the "
                    f"kill-switch to let the daemon advance "
                    f"automatically."
                ),
            )
        except OSError:
            pass
        return

    # Advance: move pointer to the next focus. Bookkeeping bumps
    # `exhausted_titles` so the operator-CLI surface can render the
    # full advance history without a separate event-log walk.
    old_title = active.title
    new_idx = active_idx + 1
    new_title = foci[new_idx].title if new_idx < len(foci) else ""
    exhausted = list(pointer.get("exhausted_titles") or [])
    if old_title and old_title not in exhausted:
        exhausted.append(old_title)
    pointer["active_index"] = new_idx
    pointer["active_title"] = new_title
    pointer["empty_cycles"] = 0
    pointer["exhausted_titles"] = exhausted
    # Reset `roadmap_complete_emitted` so a future re-exhaustion (e.g.
    # operator extends the roadmap → advance to a new focus → that
    # one also exhausts → fresh `roadmap_complete` event) re-fires
    # cleanly.
    pointer["roadmap_complete_emitted"] = False
    try:
        goal.save_pointer(cfg, pointer)
    except OSError:
        pass
    events.append(
        cfg.events_file,
        "focus_advanced",
        **{"from": old_title, "to": new_title},
        trigger=advance_trigger,
        new_index=new_idx,
        total_foci=len(foci),
    )

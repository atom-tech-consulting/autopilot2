"""Focus-list pointer advance (TB-226 axis 4).

Lifted from `ap2/daemon.py` as part of TB-263's responsibility split. The
orchestrator (`_tick`) calls `_maybe_advance_focus` once per tick; this
module owns the pointer-advance policy itself:

  - `_ideation_empty_against_focus`: cycle-grouped counter for the
    heuristic "N consecutive 0-proposal cycles against the active
    focus" path. Each ideation cycle is bounded by
    `ideation_empty_board` (entry) and one of `ideation_complete` /
    `ideation_cycle_summary` / `ideation_timeout` / `ideation_error`
    (exit); per-cycle accounting avoids the pre-TB-292 double-count
    where one cycle bumped the counter by 2 (entry + exit events both
    counted) and one productive cycle netted +1 (reset zeroed only
    between the two increments). TB-300 added `ideation_cycle_summary`
    to the exit-marker set: the agent emits it (not `ideation_complete`)
    on the 0-proposal path, so under the prior single-name predicate
    every natural empty cycle slipped past the counter and the
    `AP2_FOCUS_ADVANCE_EMPTY_CYCLES` threshold was structurally
    unreachable.
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

When all foci exhaust, emit `roadmap_complete` (once) and set the
pointer's `roadmap_complete_emitted` flag so `ap2 status`'s focus
line (`focus: ROADMAP_COMPLETE — ideation parked; `ap2 update-goal`
to resume or `ap2 ack roadmap_complete` to dismiss`, derived from
`focus_pointer.json` via `goal.roadmap_exhausted`) and the web home
page surface the parked-ideation state. TB-275: this is an
ideation-trigger gate only — `_maybe_ideate` skips with
`reason=roadmap_complete` until the operator extends the roadmap
(`ap2 update-goal`) or dismisses the notice (`ap2 ack
roadmap_complete`). Task dispatch is NOT affected; already-queued
Backlog tasks continue to drain. Use `ap2 pause` for an explicit
full-stop.

TB-302: the daemon no longer appends a `Roadmap complete: ...`
bullet to `.cc-autopilot/ideation_state.md` on first detection.
The roadmap-complete signal is redundantly available via the
`roadmap_complete` event in `events.jsonl`, the pointer file, the
`ap2 status` focus line, the web home page, and the TB-244
focus-rotation digest in the cron status-report. The previous
bullet write (1) bypassed the post-cycle scrub of exhaustion-
asserting sentences in `ideation_state.md` (the scrub runs inside
`_run_ideation`; the advance pass runs after it returns), feeding
verdict-language priming back into the next ideation cycle after
the operator extends the roadmap; and (2) wrote to
`ideation_state.md` outside the `_run_ideation` snapshot window,
leaving uncommitted working-tree drift the state-commit pipeline
could not capture. Post-fix `ideation_state.md` is single-writer
(only the ideation agent writes it via `ideation_state_write`).
The `_append_decisions_needed_bullet` helper remains in use for
the kill-switch branch below and for halt-style callers
(`auto_unfreeze.py`'s daily-cap halt, `daemon.py`'s TB-224
task_error halt) that lack a dedicated focus-line surface.

Goal.md itself is NEVER mutated (goal.md L187-191 Non-goal). The
pointer file lives at `.cc-autopilot/focus_pointer.json`; it's both
fenced from task agents (TASK_AGENT_FENCED_PATHS) and gitignored so
rollbacks don't re-fire stale `focus_advanced` events.

Kill-switch: `AP2_FOCUS_AUTO_ADVANCE_DISABLED=1` (or
`[components.focus_advance] auto_advance_disabled = true` post-TB-329
TOML overlay) short-circuits the advance attempt even when criteria
are met. The daemon surfaces a `## Decisions needed from operator`
bullet instead so the operator can advance manually via
`ap2 update-goal`. TB-329 axis-5: the read-site now routes through
`cfg.get_component_value("focus_advance", "auto_advance_disabled")` /
`cfg.get_component_value("focus_advance", "empty_cycles")` (via the
intra-package `_focus_auto_advance_disabled(cfg)` /
`_advance_empty_cycles_threshold(cfg)` helpers below) instead of the
pre-migration `goal.auto_advance_disabled()` /
`goal.advance_empty_cycles_threshold()` env-only path. See the
manifest's TB-329 doc block for the chosen access-shape rationale
and the FLAT_TO_SECTIONED latent-bug fix that landed alongside.
"""
from __future__ import annotations

from ap2 import events, goal
from ap2.components.auto_approve import _append_decisions_needed_bullet
from ap2.config import Config


_FOCUS_RECENT_TAIL_N = 200

# Mirror of the `ap2.goal` parser constants (kept here so the
# `_advance_empty_cycles_threshold(cfg)` helper below can clamp without
# the call chain re-routing through `goal.py`'s env-only helper, which
# is what TB-329 is migrating away from). `goal.ADVANCE_EMPTY_CYCLES_*`
# remain the single source of truth — they're imported lazily inside
# the helper to keep the module-level alias surface stable for the
# `daemon._maybe_advance_focus` re-export contract (TB-313).


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
    (`"1"` / `"true"` / `"yes"` / `"on"`, case-insensitive) so the
    pre-migration `_maybe_advance_focus` kill-switch test pin
    (`test_auto_advance_disabled_short_circuits` in
    `test_tb226_focus_rotation.py`) passes without modification. The
    TOML layer's typed `True` / `False` is also honored so an operator
    who opts into `[components.focus_advance] auto_advance_disabled =
    true` gets the same behavior the shell-export operator does.

    Default unset → False (auto-advance enabled), bit-for-bit
    identical to the pre-TB-329 env-only behavior.
    """
    raw = cfg.get_component_value("focus_advance", "auto_advance_disabled")
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return False
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _advance_empty_cycles_threshold(cfg: Config) -> int:
    """TB-329 axis-5: effective threshold for the empty-cycles
    heuristic advance signal (TB-283 / TB-292).

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
    disable the advance path or wedge it permanently. The clamp
    constants live in `ap2/goal.py` and are referenced here via late
    import so the parser bounds stay single-source-of-truth.
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


def _ideation_empty_against_focus(tail: list[dict], focus_title: str) -> int:
    """Count consecutive recent ideation cycles that exited without
    recording a proposal against `focus_title`. Cycle-grouped: each
    ideation cycle is bounded by `ideation_empty_board` (daemon-emitted
    entry marker at `ideation._run_ideation`) and one of
    `ideation_complete` / `ideation_cycle_summary` / `ideation_timeout`
    / `ideation_error` (exit). The agent's two-event vocabulary is
    intentional: `ideation_complete` carries a proposal summary (used
    when ≥1 proposal landed this cycle), `ideation_cycle_summary`
    carries a no-proposal-reasoning summary (used when 0 proposals
    landed). Either name closes the cycle the same way from the
    counter's perspective.

    Per cycle:

      - Exited via `ideation_complete` OR `ideation_cycle_summary`
        AND no `ideation_proposal_recorded` fired within the cycle →
        increment count by 1.
      - Any `ideation_proposal_recorded` fired within the cycle → on
        either exit marker, reset count to 0 (a fresh proposal landed
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
    TB-300 then extended the exit-marker set from `ideation_complete`
    alone to also include `ideation_cycle_summary`: the agent emits
    the latter (not the former) on every 0-proposal cycle, so under
    the prior single-name predicate every natural empty cycle was
    invisible to the counter and the threshold was structurally
    unreachable — auto-advance never fired regardless of how many
    consecutive 0-proposal cycles ran.
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
    """Focus-list advance pass (TB-226 axis 4).

    Reads goal.md's focus list + the pointer state file. If the active
    focus is exhausted, advance to the next; if all foci are exhausted,
    emit `roadmap_complete` (once) and set the pointer's
    `roadmap_complete_emitted` flag so the ideation-trigger gate
    (`_maybe_ideate` in `ap2/ideation.py`) parks on subsequent ticks
    until the operator extends the roadmap + acks. TB-275: task
    dispatch is NOT affected — only the ideation trigger.

    TB-302: the roadmap-complete branch no longer appends a
    `Roadmap complete: ...` bullet to
    `.cc-autopilot/ideation_state.md` — the pointer-driven
    `ap2 status` focus line is the canonical operator-facing
    surface (see module docstring for the redundant-signal audit and
    the two bugs the bullet write caused). The kill-switch branch
    further down still writes a decisions-needed bullet because
    operator-killed-but-criteria-met has no equivalent
    naturally-observable focus-line surface.

    Pure / side-effect-bounded: writes events + the pointer file +
    (rarely, only on the kill-switch path) one decisions-needed
    bullet. Does NOT mutate goal.md itself. Tolerates a missing
    goal.md / empty focus list gracefully (early return; the daemon's
    other gates handle the pre-focus-list state).

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
            # set the pointer's `roadmap_complete_emitted` flag.
            # Subsequent ticks short-circuit here. TB-275: ideation is
            # parked (`_maybe_ideate` skips with
            # `reason=roadmap_complete`) but task dispatch is NOT
            # affected. Already-queued Backlog tasks (operator-added
            # via `ap2 add`, operator-approved via `ap2 approve`, or
            # previously auto-approved by ideation) continue to
            # auto-promote and dispatch normally.
            #
            # TB-302: the daemon no longer appends a
            # `Roadmap complete: ...` bullet to
            # `.cc-autopilot/ideation_state.md` here. The signal is
            # already surfaced redundantly via (a) this
            # `roadmap_complete` event in `events.jsonl`,
            # (b) `focus_pointer.json` (`active_index past end`,
            # `exhausted_titles`, `roadmap_complete_emitted=true`,
            # empty `active_title`), (c) `ap2 status`'s focus line
            # (`focus: ROADMAP_COMPLETE — ideation parked;
            # `ap2 update-goal` to resume or `ap2 ack
            # roadmap_complete` to dismiss`, derived from the
            # pointer), and (d) the TB-244 focus-rotation digest in
            # the cron status-report. The bullet was a fifth,
            # daemon-written surface that (1) bypassed the
            # ideation-cycle scrub of exhaustion-asserting sentences
            # — feeding verdict-language priming back into the next
            # ideation cycle after operator extends the roadmap —
            # and (2) wrote to `ideation_state.md` outside the
            # `_run_ideation` snapshot window, leaving uncommitted
            # working-tree drift the state-commit pipeline could not
            # capture. Post-fix `ideation_state.md` is single-writer
            # (only the ideation agent writes it via
            # `ideation_state_write`).
            events.append(
                cfg.events_file,
                "roadmap_complete",
                exhausted_count=len(foci),
                trigger="pointer_past_last",
            )
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
    # TB-329 axis-5: cfg-routed reads. The pre-TB-329 path called
    # `goal.auto_advance_disabled()` / `goal.advance_empty_cycles_threshold()`
    # which read `os.environ` directly. The new helpers route through
    # `cfg.get_component_value("focus_advance", <key>)` (see TB-326
    # pilot manifest docstring) so a TOML-opted operator's
    # `[components.focus_advance]` values flow transparently while
    # the shell-export operator keeps the legacy flat env names
    # via the `FLAT_TO_SECTIONED` reverse-lookup back-compat path.
    # The env-only `goal.*` helpers are retained for the
    # `test_tb226_focus_rotation.py` unit pins (per the briefing's
    # "tests pass without modification" contract); only this call
    # site swaps.
    advance_disabled = _focus_auto_advance_disabled(cfg)

    advance_trigger: str | None = None

    # TB-283: empty-cycles is the sole advance signal — runs for every
    # focus regardless of whether it carries a `Progress signals:`
    # sub-block (TB-285 rename of the prior `Done when:` block).
    # Count consecutive ideation cycles that produced 0 proposals
    # against the active focus.
    threshold = _advance_empty_cycles_threshold(cfg)
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

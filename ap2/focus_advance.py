"""Focus-list pointer advance (TB-226 axis 4).

Lifted from `ap2/daemon.py` as part of TB-263's responsibility split. The
orchestrator (`_tick`) calls `_maybe_advance_focus` once per tick; this
module owns the pointer-advance policy itself:

  - `_ideation_empty_against_focus`: tail-scan counter for the heuristic
    "N consecutive 0-proposal cycles against the active focus" path.
  - `_judge_done_when`: one-shot SDK judge call evaluating whether a
    focus's `Done when:` bullets are substantively met. Test seam: the
    monkey-patched value on `daemon._judge_done_when` takes effect via
    the daemon-module attribute lookup below.
  - `_maybe_advance_focus`: the orchestrator entry point. Reads goal.md's
    focus list + `focus_pointer.json`, advances the in-memory pointer
    when criteria are met, emits `roadmap_complete` when all foci are
    exhausted.

Reads goal.md's multi-`## Current focus:` heading list + the runtime
pointer (`focus_pointer.json`). Advances the in-memory pointer when:
  - The active focus carries an explicit `Done when:` sub-block AND a
    short LLM-judge call rules the bullets substantively met (one judge
    call per advance attempt, cost knob `AP2_FOCUS_DONE_WHEN_JUDGE_EFFORT`).
  - The active focus has NO explicit `Done when:` sub-block AND the
    heuristic-fallback empty-cycles counter has reached
    `AP2_FOCUS_ADVANCE_EMPTY_CYCLES` (default 3). The counter
    increments on each tick where ideation produced 0 proposals against
    the active focus (the empty-board signal).
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

import os

from . import events, goal
from .auto_approve import _append_decisions_needed_bullet
from .config import Config


_FOCUS_RECENT_TAIL_N = 200


def _ideation_empty_against_focus(tail: list[dict], focus_title: str) -> int:
    """Count consecutive recent ideation cycles that produced 0 proposals
    against `focus_title`. Walks `tail` (newest events at the end)
    backwards; resets the count at the first cycle that DID propose
    something against the focus (an `ideation_complete` whose summary
    mentions a TB-N proposal against the focus title, OR any
    `ideation_proposal_recorded` event in the window).

    Counting policy (deliberately permissive — the briefing's heuristic
    is "N consecutive 0-proposal cycles against the active focus"):
      - `ideation_empty_board` and `ideation_complete` events with no
        proposal-recorded counterpart in the same window count toward
        the empty-cycles total.
      - `ideation_proposal_recorded` resets the counter (a fresh
        proposal landed against the active focus; the focus isn't
        exhausted).
      - Events older than the most recent `focus_advanced from=<title>`
        are ignored (the prior focus's cycles don't count against the
        new active focus's freshness).
    """
    # Reset cutoff: the most recent `focus_advanced` event marks the
    # start of the current focus's window.
    cutoff_idx = -1
    for i, e in enumerate(tail):
        if e.get("type") == "focus_advanced" and str(e.get("to") or "") == focus_title:
            cutoff_idx = i
    relevant = tail[cutoff_idx + 1:]
    count = 0
    for e in relevant:
        typ = e.get("type")
        if typ == "ideation_proposal_recorded":
            # A real proposal landed → reset.
            count = 0
            continue
        if typ in ("ideation_empty_board", "ideation_complete"):
            count += 1
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

    The SDK Done-when judge is invoked at most once per tick (only when
    the active focus has explicit `Done when:` bullets). Cost is bounded
    by `AP2_FOCUS_DONE_WHEN_JUDGE_EFFORT` (default `medium` — cheaper
    than the verifier's `high` because the question is one-shot and
    coarse-grained).
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

    if active.has_done_when() and active.done_when_bullets:
        # Done-when judge path. Pure / SDK call only when the focus
        # has bullets to evaluate against. An empty Done-when sub-
        # block ("operator authored the heading but no criteria yet")
        # falls through to the heuristic path: there's nothing to
        # judge yet.
        #
        # TB-263: indirection through `daemon._judge_done_when` so the
        # test seam `monkeypatch.setattr(daemon, "_judge_done_when", ...)`
        # in `test_tb226_focus_rotation.py` continues to take effect
        # after the focus-advance lift. The daemon re-exports
        # `_judge_done_when` from this module; the late `from .`
        # binding here means each call resolves through daemon's
        # current attribute value (the monkey-patched stub).
        from . import daemon as _daemon
        verdict = await _daemon._judge_done_when(cfg, sdk, active)
        if verdict == "yes":
            advance_trigger = "done_when_judge"
        # `no` / `insufficient_evidence` / judge-error → no advance.
    else:
        # Heuristic-fallback path: count consecutive ideation cycles
        # that produced 0 proposals against the active focus.
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


async def _judge_done_when(cfg: Config, sdk, focus: "goal.FocusItem") -> str:
    """Invoke a short SDK judge call to evaluate whether a focus's
    `Done when:` bullets are substantively met.

    Returns one of `"yes"`, `"no"`, `"insufficient_evidence"`, or
    `"judge_error"`. The caller only advances on `"yes"`; all other
    verdicts are conservative (leave the pointer alone).

    Cost is bounded by `AP2_FOCUS_DONE_WHEN_JUDGE_EFFORT` (default
    `medium`). The prompt is a compact stand-alone block: focus title +
    Done-when bullets + the last ~10 `task_complete` titles + the head
    of `ideation_state.md`. No filesystem reads beyond those — the
    judge gets a finite context window per advance attempt.

    Test seam: the SDK call is mocked in `test_tb226_focus_rotation.py`
    by monkey-patching this function to return a fixed verdict. The
    function is async so the test stub can be an `async def`.
    """
    bullets = focus.done_when_bullets or []
    if not bullets:
        # Defensive: caller should already check `has_done_when()` +
        # non-empty bullets, but if we get here we can't make a
        # judgment.
        return "insufficient_evidence"

    # Build the prompt body. Compact — the brief stipulates a SHORT
    # judge call, not a full agent.
    tail = events.tail(cfg.events_file, 200)
    recent_completes: list[str] = []
    for e in tail:
        if e.get("type") != "task_complete":
            continue
        tid = str(e.get("task") or "")
        status = str(e.get("status") or "")
        summary = str(e.get("summary") or "")[:200]
        if tid:
            recent_completes.append(f"- {tid} [{status}] {summary}")
    recent_completes = recent_completes[-10:]

    ideation_state_path = (
        cfg.project_root / ".cc-autopilot" / "ideation_state.md"
    )
    if ideation_state_path.exists():
        try:
            ideation_head = ideation_state_path.read_text()[:3000]
        except OSError:
            ideation_head = ""
    else:
        ideation_head = ""

    bullet_block = "\n".join(f"- {b}" for b in bullets)
    completes_block = "\n".join(recent_completes) or "(none in window)"
    prompt = (
        f"You are evaluating whether the focus `{focus.title}` in "
        f"goal.md is substantively done.\n\n"
        f"## Done-when bullets\n\n{bullet_block}\n\n"
        f"## Recent task completes (last 10)\n\n{completes_block}\n\n"
        f"## Ideation state (head)\n\n{ideation_head}\n\n"
        f"Are the Done-when bullets substantively met? Reply with one "
        f"of `yes` / `no` / `insufficient_evidence` on the FIRST line "
        f"of your response, followed by a single sentence of "
        f"rationale. The daemon parses the first token only."
    )

    effort = goal.done_when_judge_effort()
    text = ""
    try:
        options = sdk.ClaudeAgentOptions(
            cwd=str(cfg.project_root),
            allowed_tools=[],
            permission_mode="bypassPermissions",
            # 4 turns is enough for the verdict + rationale; the judge
            # has no tools so it cannot ramble across many turns. Kept
            # as a small int literal (not an env knob) because the
            # briefing names only the three TB-226 knobs as new surface
            # and adding a fourth dilutes the operator-facing knob list.
            max_turns=4,
            setting_sources=["project"],
            model=os.environ.get("AP2_AGENT_MODEL", "claude-opus-4-7"),
            extra_args={"effort": effort},
        )
        async for msg in sdk.query(prompt=prompt, options=options):
            content = getattr(msg, "content", None)
            if isinstance(content, list):
                for part in content:
                    t = getattr(part, "text", None)
                    if isinstance(t, str) and t.strip():
                        text = t.strip()
            else:
                t = getattr(msg, "result", None)
                if isinstance(t, str) and t.strip():
                    text = t.strip()
    except Exception:  # noqa: BLE001
        return "judge_error"

    if not text:
        return "insufficient_evidence"
    first = text.splitlines()[0].strip().lower()
    # Tolerate `**yes**` / `Yes.` / ``"yes"`` shapes.
    first = first.strip("*`'\".:, ")
    if first.startswith("yes"):
        return "yes"
    if first.startswith("no"):
        return "no"
    if "insufficient" in first:
        return "insufficient_evidence"
    return "insufficient_evidence"

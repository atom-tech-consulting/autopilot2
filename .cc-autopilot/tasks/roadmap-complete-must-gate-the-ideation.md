# roadmap_complete must gate the ideation trigger only — remove the daemon Backlog-dispatch halt so the queue always drains

Tags: #autopilot #daemon #ideation #roadmap #dispatch #regression-pin

## Goal

When goal.md's `## Current focus:` roadmap is exhausted, the daemon enters a `roadmap_complete` state that currently does TWO things: (1) gates the ideation trigger so no new proposals are generated — correct, and already implemented at `ap2/ideation.py:847` (`if _goal.roadmap_exhausted(cfg): emit ideation_skipped`); and (2) ALSO halts auto-promotion of Backlog tasks at `ap2/daemon.py:1887` (`if backlog is not None and goal.roadmap_exhausted(cfg): backlog = None`) — incorrect overreach. The second gate freezes the EXISTING queue, including operator-added (`ap2 add`) and operator-approved (`ap2 approve`) tasks that have nothing to do with the roadmap, forcing the operator to `ap2 ack roadmap_complete` just to drain work they already explicitly queued.

This bit live on 2026-05-20: TB-273 (operator-approved) and TB-274 (operator-added) sat frozen in Backlog for hours with 0 Active because `roadmap_complete` fired at 12:20 and the dispatch halt blocked promotion. Note `ap2 approve` / `ap2 add` leave tasks in Backlog (not the Ready section), so the un-gated `board.next_ready()` escape hatch the halt's comment relies on does NOT actually let operator-queued work through.

The fix: roadmap_complete gates the IDEATION TRIGGER only (already done), and NEVER blocks task dispatch. Once ideation is gated, no new speculative work can enter the Backlog anyway — so everything queued is operator-originated or already-proposed, and should always drain. A genuine full-stop is `ap2 pause`, a separate explicit mechanism.

Goal anchor: serves `goal.md` `## Done when` bullet "an operator can point ap2 at a fresh project, paste a goal.md, and walk away for a week without intervention." A daemon that freezes its own already-queued work and demands an operator ack to resume is the antithesis of walk-away — it manufactures an intervention for work the operator already greenlit.

Why now: this just cost a multi-hour stall on two operator-queued tasks and forced a manual ack. The dispatch halt is pure overreach — the legitimate "no new work without a focus" goal is already met by the ideation-trigger gate; the dispatch halt only adds the freeze-the-queue failure mode.

## Scope

- `ap2/daemon.py` — remove the roadmap-complete dispatch halt at ~line 1887: the `if backlog is not None and goal.roadmap_exhausted(cfg): backlog = None` block. Backlog auto-promotion proceeds regardless of roadmap state. (Confirm `goal.roadmap_exhausted` has no other load-bearing caller in the dispatch path before removing the import if it becomes unused.)
- `ap2/ideation.py` — the ideation-trigger gate at ~line 847 STAYS (it's the correct, sole mechanism for "stop proposing when roadmap exhausted"). Verify it's intact; no change expected beyond confirming.
- `ap2/focus_advance.py` — reword the `roadmap_complete` decisions-needed bullet (~line 144, currently "Auto-promote of Backlog tasks is halted until ... ack") so it no longer claims dispatch is halted. New wording: ideation is parked (no active focus); extend the roadmap (`ap2 update-goal`) to resume ideation, or `ap2 ack roadmap_complete` to dismiss the notice. Dispatch is NOT affected.
- `ap2 status` focus line / any surface that prints "ROADMAP_COMPLETE — ack to resume" — reword so it doesn't imply task dispatch is blocked (it now means "ideation parked", not "queue frozen").
- Tests: update the existing tests that pin the OLD dispatch-halt behavior (notably `ap2/tests/test_tb226_focus_rotation.py`, and check `test_tb246_ideation_roadmap_complete_gate.py`) to assert the NEW behavior — roadmap-exhausted gates ideation but a dispatchable Backlog task still promotes/dispatches. Add a regression-pin test for the exact bug: roadmap exhausted + dispatchable Backlog task present → task dispatches (not held).

## Design

- Split the halt's two responsibilities cleanly: ideation-trigger gate (keep, `ideation.py`) vs dispatch gate (remove, `daemon.py:1887`).
- The operator `ap2 ack roadmap_complete` / roadmap-extension flow now affects only whether IDEATION resumes — it never gates dispatch. Acking becomes "dismiss the parked-ideation notice"; extending the roadmap re-arms ideation.
- Hard-stop semantics (halt everything including queued work) remain available via `ap2 pause` — a deliberate, explicit operator action, not a side effect of running out of focus.
- Keep the `roadmap_complete` event itself (audit signal that ideation hit the end of the roadmap) and the decisions-needed bullet (informational nudge to extend the roadmap), just stripped of dispatch-halting power and reworded.
- Un-approved ideation proposals (`@blocked:review`) still won't auto-promote — that's the review gate, independent of the roadmap, and is unchanged.

## Verification

- `uv run pytest -q ap2/tests/` — full suite passes (with the old halt-behavior tests updated to the new behavior).
- `! grep -qE "and goal\.roadmap_exhausted\(cfg\)" ap2/daemon.py` — the dispatch-path roadmap-exhaustion halt conjunction is gone from daemon.py (the `!` inverts grep so absence passes).
- Prose: a regression-pin test asserts that when the roadmap is exhausted AND a dispatchable Backlog task exists, the daemon auto-promotes/dispatches it (the exact TB-273/TB-274 freeze being fixed). The judge confirms by reading the test and seeing it would have failed pre-fix.
- Prose: the ideation-trigger gate in `ap2/ideation.py` still skips ideation (`ideation_skipped`) when `roadmap_exhausted` is true — a test pins this is intact, so the fix removes ONLY the dispatch halt, not the ideation gate.
- Prose: `ap2/focus_advance.py`'s `roadmap_complete` decisions-needed bullet and the `ap2 status` roadmap-complete surface no longer claim Backlog auto-promotion / dispatch is halted; they describe ideation being parked. The judge confirms via Read.

## Out of scope

- Changing `ap2 pause` / `ap2 resume` (the explicit full-stop) — unchanged.
- Changing the review gate (`@blocked:review`) behavior — un-approved ideation proposals still require `ap2 approve`.
- Auto-extending the roadmap or auto-acking — operator still decides whether/how to give ideation a new focus.
- Removing the `roadmap_complete` event or the decisions-needed surface entirely — keep both as informational signal, just non-halting.
- Touching the auto-approve / auto-unfreeze halts (TB-223/224/272) — those are separate, legitimate circuit breakers and stay as-is.

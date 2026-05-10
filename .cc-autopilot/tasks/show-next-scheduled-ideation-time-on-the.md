# Show next scheduled ideation time on the web overview

## Goal

This task is anchored in goal.md's "Current focus: ideation quality signal collection" — the focus calls for accumulating operator-decision and proposal-outcome data so ideation can be tuned against evidence. Operators observing proposals as they arrive is part of that loop: "Design the instrumentation with both audiences [operator and prompt-author] in mind from the start: structured, agent-readable, and persistent across cycles." Today the web overview (`/`) surfaces board state, recent events, and pending operator queue ops (TB-162), but offers no visibility into ideation's cadence — the operator can't tell at a glance "when will the next proposals land?" without checking `cron_state.json` by hand and computing the cooldown delta against `now`.

This task adds a compact "Ideation" status card to the web overview's top section — same surface as TB-162's pending-queue card — showing the gate state and time-to-next-fire when applicable.

The operator gains a synchronous, no-CLI-required answer to "is ideation about to propose, blocked, or in cooldown?" — useful when correlating proposals with the operator-decision signal the focus area is collecting.

Why now: TB-160 made the trigger threshold operator-tunable (`AP2_IDEATION_TRIGGER_TASK_COUNT`), TB-152 + TB-163 wired rejection reasons into the operator log and back into the prompt, and TB-159 added manual `ap2 ideate` triggering. Together those make ideation cadence variable in ways the operator now actively manages — but with no synchronous visibility on the resulting timing. Surfacing the gate state on the overview closes the observability gap created by those changes, and gives the operator a foothold for the empirical signal-collection work the new focus calls for.

## Scope

- `ap2/web.py` — new helper `_render_ideation_status_block(cfg) -> str` that computes the current ideation gate state and emits an HTML card. Empty card (or omitted server-side) is acceptable when the daemon is offline / cron state file is missing.
- `ap2/web.py` — `_render_home` (the `/` index handler) inserts the new card alongside (above or below) the existing pending-queue card from TB-162.
- `ap2/web.py` (CSS) — small `.ideation-status` styling block, color-tinted by state (eligible=green, cooldown=neutral, blocked=yellow/orange, disabled=grey). Reuse existing palette where reasonable.
- Tests in `ap2/tests/test_web.py`.

## Design

### Gate state computation

A helper returns the current state (or in line with `_render_ideation_status_block`):

```python
def _ideation_gate_state(cfg: Config) -> dict:
    """Returns:
      disabled: bool                 (AP2_IDEATION_DISABLED in env)
      threshold: int                 (AP2_IDEATION_TRIGGER_TASK_COUNT)
      cooldown_s: int                (AP2_IDEATION_COOLDOWN_S)
      active_count: int
      queued_count: int              (Ready + Backlog)
      last_fire_ts: str | None       (from cron_state.json, IDEATION_NAME)
      next_eligible_ts: str | None   (last_fire + cooldown_s; None if never fired)
      seconds_until_eligible: int    (>=0; 0 if cooldown elapsed or never fired)
      gate_status: str               ("eligible" | "cooldown" | "active_running"
                                      | "queued_full" | "disabled")
    """
```

Resolves the same env knobs / cron state / board counts that `_maybe_ideate` uses, so the displayed state matches the daemon's actual decision logic. Reuse `ap2.ideation._cooldown_s` and `_trigger_task_count` if they're importable; otherwise duplicate the read with a comment pointing at the shared definition.

### Render shape

Five states map to five card variants. Concrete examples (text content, exact HTML tbd):

- **eligible**: `Ideation — eligible, will fire on next tick (≤30s)` (green)
- **cooldown**: `Ideation — cooldown 28m remaining (next eligible 2026-05-04T20:11:25Z)` (neutral)
- **active_running**: `Ideation — blocked: Active task in flight (TB-N)` (yellow)
- **queued_full**: `Ideation — blocked: Ready+Backlog = 5 ≥ threshold 5` (yellow)
- **disabled**: `Ideation — disabled (AP2_IDEATION_DISABLED set)` (grey)

The `cooldown` state shows both the absolute timestamp AND the relative duration — operators reading at any time can compute "is this soon?" without doing math.

When multiple gates would block (e.g., active_running AND queued_full AND cooldown not elapsed), report the FIRST blocking gate per the daemon's check order (the existing `_maybe_ideate` order: disabled → active → threshold → cooldown). This matches the daemon's behavior: a blocked gate earlier in the chain prevents later checks from being evaluated, so reporting the deepest blocker would be misleading.

### Where it sits on `/`

Above the events table, alongside the TB-162 pending-queue card. Suggested ordering in `_render_home`:

1. Pending queue card (TB-162) — when non-empty
2. Ideation status card (this task) — always rendered
3. Events table (existing)

The ideation card is small (1-2 lines) — always rendering is fine; no reason to omit when state is "eligible" or "cooldown."

### Auto-refresh

`/` already auto-refreshes via meta-refresh (TB-130-era plumbing). The card re-evaluates state on every refresh — no client-side ticking needed.

## Verification

- `uv run pytest -q ap2/tests/` — full regression gate passes.
- `grep -nE "_render_ideation_status_block|_ideation_gate_state" ap2/web.py` — helpers are wired (≥1 hit each).
- `grep -qE "ideation-status" ap2/web.py` — CSS class is referenced.
- prose: a test in `test_web.py` synthesizes a fixture project where (a) `cron_state.json` carries an `IDEATION_NAME` last-run timestamp 30 min ago, (b) `AP2_IDEATION_COOLDOWN_S=7200` (default 2h), (c) board has 0 Active and 2 Ready+Backlog (under threshold 5); calls `_render_ideation_status_block(cfg)` (or fetches `/`) and asserts the rendered HTML contains a "cooldown" state indicator AND an absolute next-eligible timestamp AND a relative remaining-duration string.
- prose: a test pins the eligible-state shape — same fixture but last-run timestamp set to 3h ago (cooldown elapsed); rendering reports "eligible" / "next tick" semantics. No "cooldown remaining" text.
- prose: a test pins the active-running blocker — fixture with 1 Active task and recent last-run; assert the rendered HTML names the blocker as "Active task in flight" (or equivalent string identifying the gate); no "cooldown" or "queued" wording leaks.
- prose: a test pins the threshold-full blocker — fixture with 0 Active and Ready+Backlog = threshold; assert the rendered HTML names the blocker as the queue-depth gate, surfaces the actual count and the threshold value (e.g., "5 ≥ threshold 5") so the operator can sanity-check the env knob.
- prose: a test pins the disabled state — fixture with `AP2_IDEATION_DISABLED=1` set in env (use `monkeypatch.setenv`); assert the rendered HTML names the disabled state and references the env knob name verbatim so the operator can grep their env file.
- prose: a test pins gate-priority — fixture where multiple gates would block (e.g., disabled AND active AND cooldown); assert the rendered HTML reports ONLY the first-checked gate per the daemon's `_maybe_ideate` order (disabled wins over active wins over threshold wins over cooldown).

## Out of scope

- Surfacing this same data in `ap2 status` CLI. CLI would need its own renderer; defer until friction observed (the web is the primary at-a-glance surface).
- Showing a graph / time-series of ideation cadence over time. Single point-in-time state is enough for v1.
- Letting the operator click a "force ideate now" button on the card. `ap2 ideate` (TB-159) is the operator-facing knob; web stays read-only.
- Surfacing per-cycle ideation cost / token usage in this card. TB-166 captures `control_run_usage` events; a future TB can add a "last ideation: $X.XX" cell if useful, but it's a separate concern from cadence.
- Refactoring the daemon's gate-check logic to a shared helper for both `_maybe_ideate` and the web. v1 duplicates with a comment; if drift becomes a concern, a follow-up TB consolidates.
- Live ticking down the cooldown countdown without page refresh. Meta-refresh on `/` is sufficient cadence.
- Surfacing pending-review counts on the same card. TB-151 already surfaces those in `ap2 status` and the web's existing review section.

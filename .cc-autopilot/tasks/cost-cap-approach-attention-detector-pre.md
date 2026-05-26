### TB-290 — `cost_cap_approach` attention detector (pre-trip window-cap-approach surface)

## Goal

Add a fifth attention detector to `ap2/attention.py` — `_detect_cost_cap_approach` — returning a singleton `AttentionCondition` when the rolling 24h auto-approved `task_run_usage` token sum (same walk `auto_approve._auto_approve_check_violations` performs for its window-cap branch, `auto_approve.py` L442-463) is ≥ `AP2_AUTO_APPROVE_COST_APPROACH_PCT * AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP` AND below `AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP`. Closes the pre-trip path of the "cost anomalies" leg of `Current focus: operator-legible reporting and monitoring` Progress signal #3 ("Attention-needing conditions ... cost or validator-judge anomalies ... surfaced proactively in operator-legible terms, distinct from routine progress updates").

The post-trip state (`window_token_cap_exceeded`) already surfaces via the existing `_detect_auto_approve_paused` once `_auto_approve_check_violations` returns the `window_cap` reason and `_PAUSE_REASON_ACK_VERB` resolves to `auto_approve_window_resume`. This detector is the pre-trip companion: same 24h walk, same cap knob, but fires at a configurable percentage threshold below the cap so the walk-away operator can act before auto-approve halts.

Why now: the post-trip surface puts the bullet in the operator's face once dispatch is already paused; once paused they must `ap2 ack auto_approve_window_resume` and decide whether to bump the cap or wait out the 24h window. A 75%-of-cap warning gives them a budget-spending signal hours earlier, on the same Attention surface as the other detectors. Last cycle's ideation_state.md (2026-05-26T06:05Z) listed cost_cap_approach as deferred on a faulty premise (grep `cost_cap ap2/` returned only Out-of-scope mentions and a janitor test, the actual cap knobs are spelled `AP2_AUTO_APPROVE_{PER_TASK,WINDOW}_TOKEN_CAP`); the infrastructure is in place — only the pre-trip detector is missing.

## Scope

- `ap2/attention.py`:
  - Add `DEFAULT_AUTO_APPROVE_COST_APPROACH_PCT = 75` to `ap2/config.py` and import here (mirrors `DEFAULT_TASK_STUCK_THRESHOLD_S` / `DEFAULT_TASK_FROZEN_RECENCY_S` shape).
  - Add `_cost_approach_pct() -> int` resolver mirroring `_task_frozen_recency_s()` shape (fresh-read-each-call from `os.environ`, invalid-value fallback to default, clamp to 0-99 — `>= 100` means trip-not-approach which is the post-trip surface).
  - Add `_detect_cost_cap_approach(cfg, *, tail, now) -> list[AttentionCondition]` returning zero or one element. Singleton key `cost_cap_approach:window`. Walk the events tail with the same shape `_auto_approve_check_violations` uses for its window-cap branch (locate the most recent `operator_ack` with the `auto_approve_window_resume` token, restrict to events after that index, filter `task_run_usage` events to auto-approved task ids inside the 24h window, sum input+output tokens). Implementation may either call existing `auto_approve.py` helpers if convenient or inline the walk — the only load-bearing property is that the sum matches the post-trip surface's sum, since drift between the approach-check and trip-check would mean an Attention bullet that doesn't predict the eventual pause.
  - Wire into `detect_attention_conditions`'s `out.extend(...)` list following the established sibling-detector pattern.
- `ap2/tests/test_tb290_attention_cost_cap_approach.py`: new regression module. Pin the briefing arcs:
  - No fire when `AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP` is unset / 0 (cap disabled → no approach state).
  - No fire when sum < `pct * cap` (below threshold).
  - One condition when sum ≥ `pct * cap` AND sum < cap (approach window).
  - No fire when sum ≥ cap (the post-trip surface owns this; we explicitly suppress to avoid double-bullet noise).
  - Per-(type, key) debounce respected across consecutive ticks within `AP2_ATTENTION_DEBOUNCE_S`.
  - `AP2_AUTO_APPROVE_COST_APPROACH_PCT=50` env override fires at 50% sum.
  - Recent `operator_ack` with `auto_approve_window_resume` token resets the count (mirrors the same semantics the post-trip surface uses — events before the ack don't count).
- `ap2/howto.md` + `ap2/architecture.md`: add `cost_cap_approach` to the attention-detector inventory line alongside the sibling detector names.
- `ap2/env_reload.py`: add `AP2_AUTO_APPROVE_COST_APPROACH_PCT` to `HOT_RELOADABLE_KNOBS` so an operator tightening the threshold takes effect on the next tick without a daemon restart.

## Design

- `type="cost_cap_approach"`, `key="cost_cap_approach:window"` — singleton condition (one such state at a time across the daemon). Per-(type,key) debounce in the existing `should_suppress` helper handles "still approaching at next tick" without re-firing.
- `summary` shape (rendered verbatim by the generic-fallback branch in `render_attention_section` at `ap2/status_report.py` L991-994; no per-detector renderer branch needed): `f"auto-approve cost cap approach: {total} tokens used in last 24h, {pct_used:.0f}% of window cap {cap} (threshold {approach_pct}%); consider raising AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP or pausing via ap2 ack auto_approve_window_resume"`.
- `extras={"total_tokens_24h": total, "window_cap": cap, "approach_pct": approach_pct, "pct_used": pct_used, "window_s": 86400}`.
- Anchor `ts` = the freshest in-window `task_run_usage` event's `ts` (fall back to `now` when no parseable ts — same defensive shape used by the sibling singleton detector).
- Strict-less-than-cap upper bound (not just `>= pct*cap`): when `sum >= cap`, the existing trip check halts dispatch and the post-trip pause bullet fires the same tick. Two simultaneous bullets (one "approach" + one "tripped") would be noise; this detector explicitly hands off above the trip line. The dispatch precedence in `_auto_approve_check_violations` (task_error → per_task_cap → window_cap) already short-circuits — by the time the sum is above the cap, dispatch has halted and the operator's signal is the pause, not the approach.
- No-op when the resolved window cap is 0 (existing knob semantics: unset / 0 / negative env → cap disabled). The detector is opt-in via the operator configuring the cap, consistent with the cap subsystem's "operators who haven't budgeted their project don't get a hardcoded cap surprising them" design.
- Independent of the post-trip pause detector — both detectors can be live, but mutual exclusion at `sum >= cap` ensures no duplicate-bullet noise.

## Verification

- `uv run pytest -q ap2/tests/test_tb290_attention_cost_cap_approach.py` — new test module passes (≥6 tests covering the scenarios above).
- `uv run pytest -q ap2/tests/` — full suite passes (no regressions on sibling detector surfaces).
- `grep -q "_detect_cost_cap_approach" ap2/attention.py` — detector function present.
- `grep -q "AP2_AUTO_APPROVE_COST_APPROACH_PCT" ap2/config.py` — env knob default named.
- `grep -q "DEFAULT_AUTO_APPROVE_COST_APPROACH_PCT" ap2/config.py` — default constant named.
- `grep -q "AP2_AUTO_APPROVE_COST_APPROACH_PCT" ap2/env_reload.py` — knob listed in `HOT_RELOADABLE_KNOBS`.
- `grep -q "cost_cap_approach" ap2/howto.md` — detector named in the inventory.
- `grep -q "cost_cap_approach" ap2/architecture.md` — detector named in the architecture map.
- `grep -rq "_detect_cost_cap_approach" ap2/tests/` — test references the detector by name (drift-gate coverage for the new detector identifier).
- `! grep -q "_detect_cost_cap_approach" ap2/auto_approve.py` — the detector lives in `attention.py`, not auto-approve (absence-check: the new logic must not bleed into the gate module — the gate stays the trip check, attention stays the surface).

## Out of scope

- Per-task in-flight cost-approach detection — `task_run_usage` events fire at task_complete, not during the run, so streaming/in-progress accounting requires a different event vocabulary. Window-cap approach (this task) uses completed events and is the directly-actionable surface.
- Recalibrating `AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP` or the new `AP2_AUTO_APPROVE_COST_APPROACH_PCT` defaults — both are operator-owned configuration; this task ships the detector with conservative defaults (cap-disabled-by-default; 75% approach threshold) and leaves tuning to the operator.
- Modifying the post-trip pause detector or `_auto_approve_check_violations` — both remain; this detector is purely additive and explicitly hands off above the trip line.
- Web `/attention` page evolution — a "once the event vocabulary lands AND accrues data" follow-up. After this task, 5 detectors exist and ~few days of attention_raised data; defer the page until ≥1 week of data accrues to validate the design.
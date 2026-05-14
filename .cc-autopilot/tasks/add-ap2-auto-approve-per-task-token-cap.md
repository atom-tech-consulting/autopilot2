# Add `AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP` + `AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP` + `task_error` halt on top of TB-223's auto-approve gate (axis 3 cost + blast-radius guards)

Tags: `#autopilot` `#automation` `#operator-surface` `#cost` `#regression-pin`

## Goal

Advance **Current focus: end-to-end automation** axis 3 ("Cost and blast-radius guards", goal.md L103-113) by layering explicit per-task and per-24h-window token ceilings, plus a single-event `task_error` halt, on top of TB-223's `AP2_AUTO_APPROVE` gate. TB-223 ships the auto-approve switch + tag-opt-out + cumulative-regression pause (N consecutive `verification_failed` → Frozen) but explicitly excludes "Token-cost ceilings / per-window budgets" (TB-223 brief L77) and does NOT distinguish infrastructure failures (`task_error`) from work-quality failures (`verification_failed`). Goal.md L107-110 names both as separate halt-conditions ("cost ceilings... regression pauses... unscheduled-failure detection (verifier returns `task_error` not `verification_failed` → infrastructure issue, halt and surface)"); this task delivers the missing two.

Why now: TB-223 lands the auto-approve gate but the operator can't responsibly flip `AP2_AUTO_APPROVE=1` until the cost ceilings exist — a runaway "successful-but-wasteful" loop satisfies verification while burning tokens unbounded, and a `task_error` cascade (SDK timeout, agent OOM, kernel SIGKILL) needs operator attention not a silent retry. Goal.md's delete-test (L146-151): if these guards don't ship, the auto-approve mode is unbounded-blast-radius and the operator's only safe choice is to leave the knob unset. With them, the safety floor catches the exact failure modes the operator's per-task review currently catches (cost runaway, infrastructure halt) and the walk-away envelope expands.

## Scope

(1) **New env knob `AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP`** (default unset = no cap):
  - Integer max combined input+output tokens per task. Read at auto-promote time in `ap2/daemon.py`'s auto-promote path (the same site TB-223 wires `AP2_AUTO_APPROVE` into).
  - Computed per-task from `task_run_usage` events emitted by the in-flight agent (TB-157 instrumentation; TB-165 persists usage on success; TB-166 covers control-agent usage). On each `task_run_usage` arrival for an auto-approved task, if cumulative > cap, halt the in-flight task via the existing graceful-cancel path (the same path operator `ap2 stop` uses) and emit `auto_approve_halted` with `reason=per_task_cap`.
  - When unset (default), no cap is applied — current behavior preserved.

(2) **New env knob `AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP`** (default unset = no cap):
  - Integer max cumulative input+output tokens across all auto-approved tasks in a rolling 24h window.
  - Computed by summing `task_run_usage` token fields over tasks identified as auto-approved via TB-223's `auto_approved` audit event (4) within `now - 24h`.
  - At auto-promote time, if the projected cumulative (current window total + 0 for the new task — projection is post-hoc, not predictive) ALREADY exceeds the cap, halt auto-promotion (mirroring TB-223's freeze-threshold pause shape) until the operator emits `ap2 ack auto_approve_window_resume` (TB-106 ack pattern, identical to TB-223 (3)'s ack). Manual `ap2 approve TB-N` continues to dispatch — only the auto-approved path pauses.
  - Emit `auto_approve_halted reason=window_cap window_used=<int> cap=<int>` event on halt.

(3) **`task_error` → single-event halt + decisions-needed**:
  - Distinct from `verification_failed` (TB-223 regression-pause condition). A `task_error` event on an auto-approved task indicates infrastructure failure (SDK timeout, agent OOM, briefing read failure, etc.) per `ap2/events.py` conventions; one is enough to halt — no N-consecutive threshold.
  - Daemon detects `task_error` on an auto-approved task in the same auto-promote tick that TB-223's freeze-threshold check runs; halts auto-promote and emits a `decisions needed from operator` ideation_state entry naming the failing TB-N + the error excerpt. Operator resumes via the same `ap2 ack auto_approve_window_resume` ack (one ack covers both window-cap and task-error halts since they share the same auto-promote-paused state).

(4) **Audit events**:
  - `auto_approve_skipped reason=<token> task=<TB-N>` — per-task cap or window cap preempted a promotion attempt.
  - `auto_approve_halted reason=<token> task=<TB-N>` — running task halted (per_task_cap) or auto-promote stream halted (window_cap, task_error).
  - Registered in `ap2/events.py` event-type list (TB-208 / TB-211 / TB-212 coverage-drift gate consumes the registered list).

(5) **Documentation**:
  - Extend the `## Operator-in-the-loop relaxations` section in `ap2/howto.md` (introduced by TB-223 (5)) with both new knobs' behavior, defaults (unset), the 24h-rolling-window computation (sum of `task_run_usage` events filtered by `auto_approved` event task-ids within `now - 24h`), and the `task_error` halt rule.
  - Add `AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP` and `AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP` rows to `ap2/tests/test_docs_drift.py`'s env-knob registry (TB-203 pattern).
  - Add the two new knobs to `ap2/tests/test_coverage_drift.py`'s test-presence env-knob registry; this task's own tests satisfy the gate without a shim row (TB-208 / TB-210 pattern).
  - Add `auto_approve_skipped` and `auto_approve_halted` to the event-type drift gate (TB-208 / TB-211 / TB-212 pattern); this task's own tests satisfy the gate.

(6) **Not in this task**:
  - Auto-unfreeze on agent-diagnosed briefing-shape fixes (axis 2 — separate proposal this cycle).
  - Multi-focus pointer / focus-advance (axis 4 — separate future proposal).
  - Per-tag cost caps (tag-based opt-out from TB-223 (2) is the first filter; per-tag budgets is a different shape).
  - A `ap2 cost-status` CLI verb (`ap2 logs` + `ap2 status` already render usage from TB-157 / TB-165 / TB-179 / TB-181 surfaces).
  - Predictive cost-estimation before a task dispatches (the per-task cap reacts on `task_run_usage` arrivals; no estimator).
  - Refactoring the `task_run_usage` event schema (existing fields are read as-is).

## Design

Sequencing: this task is structurally dependent on TB-223 (consumes TB-223's `auto_approved` audit event + the auto-promote insertion point that reads `AP2_AUTO_APPROVE`). If TB-223 has not landed when this task is dispatched, the briefing's references will not resolve cleanly. Operator should `ap2 approve TB-223` before approving this task, or this task should land with `@blocked:TB-223` (the operator can adjust at approve-time).

Why two knobs, not one: per-task cap catches the single-runaway pattern (one task in an infinite-tool-call loop burning $50 of tokens before the verifier even runs). Window cap catches the drift pattern (50 small tasks each within the per-task cap but cumulatively unbounded). They protect against orthogonal failure modes; both must be operator-tunable.

Why default unset on both: matches TB-223's conservative-default pattern (auto-approve is opt-in; cost caps are opt-in extensions). Operators who haven't done the cost-budgeting math for their project don't need a hardcoded cap surprising them. The docs explicitly call out the recommended pattern: "set both caps BEFORE flipping `AP2_AUTO_APPROVE=1`."

Why 24h rolling window vs. calendar-day: matches operator's natural rhythm without timezone ambiguity. Computed from a single events.jsonl tail scan filtered to `auto_approved` task-ids + their `task_run_usage` totals — no new state file, no new persistence contract. Same shape the cron status-report already uses for recent-events surfacing.

Why single-event `task_error` halt (no N threshold): `task_error` is structurally rare in steady-state (the verifier's normal failure path is `verification_failed`, not `task_error`); a single event indicates infrastructure breakage that benefits from operator attention immediately, not after N similar events. Distinct from TB-223's regression-pause N=3 default which is calibrated for the noisier `verification_failed` channel.

Shared ack verb `ap2 ack auto_approve_window_resume`: window-cap and task-error halts share the same "auto-promote paused, manual approve still works" state. One ack verb to resume both reduces the operator's mental model size. The ack writes the same `operator_log.md` line shape as other TB-106-pattern acks.

## Verification

- `uv run pytest -q ap2/tests/` — full suite green (exit 0) after the change.
- `grep -nE "AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP" ap2/daemon.py` — per-task cap knob is read in the auto-promote / running-task path.
- `grep -nE "AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP" ap2/daemon.py` — window cap knob is read in the auto-promote path.
- `grep -nE "AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP|AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP" ap2/howto.md` — both knobs documented in howto.
- `grep -nE "task_error" ap2/daemon.py` — at least one match in the auto-promote-halt path (the new handler).
- `grep -nE "auto_approve_halted|auto_approve_skipped" ap2/events.py` — both new event types registered in the event-type list.
- `grep -rnE "AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP" ap2/tests/` — at least one test file references the per-task cap knob.
- `grep -rnE "AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP" ap2/tests/` — at least one test file references the window cap knob.
- `[ "$(grep -rlE 'auto_approve_halted' ap2/tests/ | wc -l)" -ge 1 ]` — at least one test file references the halt event type.
- `[ "$(grep -rlE 'auto_approve_skipped' ap2/tests/ | wc -l)" -ge 1 ]` — at least one test file references the skipped event type.
- Prose: new tests cover at minimum six behavioral pinning cases — (a) unset per-task cap, no skip; (b) per-task cap exceeded mid-run, running task halted with `auto_approve_halted reason=per_task_cap`; (c) unset window cap, no halt; (d) window cap exceeded, auto-promote halted with `auto_approve_halted reason=window_cap` and manual `ap2 approve` still dispatches; (e) `task_error` on an auto-approved task halts auto-promote with one event (no N threshold); (f) `ap2 ack auto_approve_window_resume` resumes auto-promote after window-cap or task-error halt. Judge confirms via `Read` of new test files.
- Prose: `ap2/howto.md`'s `## Operator-in-the-loop relaxations` section explicitly names both new knobs' defaults (unset), the 24h-rolling-window computation contract, the `task_error` single-event halt rule, and the shared ack verb. Judge confirms via `Read` of the howto section.

## Out of scope

- Auto-unfreeze on agent-diagnosed briefing-shape fixes (axis 2 — separate proposal this cycle).
- Multi-focus pointer / focus-advance (axis 4 — separate future proposal).
- Per-tag cost caps — tag-based opt-out from TB-223 (2) is the first filter; per-tag budgets is a different shape.
- A `ap2 cost-status` CLI verb — `ap2 logs` + `ap2 status` already render usage via TB-157 / TB-165 / TB-179 / TB-181.
- Predictive cost-estimation before a task dispatches — the per-task cap reacts on `task_run_usage` arrivals; no estimator.
- Refactoring the `task_run_usage` event schema — existing fields are read as-is.
- Calendar-day window (vs. rolling 24h) — rolling matches operator rhythm without timezone ambiguity.
- Auto-rollback of high-cost auto-approved tasks after completion — `ap2 rollback` already exists; auto-rollback is a separate proposal.

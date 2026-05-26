# TB-287 — `task_frozen` attention detector (TB-282 follow-up closing Progress signal #3 "frozen tasks" leg)

## Goal

Add a second attention detector to `ap2/attention.py` — `_detect_task_frozen` — that returns one `AttentionCondition` per task in the board's `## Frozen` section whose entry-into-Frozen timestamp (the most-recent `retry_exhausted` or `task_failed` event for that task) is within `AP2_TASK_FROZEN_RECENCY_S` (default 86400 = 24h) AND has no intervening operator-driven `task_unfrozen` or `task_deleted` event. Closes the "frozen tasks" leg of Current focus: operator-legible reporting and monitoring Progress signal #3 ("Attention-needing conditions (stuck / failed / frozen tasks, decisions-needed, cost or validator-judge anomalies) are surfaced proactively in operator-legible terms, distinct from routine progress updates"), which TB-282 deliberately deferred via its Out-of-scope clause naming `frozen_task_recency` as one of the obvious follow-ups (see `ap2/attention.py` L29-32).

Why now: today the 3 Frozen tasks (TB-119 / TB-120 / TB-133) surface only as the `3F` aggregate count in `ap2 status` and the status-report headline. A walk-away operator returning after a day where a new task froze sees the count tick up but gets no proactive nudge to run `ap2 unfreeze TB-N` — exactly the "operator must poll each project to find problems" failure mode goal.md L210-213 names. The detector wraps an existing event-tail walk plus board read; mechanical cost is one function and one test module, mirroring TB-282's `_detect_task_stuck` shape.

## Scope

- `ap2/attention.py`: add `_detect_task_frozen(cfg, *, tail, now)` returning `list[AttentionCondition]`; wire into `detect_attention_conditions` via a second `out.extend(...)` line after the existing `task_stuck` call.
- `ap2/config.py`: declare `DEFAULT_TASK_FROZEN_RECENCY_S = 86400` and add an `_task_frozen_recency_s()` resolver in `attention.py` mirroring `_task_stuck_threshold_s` (fresh-read-each-call so env-reload propagates).
- `ap2/tests/test_tb287_attention_task_frozen.py`: happy-path (Frozen task within recency window → condition fires with operator-legible summary), dormancy (Frozen task older than recency window → no fire), intervening-unfreeze (Frozen → operator queue ack-unfreeze → Backlog → no fire), per-key dedup (two distinct Frozen tasks both surface — debounce is per `task_frozen:<task_id>` key, not per detector kind), env-knob override (`AP2_TASK_FROZEN_RECENCY_S=3600` shortens the window).
- `ap2/howto.md` and `ap2/architecture.md`: add `task_frozen` to the attention-detector inventory line(s) alongside `task_stuck`.

## Design

The detector reads `cfg.tasks_file` board fresh, iterates `Board.iter_tasks("Frozen")`, and for each Frozen task walks the tail in reverse looking for the most-recent `retry_exhausted` or `task_failed` event whose `task` matches. Walks STOP early on a later `task_unfrozen` / `task_deleted` event for that task (means the operator already acted; not a stuck Frozen). The `AttentionCondition` carries:

- `type="task_frozen"`, `key=f"task_frozen:{task_id}"` (per-task debounce so a second freeze isn't suppressed by a first).
- `summary` shape: `f"{task_id} Frozen for {age_h:.1f}h since {freeze_ts}; resume via `ap2 unfreeze {task_id}`"`.
- `extras={"task": task_id, "title": title_or_empty, "age_s": int(age_s), "freeze_ts": freeze_ts, "recency_s": recency_s}`.

`Board.get(task_id).title` resolution is best-effort (mirrors `_detect_task_stuck`'s pattern at L214-217 — empty-string on miss, renderer substitutes a stable placeholder).

Why per-task debounce: a single tick may surface several Frozen tasks; the operator must see each. The existing `AP2_ATTENTION_DEBOUNCE_S` (default 14400 = 4h) prevents re-firing the same `task_frozen:TB-N` key in the same window — operator can ack-unfreeze and the next tick after the window will re-evaluate.

## Verification

- `uv run pytest -q ap2/tests/test_tb287_attention_task_frozen.py` — new test module passes (≥5 tests covering the scenarios above).
- `uv run pytest -q ap2/tests/` — full suite passes (no regressions).
- `grep -q "_detect_task_frozen" ap2/attention.py` — detector function present.
- `grep -q "AP2_TASK_FROZEN_RECENCY_S" ap2/config.py` — env knob declared in config.
- `grep -rq "AP2_TASK_FROZEN_RECENCY_S" ap2/tests/` — env knob has a test reference (test_coverage_drift.py drift gate satisfied).
- `grep -q "task_frozen" ap2/howto.md` — detector named in the inventory.
- `grep -q "task_frozen" ap2/architecture.md` — detector named in the architecture map.

## Out of scope

- Additional detector kinds (`validator_judge_noisy`, `auto_approve_paused`, `cost_cap_approach`) — separate proposed tasks this cycle.
- Auto-unfreeze on Frozen task discovery — operator owns the unfreeze-or-delete decision per goal.md L98-100 (briefing-shape edits the agent self-diagnoses) and is a separate axis-2 surface.
- Changing `_TERMINAL_TASK_EVENT_TYPES` in `attention.py` — that frozenset is `task_stuck`'s contract, not shared with `task_frozen` (the new detector identifies freeze entry, not run termination).
- MM-handler ack-verb plumbing — `ap2 unfreeze` already exists as an operator-queue verb (operator_log.md history confirms regular use).

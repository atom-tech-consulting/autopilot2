# TB-160 — Make ideation trigger threshold configurable via `AP2_IDEATION_TRIGGER_TASK_COUNT` (default 3)

## Goal

Today the natural ideation gate is binary: `has_work = any(task in Active|Ready|Backlog)` — if ANY of those sections has at least one task, ideation skips. The threshold is hardcoded as zero.

This is asymmetric with the ideation prompt, which already says "Propose new tasks ONLY if Backlog has fewer than 3 workable items" — so the prompt expects to fire while Backlog has 0/1/2 items, but the daemon gate keeps it idle as soon as Backlog has 1.

This task makes the threshold configurable: introduce `AP2_IDEATION_TRIGGER_TASK_COUNT` (default 3) and replace the boolean `has_work` gate with a count comparison. Ideation fires when the workable-item count is BELOW the threshold, idles otherwise. This brings the daemon gate in line with the prompt and lets operators tune cadence per project (a project with very fluid scope may want threshold 5; a project with tight focus may want 1).

This unblocks faster ideation iteration: instead of waiting for the board to fully empty before the next natural fire, ideation can re-evaluate as soon as Backlog drops below the threshold.

## Scope

- `ap2/ideation.py` — replace the boolean `has_work` gate in `_maybe_ideate` with a threshold check against `AP2_IDEATION_TRIGGER_TASK_COUNT`. Add a `_trigger_task_count()` helper alongside the existing `_cooldown_s()` (same pattern: read env, parse int with bounds-check, default to module constant). Add `IDEATION_TRIGGER_TASK_COUNT_DEFAULT = 3` next to `IDEATION_COOLDOWN_DEFAULT_S`.
- `ap2/ideation.py` (header doc) — update the comment block at the top of the file to document the new env knob alongside `AP2_IDEATION_COOLDOWN_S` and `AP2_IDEATION_DISABLED`.
- `ap2/architecture.md` and/or `ap2/howto.md` — document the new env knob in the operator-facing reference (where `AP2_IDEATION_COOLDOWN_S` is already documented).
- Tests in `ap2/tests/test_ideation*.py`.

## Design

### What counts toward the threshold

The existing `has_work` check counts tasks across **Active + Ready + Backlog**. Preserve that aggregation: the new threshold compares the same union.

Concretely:

```python
workable = sum(
    sum(1 for _ in board.iter_tasks(section=s))
    for s in ("Active", "Ready", "Backlog")
)
if workable >= _trigger_task_count():
    return
```

Pipeline Pending and Frozen still don't count (they don't represent "the operator has plenty of work queued" the same way Backlog does).

### Active still implicitly hard-gated

When Active is non-empty (typical: 1 task in flight), `workable >= 1`. With default threshold 3, a 1-active-task board with empty Backlog would have `workable=1`, which is below 3 — so ideation could in principle fire mid-task. That's the same SDK-contention concern TB-159 addresses for forced ideation: concurrent task-agent + control-agent runs in `_main_tick_loop` are risky.

**Decision:** keep Active as a HARD gate independent of the threshold — if Active is non-empty, ideation skips regardless of count. The threshold applies to Ready + Backlog only:

```python
if any(board.iter_tasks(section="Active")):
    return  # hard SDK-contention gate
queued = sum(
    sum(1 for _ in board.iter_tasks(section=s))
    for s in ("Ready", "Backlog")
)
if queued >= _trigger_task_count():
    return
```

This matches the historical behavior (Active=non-empty → skip) while making the queue-depth threshold tunable.

### Env knob parsing

Mirror `_cooldown_s` (`ap2/ideation.py:52-60`): read `AP2_IDEATION_TRIGGER_TASK_COUNT`, attempt int parse, fall back to default on invalid / unset. A negative or zero value is illegal — fall back to default and log a warning event (or just default silently, matching `_cooldown_s`'s permissive style).

### Migration / backwards compatibility

Default of 3 means projects that don't set the env knob behave DIFFERENTLY than today (was: skip on any Backlog item; now: skip when ≥3 queued items). This is intentional — the new behavior is what the prompt already expects. Operators wanting the old "fire only on truly empty board" behavior set `AP2_IDEATION_TRIGGER_TASK_COUNT=1`. Document this in the env-knob reference.

## Verification

- `uv run pytest -q ap2/tests/` — full regression gate passes.
- `grep -nE "AP2_IDEATION_TRIGGER_TASK_COUNT" ap2/ideation.py` — env knob is read in the ideation module.
- `grep -nE "IDEATION_TRIGGER_TASK_COUNT_DEFAULT" ap2/ideation.py` — module constant is defined.
- `python3 -c "from ap2.ideation import IDEATION_TRIGGER_TASK_COUNT_DEFAULT; assert IDEATION_TRIGGER_TASK_COUNT_DEFAULT == 3"` — default is 3.
- `grep -qE "AP2_IDEATION_TRIGGER_TASK_COUNT" ap2/architecture.md ap2/howto.md` — operator-facing doc mentions the new knob (at least one of the two files).
- prose: a test in `test_ideation*.py` constructs a Board with 2 Backlog tasks and 0 Active/Ready/Pipeline-Pending — with `AP2_IDEATION_TRIGGER_TASK_COUNT` unset (default 3), ideation fires (spy on `_run_control_agent`); with `AP2_IDEATION_TRIGGER_TASK_COUNT=1`, ideation skips.
- prose: a test pins the Active hard-gate independence — Board with 1 Active task and 0 Ready/Backlog, threshold default 3 — ideation skips because Active is non-empty (NOT because count < 3).
- prose: a test pins the threshold count semantics — `_trigger_task_count` reads the env knob, parses int, falls back to default on invalid (e.g. `"abc"`, `"-1"`, `""`) — same permissive behavior as `_cooldown_s`.
- prose: a test exercises the boundary — Board with EXACTLY threshold-many Ready+Backlog items causes skip (`>=` semantics, not `>`). Board with threshold-minus-one items fires.

## Out of scope

- Splitting the threshold into separate Ready and Backlog counts. Combined Ready+Backlog mirrors the existing `has_work` aggregation; per-section thresholds are over-design for v1.
- Per-project per-section thresholds via the env (e.g. `AP2_IDEATION_TRIGGER_BACKLOG_COUNT` separate from a Ready count). Single combined env knob is enough.
- Lowering the prompt's "Propose new tasks ONLY if Backlog has fewer than 3 workable items" line in `ap2/ideation.default.md` to reference the env knob. The prompt's 3-cap is for the agent's per-cycle proposal LIMIT, not the daemon's trigger gate; they happen to share the value 3 today by coincidence and stay independently tunable.
- Auto-tuning the threshold based on observed completion rate. Manual env knob is enough for v1.
- Web UI surfacing of the current threshold. CLI / env file is enough.

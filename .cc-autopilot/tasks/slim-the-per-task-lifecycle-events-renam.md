# Slim the per-task lifecycle events: rename `task_start`→`task_solve`, fold `verify_passed`+`judge_call` into one terminal `task_verify`

Tags: #autopilot #observability #events #verify #volume #ux

## Goal

The per-task event sequence is high-volume and confusing to read after the
fact. For a task with K prose-bullets it emits:
`task_start` → `verify_passed` → `judge_call` ×K → `task_complete`
— and `verify_passed` is **misnamed**: its payload is `{command, exit_code}`,
i.e. it's the `AP2_VERIFY_CMD` regression-gate / shell command passing, NOT
"verification passed." It fires mid-stream *before* the prose judging, so a
reader sees "verify passed" and then K more `judge_call`s, which reads like a
contradiction. The verdict is scattered across K+2 events with a misleading
"passed" in the middle. (Observed on TB-379: `verify_passed` then three
`judge_call`s with a silent 5-min gap between.)

Collapse the lifecycle to three legible, parallel verbs — **solve → verify →
complete** — one event per phase, while *retaining* the per-bullet detail by
folding it into the verify event's payload. This both clarifies the sequence
AND reduces event volume (K+3 lifecycle events → 3).

Why now: the event history is already high-volume; this is a net reduction
that also fixes the misleading `verify_passed` naming and the
scattered-verdict read. Meta-infra observability, no focus anchor →
`--skip-goal-alignment`.

## Scope

- **Rename `task_start` → `task_solve`** at its emission in `daemon.run_task`.
  Update every consumer that matches on `task_start` (status renderer,
  `automation_stats` / `automation_status`, the `web_*` pages, attention
  detectors, the ideation events block, and tests) to recognize `task_solve`.
  Readers that scan `events.jsonl` history must accept BOTH `task_solve` (new)
  and `task_start` (pre-existing events are not rewritten) so historical
  analysis doesn't break.
- **Fold `verify_passed` + the per-bullet `judge_call` events into ONE
  terminal `task_verify` event**, emitted once when verification completes
  (after the regression/shell command AND all prose-bullet judging), just
  before `task_complete` (and before the verification-failed disposition on
  the failure path). Suggested payload — the per-bullet detail that was in
  each `judge_call` moves into `bullets[]`, so no information is lost:
  ```
  task_verify {
    task, verdict: pass|fail|partial,
    shell: "N/N", prose: "M/M",
    verify_cmd: {command, exit_code},
    bullets: [ {idx, kind: shell|prose, verdict} , ... ]
  }
  ```
- **Stop emitting** the top-level `verify_passed` and per-bullet `judge_call`
  events. Migrate their consumers (e.g. the `judge_count` aggregator,
  `automation_stats`, the status-report verify/validator activity sections,
  `web`) to derive verification state from `task_verify`.
- **Do NOT add a top-level `judge_retry` (or any per-retry) event** — keep
  volume down. The judging phase is intentionally quiet; a `transient_retries`
  integer MAY be folded into `task_verify` as an optional field, but emit no
  per-retry events.
- **Failure-path symmetry**: emit `task_verify` in ALL outcomes (with
  `verdict: fail|partial` and the failing bullet(s) named), so it is the
  single terminal verification event regardless of result — don't leave
  failures on the old vocabulary while success uses the new one. The
  downstream task disposition (retry/freeze on fail, complete on pass) is
  unchanged.
- **Preserve verification behavior**: shell bullets still run; prose bullets
  still LLM-judged with the same retry logic; the verdict and disposition are
  unchanged. Only the EVENT emission changes — accumulate per-bullet results
  and emit one terminal event instead of streaming them.

## Design

- **Three lifecycle verbs, one event each**: `task_solve` → `task_verify` →
  `task_complete` (plus the existing `task_run_usage`). A K-prose-bullet task
  drops from K+3 lifecycle events to 3; per-bullet detail survives inside
  `task_verify.bullets`, so drill-down is preserved without per-bullet events.
- **One source of truth for "did it pass, and how."** Today the answer is
  stitched from a misnamed mid-stream `verify_passed` + K `judge_call`s + an
  inference from `task_complete`. `task_verify` is terminal and self-contained.
- **Volume-first trade-off, accepted**: no streaming/retry events during
  judging means a long judge phase is quiet in the log; the terminal summary
  (with the optional retry count) carries the outcome. This is the explicit
  preference — favor a compact history over real-time judging visibility.

## Verification

- `uv run --extra dev pytest -q ap2/tests/ --ignore=ap2/tests/smoke` — full suite passes, including the updated event-consumer tests.
- `grep -q "task_solve" ap2/daemon.py` — `run_task` emits `task_solve`.
- `! grep -qE "\"verify_passed\"|\"judge_call\"" ap2/daemon.py ap2/verify.py` — the old top-level verify_passed / per-bullet judge_call emissions are gone.
- `grep -q "task_verify" ap2/verify.py` — the terminal `task_verify` event is emitted from the verification path.
- New test: a task with mixed shell + prose verification emits exactly ONE `task_verify` carrying per-bullet verdicts (shell and prose) and an overall verdict, and emits NO `verify_passed` or per-bullet `judge_call`; `task_solve` precedes it and `task_complete` follows.
- New test: a failing prose bullet produces a `task_verify` with `verdict` fail/partial naming the bullet (failure-path symmetry), not the legacy events.
- `ap2/automation_status.py` / status renderer Prose: verification state on `ap2 status` / the status-report digest / web is derived from `task_verify`, and history scanners accept legacy `task_start` / `verify_passed` / `judge_call` events without error. Judge confirms via Read.

## Out of scope

- Rewriting historical `events.jsonl` (old event names remain; readers tolerate both old and new).
- Changing verification LOGIC, the prose-judge, or the retry behavior — only event emission/shape changes.
- Capturing the judge SDK subprocess stderr / any new retry-visibility event (explicitly declined here to keep volume down).

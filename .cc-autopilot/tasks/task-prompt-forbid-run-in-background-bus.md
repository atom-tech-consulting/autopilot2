# Task prompt: forbid run_in_background busy-poll loops; mandate foreground verification (daemon verifier owns the full suite)

Tags: #autopilot #prompts #robustness #task-agent #timeout

## Goal

Add execution-discipline guidance to the global task-agent prompt
(`build_task_prompt` in `ap2/prompts.py`) so task agents stop launching slow
commands with `run_in_background` and busy-polling the output file — the runaway
loop that exhausts the task timeout. Operator-filed meta-infra robustness fix; no
goal.md focus anchor (filed with `--skip-goal-alignment`).

Why now: a large refactor task recently froze when its agent ran the full
`pytest -q ap2/tests/` suite via `run_in_background` and then polled the output
file turn after turn (1500+ messages), hammering the API into `max_tokens` +
rate limits until the `claude` CLI subprocess exited 1 and the task timed out —
repeatedly, until retries exhausted and it Froze. A per-briefing note fixes one
task; the durable fix is one short paragraph in the shared task prompt so every
task agent gets it. If we delete this, the next big task repeats the freeze.

## Scope

- Add a short, explicit instruction block to `build_task_prompt` (`ap2/prompts.py`)
  that tells the task agent: run verification / test commands in the FOREGROUND and
  let them finish; do NOT launch them with `run_in_background` and poll the output
  file (it loops, balloons the run, and exhausts the timeout); iterate against
  TARGETED test files rather than re-running the full `ap2/tests/` suite; and note
  that the daemon's verifier runs the full `## Verification` suite after
  `report_result`, so the agent need not self-run the entire suite.
- Keep it concise (a few lines joining the existing prompt guidance); do not
  restructure the prompt.

## Design

- Single insertion into `build_task_prompt`; no behavioral/dispatch code change.
- Mirror the wording already added to the affected tasks' briefings so the guidance
  is consistent across the per-briefing notes and the global prompt.

## Verification

- `grep -qiE "run_in_background|foreground" ap2/prompts.py` — the task prompt carries the background-poll-avoidance / foreground-verification guidance.
- `uv run --extra dev pytest -q ap2/tests/ --ignore=ap2/tests/smoke` — the full suite stays green.
- `ap2/prompts.py` Prose: `build_task_prompt` instructs the task agent to run verification in the foreground and NOT to `run_in_background` + poll, and states the daemon verifier owns the full `## Verification` suite; judge confirms via Read.

## Out of scope

- Daemon-side detection / killing of runaway background-poll loops (a deeper
  mitigation; separate task).
- Changing the `## Verification` gate or how the daemon verifier runs it.
- The per-briefing execution notes (already applied to the affected tasks).

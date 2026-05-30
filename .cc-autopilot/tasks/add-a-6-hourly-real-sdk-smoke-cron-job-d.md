# Add a 6-hourly real-SDK smoke cron job dispatched through a dedicated routine

Tags: #autopilot #cron #real-sdk #smoke #verification #monitoring

## Goal

The real-SDK smokes (`ap2/tests/smoke/`) were just removed from the
per-task verification gate (`.cc-autopilot/env`:
`AP2_VERIFY_CMD ... --ignore=ap2/tests/smoke`, 2026-05-30) because they
make live Claude calls that intermittently error on transient service
blips and false-failed unrelated tasks (TB-345, TB-346). But the smokes
are still valuable — they're the only live-API canary that the SDK
wiring (cron_propose, pipeline_task_start, report_result, prose-judge,
validator-judge) actually round-trips against the real model. They need
to run on a schedule instead of on every task.

Add a cron job that runs the real-SDK smoke suite every 6 hours and
alerts the operator on failure. Because ap2 control/cron agents have no
Bash (`ap2/prompts.py` "control agents have no Bash"), this cannot be a
generic agent-prompt cron job — it must dispatch through a dedicated
Python routine, exactly like the `status-report` and `janitor` cron
jobs already do (`run_cron` in `ap2/daemon.py` branches on `job.name`).

Why now: the descope (2026-05-30) just pulled live-SDK coverage out of
the per-task gate to stop the flaky false-fails; without a scheduled
replacement, real SDK-wiring regressions would go completely undetected
until a human happened to run the smokes manually. A 6h cron restores
the canary out-of-band, deterministically (a periodic run that fails is
a real signal; a per-task run that fails is mostly noise). Operator-
directed 2026-05-30; meta-infra monitoring with no active focus, so
`--skip-goal-alignment`.

## Scope

- **New routine** (e.g. `ap2/smoke_runner.py::run_smoke_check(cfg)`),
  mirroring the shape of `ap2/status_report.py::run_status_report`:
  - If `AP2_REAL_SDK` is unset/falsey, emit a `smoke_check_skipped`
    event and return immediately (never run paid calls when the flag is
    off — keeps the job inert on installs that don't opt in).
  - Otherwise run the smoke suite as a subprocess —
    `uv run --extra dev pytest -q ap2/tests/smoke/` — in the project
    root with the daemon env (so `AP2_REAL_SDK=1` propagates), bounded by
    a timeout (reuse `AP2_VERIFY_TIMEOUT_S` or a dedicated cap; don't run
    unbounded in the tick loop).
  - Emit `smoke_check_passed` (with duration) on exit 0, or
    `smoke_check_failed` (with exit code + the captured failure tail) on
    non-zero / timeout.
  - On failure ONLY, post a concise alert to Mattermost via the same
    posting path `status_report` uses (channel from the existing
    config), naming the failing test(s) and the exit reason. Do NOT post
    on success — events.jsonl carries the pass record; a 6h "smokes OK"
    post would be noise alongside the 8h status-report.
- **Daemon dispatch** (`ap2/daemon.py` `run_cron`): add a
  `if job.name == "real-sdk-smoke":` branch that awaits the new routine
  and advances `cron_state["real-sdk-smoke"].last_run`, mirroring the
  existing `status-report` / `janitor` branches. The job's `prompt`
  field is an ignored stub (same as status-report).
- **Shipped default** (`ap2/cron.default.yaml`): add a `real-sdk-smoke`
  job with `interval: 6h` and a stub `prompt` whose body documents that
  the daemon dispatches it through `ap2.smoke_runner.run_smoke_check`
  (mirror the `status-report` stub's self-documenting comment). This
  seeds new projects; existing projects activate via the operator CLI
  (out of scope below).
- **Tests** (`ap2/tests/`): add a unit test that stubs the subprocess
  and asserts (a) exit 0 → `smoke_check_passed` event + no MM post;
  (b) non-zero → `smoke_check_failed` event + exactly one MM alert
  carrying the failure tail; (c) `AP2_REAL_SDK` unset →
  `smoke_check_skipped` + no subprocess spawned. Do NOT make a real SDK
  call in the test (stub the subprocess boundary). Add a dispatch test
  that `run_cron` routes `job.name == "real-sdk-smoke"` to the routine.

## Design

- **Routine, not agent.** Running pytest is a scheduled shell action,
  not an LLM task — and control agents lack Bash anyway. The
  `status-report` and `janitor` cron jobs already establish the
  "cron job body is a Python routine selected by `job.name`" pattern;
  this is a third instance of it, not a new mechanism.
- **Inert-by-default.** Gating on `AP2_REAL_SDK` means the job is a
  no-op (one skipped-event) on any install that hasn't opted into paid
  smokes, so shipping it in `cron.default.yaml` is safe for downstream
  OSS users who don't set the flag.
- **Failure-only alerting.** Events record every run for the audit
  trail; Mattermost only hears about failures. This matches the
  signal-discipline the status-report digest already follows.
- **Timeout-bounded.** The subprocess runs inside the main tick loop
  (like status-report's agent), so it must be timeout-bounded to avoid
  stalling ticks if the live SDK hangs.

## Verification

- `uv run --extra dev pytest -q ap2/tests/ --ignore=ap2/tests/smoke` — full suite (the descoped per-task gate) passes, including the new routine + dispatch tests.
- `grep -nE "real-sdk-smoke" ap2/cron.default.yaml` — the 6h smoke job ships in the default cron template.
- `grep -nE "interval:\s*6h" ap2/cron.default.yaml` — the shipped job's interval is 6 hours.
- `grep -nE "real-sdk-smoke" ap2/daemon.py` — `run_cron` has a name branch routing the job to the routine.
- `grep -rnE "smoke_check_failed|smoke_check_passed|smoke_check_skipped" ap2/` — the three outcome events are emitted by the routine.
- `ap2/daemon.py` Prose: `run_cron` dispatches `job.name == "real-sdk-smoke"` to the new smoke-check routine and advances that job's `cron_state[...].last_run`, mirroring the existing `status-report` / `janitor` branches rather than falling through to the generic agent-prompt path. Judge confirms via Read.
- `ap2/smoke_runner.py` Prose: `run_smoke_check` returns early with a `smoke_check_skipped` event when `AP2_REAL_SDK` is unset; otherwise runs `uv run --extra dev pytest -q ap2/tests/smoke/` as a timeout-bounded subprocess, emits `smoke_check_passed` / `smoke_check_failed`, and posts to Mattermost only on failure. (Module name may differ; judge confirms the routine exists with this behavior via Read/Grep.)

## Out of scope

- **Activating the job on THIS live project.** `.cc-autopilot/cron.yaml`
  is operator-fenced and cron mutation is operator-CLI-only (TB-146);
  the agent cannot add it. After this task lands and the daemon
  restarts (to pick up the new `run_cron` branch), the operator runs
  `ap2 cron edit add real-sdk-smoke --interval 6h --prompt "(stub)"`.
  Registering it before the daemon has the routine branch would dispatch
  it as a Bash-less agent and fail — so activation is deliberately a
  post-restart operator step, not part of this task.
- Surfacing `smoke_check_*` events in the 8h status-report digest —
  a possible follow-up, not required here.
- Changing the per-task gate descope (already done in
  `.cc-autopilot/env`) or the smoke tests' own assertions.

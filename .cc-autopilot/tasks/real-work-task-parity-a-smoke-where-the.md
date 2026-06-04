# Real-work task parity: a smoke where the agent edits a file, runs a command, commits, and reports a real commit SHA — both backends

Tags: #autopilot #agent-adapter #codex #tests #smoke #parity #real-work #commit #axis-7

## Goal

This advances **Current focus: codex support through an agent adaptor
layer** toward full backend parity. Every existing task-agent smoke uses a
trivial "don't do anything, just call the tool" prompt with `commit=""` and
`files_changed=""`. So **no smoke validates an agent actually doing work** —
editing a file, running a command, and producing a real commit SHA — for
either backend, and `AgentResult.commit` extraction is entirely unexercised
live. For codex specifically the write path (`Sandbox.workspace_write`) is
untested: the dispatch smoke (TB-372) even pins a read-only sandbox. "Codex
can do a real task" is unproven; we have only proven "codex can call a tool."

Add a real-work smoke, parametrized over both backends, where the agent
makes a real change in a temp git repo, commits it, and reports the actual
commit SHA back through the adapter.

Why now: parity means codex can drive the `task` kind end-to-end, including
filesystem writes + commit — the actual job of a task agent. This is the
last task-kind parity gap after TB-374 (tool calls) and TB-373 (tool
delivery). Operator-directed 2026-06-04. Builds on TB-374's `_adapter.py`
helpers.

## Scope

- **Add a real-work task smoke** (e.g. `test_task_real_work_real_sdk.py`),
  parametrized over `BACKENDS`, that in a temporary git repo asks the agent
  to make a small concrete change (edit/create a file, optionally run a
  command), commit it, and call `report_result` with the real commit SHA and
  a non-empty `files_changed`. Dispatch through `select_adapter("task", cfg)`
  + the adapter's streaming `run` (the production path), under
  `force_backend(..., "task", backend)`.
- **For codex, use a writable sandbox** (`Sandbox.workspace_write` /
  the adapter's write-enabled option) — NOT the read-only sandbox the
  dispatch smoke uses — so the write path is actually exercised.
- **Assert real-work outcomes for BOTH backends**: the file change exists in
  the temp repo, a commit was created, and the `report_result` round-trip /
  `AgentResult.commit` carries the **actual** commit SHA (not empty), with a
  non-empty `files_changed`.
- **Preserve the opt-in posture**: `AP2_REAL_SDK` skip marker +
  `gate_backend` codex `importorskip`; runs on the 6h cron. Bound cost by
  keeping the change small (one file, one commit), and use the
  `call_with_transient_retry` wrapper.
- **No `git push`** — the smoke commits locally in a temp repo only; never
  pushes (mirror the task-agent `disallowed_tools` posture).

## Design

- **Proves the actual job.** A task agent's purpose is to change code and
  commit; asserting a real commit SHA round-trips is the only check that the
  full task path — write, command, commit, result capture, usage — works on
  a backend. Tool-call-only smokes can't catch a backend that calls
  `report_result` fine but can't actually edit files or surface a commit.
- **Codex write path is the point.** Forcing `workspace_write` exercises the
  codex sandbox/approval mapping the dispatch smoke deliberately avoided —
  the parity-critical difference between "codex echoes text" and "codex does
  work."
- **Reuse the harness + production dispatch.** Route through
  `select_adapter("task")` + streaming `run` so the smoke matches how
  `daemon.run_task` actually dispatches, and reuse `_adapter.py`'s
  parametrization + codex gate.

## Verification

- `uv run --extra dev pytest -q ap2/tests/ --ignore=ap2/tests/smoke` — full suite passes; the new smoke skips by default (confirms collection/imports are clean).
- `test -f ap2/tests/smoke/test_task_real_work_real_sdk.py` — a real-work task smoke exists.
- `grep -qE "BACKENDS|parametrize" ap2/tests/smoke/test_task_real_work_real_sdk.py` — it is backend-parametrized.
- `grep -qiE "workspace_write|write" ap2/tests/smoke/test_task_real_work_real_sdk.py` — it exercises a writable codex sandbox, not read-only.
- `ap2/tests/smoke/test_task_real_work_real_sdk.py` Prose: a backend-parametrized real-work smoke dispatches through `select_adapter("task")` + the adapter's streaming `run`, has the agent make a real file change and commit in a temp git repo, and asserts for BOTH the claude and codex backends that the change exists, a commit was created, and the round-tripped `AgentResult.commit` / report_result args carry the actual (non-empty) commit SHA and a non-empty `files_changed`; codex uses a writable sandbox; no `git push` occurs; the codex variant skips cleanly when `AP2_REAL_SDK` is unset or the codex handle is unavailable. Judge confirms via Read.

## Out of scope

- Running the live smoke (operator-owned; `AP2_REAL_SDK=1` + real credentials).
- The skip-masking guard, judge parity, and control-agent parity (sibling tasks).
- Changing `run_task`, the AgentAdapter contract, the commit-extraction logic, or production dispatch.

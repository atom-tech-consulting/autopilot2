# Control-agent parity: live real-SDK smokes for ideation (incl. board_edit), ideation_scrub, status_report, cron, mattermost — both backends

Tags: #autopilot #agent-adapter #codex #tests #smoke #parity #control-agents #axis-7

## Goal

This advances **Current focus: codex support through an agent adaptor
layer** toward full backend parity. Five backend-selectable kinds in
`ap2/adapters/select.py:AGENT_KINDS` — `ideation`, `ideation_scrub`,
`status_report`, `cron`, `mattermost` — have **no real-SDK smoke on either
backend**. They were routed through the adapter (axis-6 migrations TB-360 /
TB-365) but never live-validated, so there is no proof a codex-backed (or
even a claude-backed, end-to-end) ideation/scrub/status/cron/mattermost
agent actually produces its expected output. Notably `ideation`'s
`board_edit` propose path — how proposals reach the board — is exercised by
no live smoke at all.

Add backend-parametrized live smokes for each control-agent kind so each is
proven on **both** claude and codex.

Why now: codex is live-validated for only 1 of 9 agent kinds; these five are
the largest remaining parity gap, and `board_edit` over the new codex stdio
bridge (TB-373) is completely unexercised live. Operator-directed
2026-06-04 ("the goal is not done until full parity"). Builds on TB-374's
`_adapter.py` parametrization helpers and TB-373's stdio tool bridge.

## Scope

- **Add a real-SDK smoke per control kind**, each parametrized over
  `BACKENDS` and routed through the adapter seam via `select_adapter(<kind>)`
  / the kind's production dispatch under `force_backend`:
  - **`ideation`** — asserts the agent invokes `board_edit` to propose a
    task (the propose path), capturing the structured args; covers the
    load-bearing untested tool.
  - **`ideation_scrub`** — asserts the scrub kind runs and returns its
    expected scrubbed output shape.
  - **`status_report`** — asserts the status-report agent produces a report
    (and, if applicable, the `status_report_run` tool round-trip).
  - **`cron`** — asserts the cron control agent runs and produces its
    expected output / proposal.
  - **`mattermost`** — asserts the mattermost handler kind runs and produces
    its reply output (the `mattermost_reply` path).
- **Each asserts a real behavior, not just "non-empty"**: a specific tool
  call with expected args, or the kind's expected structured result — for
  BOTH backends.
- **Preserve the opt-in posture**: `AP2_REAL_SDK` skip marker +
  `gate_backend` codex `importorskip`, so defaults skip and the 6h cron runs
  them. Use the established `call_with_transient_retry` skip-on-transient
  wrapper.
- **Trivial prompts to bound cost** (the established smoke convention):
  isolate the dispatch + tool/output wiring from heavy reasoning.

## Design

- **Parity = every selectable kind proven on codex.** A kind that's
  adapter-routed but never live-exercised is "selectable" on paper only;
  these smokes make each genuinely usable on codex (and pin the claude path
  end-to-end too, which today is also unproven for these five).
- **`board_edit` is the priority within `ideation`.** It is how ideation's
  output reaches the board and is the most consequential untested tool over
  the codex stdio bridge — the ideation smoke must exercise it specifically.
- **Reuse the harness.** `_adapter.py` (`BACKENDS` / `gate_backend` /
  `force_backend` / `extract_tool_calls`) supplies parametrization, the
  codex opt-in gate, and backend-neutral tool-call extraction.

## Verification

- `uv run --extra dev pytest -q ap2/tests/ --ignore=ap2/tests/smoke` — full suite passes; the new smokes skip by default (confirms collection/imports are clean).
- `test -f ap2/tests/smoke/test_ideation_real_sdk.py` — an ideation control-agent smoke exists. (Sibling files for the other kinds pinned by the prose bullet.)
- `grep -qE "board_edit" ap2/tests/smoke/test_ideation_real_sdk.py` — the ideation smoke exercises the board_edit propose path.
- `ap2/tests/smoke/` Prose: backend-parametrized real-SDK smokes exist for each of `ideation` (asserting a `board_edit` propose call), `ideation_scrub`, `status_report`, `cron`, and `mattermost`; each routes through `select_adapter(<kind>)` / the kind's production dispatch, asserts a specific tool call or structured result for BOTH the claude and codex backends, and the codex variant skips cleanly when `AP2_REAL_SDK` is unset or the codex handle is unavailable. Judge confirms via Read.

## Out of scope

- Running the live smokes (operator-owned; `AP2_REAL_SDK=1` + real credentials).
- The skip-masking guard, judge parity, and task-real-work parity (sibling tasks).
- Changing the control-agent kinds' logic, the AgentAdapter contract, or production dispatch.

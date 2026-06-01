## Goal

Advance **Current focus: codex support through an agent adaptor layer** by
migrating `run_task` (the task-agent dispatch site, `ap2/daemon.py:100`) behind
the `AgentAdapter` seam — the `run_task` step in goal.md's axis-6 migration
order (L177-183). This moves the `task` agent kind (declared in `AGENT_KINDS`,
`ap2/adapters/select.py` L45) onto adapter-routed, per-kind backend-selectable
dispatch, advancing the Progress signal "Every dispatch site (task, control,
verifier-judge, ideation-scrub, validator-judge, janitor-judge) runs through
the adapter".

Why now: `run_task` is the highest-traffic dispatch site (every task agent
runs through it) and the prerequisite for the mixed-config end-to-end Progress
signal (`task=codex`, goal.md L211-213) — codex can never drive a task agent
until this site is adapter-routed. The MCP-tool-registration-through-the-adapter
path already exists in HEAD (axis 3), as do `select_adapter`, `AgentOptions`,
and the streaming `AgentAdapter.run`, so the repoint is templated rather than
greenfield; deferring it leaves the loop's busiest site assuming the Claude
stream shape, which compounds every cycle.

## Scope

- Repoint `run_task` (`ap2/daemon.py`) from constructing
  `sdk.ClaudeAgentOptions(...)` (daemon.py:218) and consuming
  `async for msg in sdk.query(...)` (daemon.py:216) to resolving
  `select_adapter("task", cfg)` and driving the streaming
  `adapter.run(prompt, tools, options)`.
- Register the task agent's full MCP toolset (the `mcp_server` passed into
  `run_task`) through the adapter's tool-registration path so the same toolset
  is exposed regardless of backend.
- Preserve every dispatch parameter and behavior verbatim: `permission_mode`,
  `max_turns`, timeout, `model`, `effort`, the per-message logging /
  `_log_message` handling, usage/cost capture, and the commit/result
  extraction — all reading the adapter's normalized event stream + `AgentResult`.

## Design

Build a backend-neutral `AgentOptions` instead of `sdk.ClaudeAgentOptions`,
resolve via `select_adapter("task", cfg)` (default `ClaudeCodeAdapter()`
fallback on the cfg-less seam so hermetic tests stay deterministic), and drive
the streaming `adapter.run(...)`, mapping the daemon's existing per-message
handlers onto the adapter's normalized `AgentEvent` stream and reading the
terminal `AgentResult` for usage / commit. The in-HEAD `ap2/ideation_scrub.py`
dispatch is a one-shot in-tree example of the `select_adapter` resolution
pattern; this site uses the streaming `run` rather than `run_to_result` because
`run_task` consumes the message stream live. Register the task toolset through
the adapter's tool-registration entry point that axis 3 already shipped.

## Verification

- `uv run pytest -q ap2/tests/test_daemon_recovery.py` — the task-dispatch path tests pass against the adapter-routed `run_task`.
- `grep -q "select_adapter" ap2/daemon.py` — `run_task` resolves the task-agent backend through the per-kind selector.
- `ap2/daemon.py` Prose: `run_task` resolves `select_adapter("task", cfg)` and drives the streaming `adapter.run(...)` instead of calling `sdk.query` directly, registers the full MCP toolset through the adapter, and preserves permission_mode / max_turns / timeout / model / effort plus the per-message logging and commit/usage extraction; judge confirms via Read.
- `ap2/daemon.py` Prose: after this migration `run_task` no longer calls `sdk.query`, and the only remaining direct `sdk.query` call in `ap2/daemon.py` is inside `_run_control_agent` (its migration is the next sequenced axis-6 task); judge confirms via Grep/Read.
- `uv run pytest -q ap2/tests/ --ignore=ap2/tests/smoke` — the full unit suite stays green.

## Out of scope

- Migrating `_run_control_agent` (ideation / status_report / cron / mattermost)
  — the next and final sequenced axis-6 TB.
- Changing the task agent's prompt, tool policy, retry semantics, or
  verification — this is a pure dispatch relocation behind the interface.
- Adding a Codex task-agent smoke or the mixed-config end-to-end test — those
  land after `_run_control_agent` migrates and are tracked separately.

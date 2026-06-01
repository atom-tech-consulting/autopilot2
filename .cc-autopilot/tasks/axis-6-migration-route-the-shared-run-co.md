## Goal

Advance **Current focus: codex support through an agent adaptor layer** by
migrating the shared `_run_control_agent` (`ap2/daemon.py:1057`) behind the
`AgentAdapter` seam — the final `_run_control_agent` step in goal.md's axis-6
migration order (L177-183), which "unlocks per-kind selection for ideation,
status-report, cron, and the mattermost-handler". This moves the `ideation`,
`status_report`, `cron`, and `mattermost` agent kinds (all declared in
`AGENT_KINDS`, `ap2/adapters/select.py` L46-49) onto adapter-routed dispatch,
closing the Progress signal "Every dispatch site (task, control,
verifier-judge, ideation-scrub, validator-judge, janitor-judge) runs through
the adapter".

Why now: `_run_control_agent` is the last axis-6 dispatch site still on direct
`sdk.query`, and it fans out to four control kinds that are otherwise hardwired
to Claude; migrating it is what makes per-kind backend selection real for the
control surfaces (e.g. a cheap backend for cron/status-report, the strongest
for ideation). The seam it needs already exists in HEAD — `select_adapter`,
`AgentOptions`, the streaming `AgentAdapter.run` — so the repoint is templated;
completing it removes the last direct `sdk.query` dispatch from `ap2/daemon.py`
and is the prerequisite for the mixed-config end-to-end Progress signal
(goal.md L211-213).

## Scope

- Repoint `_run_control_agent` (`ap2/daemon.py`) from constructing
  `sdk.ClaudeAgentOptions(...)` (daemon.py:1141) and consuming
  `async for msg in sdk.query(...)` (daemon.py:1139) to resolving
  `select_adapter(<control_kind>, cfg)` for the specific control kind the call
  is running and driving the streaming `adapter.run(prompt, tools, options)`.
- Select the adapter per the control kind being dispatched — `ideation`,
  `status_report`, `cron`, or `mattermost` — so each control surface is
  independently backend-selectable (the kind is already threaded into
  `_run_control_agent`; pass it to `select_adapter`).
- Preserve each control kind's exact tool policy, `permission_mode`,
  `max_turns`, `model`, `effort`, the per-message detail logging
  (daemon.py:1089), and usage/cost capture — all reading the adapter's
  normalized event stream + `AgentResult`.

## Design

Build a backend-neutral `AgentOptions` instead of `sdk.ClaudeAgentOptions`,
resolve via `select_adapter(<control_kind>, cfg)` with a default
`ClaudeCodeAdapter()` fallback on the cfg-less seam so hermetic tests stay
deterministic, and drive the streaming `adapter.run(...)`, mapping the existing
per-message handlers onto the adapter's normalized `AgentEvent` stream and
reading the terminal `AgentResult` for usage. The control kind is already known
at the `_run_control_agent` call boundary (it distinguishes
ideation/status_report/cron/mattermost today); thread that kind into
`select_adapter` so each surface resolves its own backend. The in-HEAD
`run_task` adapter-routing (migrated in the predecessor) and
`ap2/ideation_scrub.py` are in-tree examples of the streaming and one-shot
shapes respectively.

## Verification

- `uv run pytest -q ap2/tests/test_control_run_usage.py ap2/tests/test_concurrent_mm.py ap2/tests/test_ideation_halt.py` — the control-agent dispatch tests pass against the adapter-routed `_run_control_agent`.
- `grep -q "select_adapter" ap2/daemon.py` — `_run_control_agent` resolves its per-kind backend through the selector.
- `! grep -nE "sdk\.query\(" ap2/daemon.py` — with `run_task` (predecessor) and `_run_control_agent` both migrated, `ap2/daemon.py` has no remaining direct `sdk.query` dispatch.
- `ap2/daemon.py` Prose: `_run_control_agent` resolves `select_adapter(<control_kind>, cfg)` for the ideation / status_report / cron / mattermost kind it is running and drives the streaming `adapter.run(...)` instead of `sdk.query`, preserving each kind's tool policy / max_turns / model / effort and the per-message logging; judge confirms via Read.
- `uv run pytest -q ap2/tests/ --ignore=ap2/tests/smoke` — the full unit suite stays green.

## Out of scope

- The mixed-config end-to-end test (`ideation=claude`, `task=codex` end-to-end,
  goal.md L211-213) — tracked separately once this and `run_task` have landed.
- Removing the residual `import claude_agent_sdk` at the daemon-start auth-gate
  probe / handle-construction sites — those are not `sdk.query` dispatch and
  any cleanup is a separate concern.
- Changing any control kind's prompt, tool policy, cadence, or semantics — this
  is a pure dispatch relocation behind the interface.

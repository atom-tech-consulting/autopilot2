## Goal

This task lands axis (3) of **Current focus: codex support through an agent
adaptor layer**. ap2's custom MCP tools (`report_result`, `cron_propose`,
`pipeline_task_start`, the prose judge, and the other registered tools) register
through the `AgentAdapter` from TB-353 so both backends expose the same toolset.
Axis-3 delete-test: "if tools stay Claude-MCP-specific, a Codex agent can't
report results and the loop breaks." This builds on the `AgentAdapter` ABC from
TB-353 (hard predecessor).

Why now: the custom tools are wired today via `create_sdk_mcp_server` directly
at the dispatch sites; until tool registration moves behind the adapter, the
Codex adapter (axis 4) has no way to expose `report_result` and the agent loop
cannot close on a non-Claude backend.

## Scope

- Add a tool-registration surface to the `AgentAdapter` interface: the adapter
  accepts the ap2 tool set and is responsible for exposing it to its backend,
  plus a backend-agnostic way to enumerate the registered tool short-names.
- Move the `create_sdk_mcp_server` wiring for ap2's custom tools into
  `ClaudeCodeAdapter` so Claude tool exposure flows through the adapter rather
  than being assembled at each dispatch site.
- Keep the registered tool set identical (same tool short-names, same handlers)
  — `report_result`, `cron_propose`, `pipeline_task_start`,
  `operator_queue_append`, `mattermost_reply`, `log_event`, and whatever else
  the current dispatch wiring exposes.

## Design

- The interface's tool-registration surface is what axis 4's `CodexAdapter` will
  implement against; axis 7's parity test will assert both backends enumerate
  the same set, which is why the enumeration accessor is part of this task.
- This is a pure relocation of the existing Claude MCP wiring; the live tool
  inventory must not change. `ap2/tests/test_mcp_inventory.py` is an existing
  regression pin and must stay green.

## Verification

- `uv run --extra dev pytest -q ap2/tests/ --ignore=ap2/tests/smoke` — full
  suite (the descoped per-task gate) passes; the live Claude tool set is
  unchanged.
- `grep -q "create_sdk_mcp_server" ap2/adapters/claude_code.py` — the Claude MCP
  tool wiring now lives in the adapter.
- `uv run --extra dev pytest -q ap2/tests/test_agent_adapter.py` — the contract
  test asserts `ClaudeCodeAdapter` exposes the expected ap2 tool short-names
  through the adapter's tool-registration surface.
- `ap2/adapters/base.py` Prose: the `AgentAdapter` interface declares a
  backend-agnostic tool-registration / enumeration surface (the ap2 custom
  toolset is handed to the adapter, not assembled per-dispatch-site); judge
  confirms via Read.
- Prose: the set of ap2 MCP tool short-names registered through
  `ClaudeCodeAdapter` matches the set the current dispatch wiring exposes
  (`report_result`, `cron_propose`, `pipeline_task_start`, ...); judge confirms
  via Grep/Read that no tool was dropped or renamed.

## Out of scope

- The `CodexAdapter` tool wiring (axis 4) and per-kind selection / auth gate
  (axis 5).
- Repointing production dispatch sites' run() calls to the adapter (axis 6);
  this task only relocates the tool-registration wiring.
- Adding, removing, or renaming any MCP tool or changing any handler behavior.

# Deliver ap2's toolset to a live codex agent over stdio MCP so a codex task agent can call report_result (Level 1)

Tags: #autopilot #agent-adapter #codex #mcp-tools #axis-3 #stdio-mcp

## Goal

This advances **Current focus: codex support through an agent adaptor
layer** and closes the focus's **axis 3** delete-test, which is currently
**unmet**:

> goal.md axis 3: "ap2's custom tools … register through the adapter so
> both backends see the same toolset. Delete-test: if tools stay
> Claude-MCP-specific, a Codex agent can't report results and the loop
> breaks."

Today ap2 delivers its tools as an **in-process** SDK MCP server
(`ap2/tools.py:build_mcp_server` → `create_sdk_mcp_server(...)`), handed to
Claude via `ClaudeAgentOptions(mcp_servers={...})`. The real `openai_codex`
SDK exposes **no** in-process `mcp_servers` kwarg
(`CodexAdapter.run`, `ap2/adapters/codex.py` ~L560-565) — codex consumes
**external stdio** MCP servers via its own config. So a live codex agent
**cannot see `report_result`**, which means a codex-backed `task` kind
cannot complete an ap2 task: dispatch works (TB-372 proved live dispatch +
stream normalization), but the tool round-trip that lets an agent report
its outcome does not exist for codex. The tools "stayed Claude-MCP-specific"
— exactly the delete-test failure.

A key simplifier: `report_result`'s server handler is a **thin ack**
(`ap2/tools.py` `do_task_complete`: "No state mutation here; the daemon
owns the routing decision after the query returns"). The daemon captures
the real payload by walking the message stream
(`daemon.py:_log_message`, ~L162-182, matches `report_result` /
`mcp__autopilot__report_result` and reads `.name`/`.input`). So delivering
codex its tools is mostly a **transport** problem: advertise the same tool
catalog to codex over stdio and ack the call; the daemon extracts the
result from codex's (already-normalized) event stream — the
`CodexAdapter` already surfaces codex `mcpToolCall` notifications as
`AgentEvent`s (`ap2/adapters/codex.py` ~L195).

This is **Level 1**: keep the proven in-process Claude path unchanged; add
a stdio bridge for codex; share the one tool registry, the `AgentTools`
descriptor, and the `register_tools` interface across both backends. (Level
2 — moving Claude itself onto a shared stdio server and re-plumbing the
stateful tools — is explicitly out of scope.)

Why now: the codex focus was marked "complete" on a structural proxy (a
Complete TB per axis) while axis 3's functional delete-test was never
exercised — the parity test asserts only matching tool *names* over a stub
(`mcp_servers={"autopilot": object()}`) and the codex live smoke (TB-372)
makes no tool call. This is the work that makes codex a genuinely usable
**task** backend rather than a dispatch-only one. Operator-directed
2026-06-03; builds on the now-real `openai_codex` adapter (TB-372).

## Scope

- **Add a stdio MCP server entrypoint** (e.g. `ap2/mcp_stdio.py`, runnable
  as `python -m ap2.mcp_stdio --project <root>`) that builds the **same**
  tool set as `ap2/tools.py:build_mcp_server` and serves it over the
  official `mcp` package's stdio transport. Reuse the existing
  backend-neutral handlers (`do_task_complete`, `do_cron_propose`,
  `do_pipeline_task_start`, the prose-judge / git-log / log-insight
  handlers) as the single source of truth — do NOT fork or re-declare the
  tool definitions or their input schemas.
- **Wire CodexAdapter to deliver this stdio server to the live codex
  agent** via codex's own MCP configuration mechanism. Introspect the
  installed `openai_codex` (e.g. `CodexConfig` / `thread_start`
  parameters) to find the real config surface for registering an external
  stdio MCP server; configure it with the `python -m ap2.mcp_stdio …`
  launch command for the run's project root. Do NOT invent SDK
  symbols — every attribute used must exist on the installed module
  (the prior CodexAdapter shipped against a fabricated API; do not repeat
  that). Route this through `CodexAdapter.register_tools` /
  `build_tool_server` so the existing adapter tool-policy seam is the wiring
  point.
- **Extend the daemon's result capture to the codex stream**
  (`ap2/daemon.py`, the `run_task` stream-walk / `_log_message`): capture
  `report_result` (and `pipeline_task_start`) tool-call args from the
  codex-normalized `AgentEvent` stream (the `mcpToolCall` events the
  `CodexAdapter` emits), the same way it captures them from the Claude
  stream, so a codex task agent's `report_result` call round-trips into a
  valid `TaskResult` via `_task_result_from_tool_args`.
- **Keep the Claude in-process path unchanged.** This task adds a second
  transport (stdio) for codex; it does not move Claude off
  `create_sdk_mcp_server`. The shared surface is the one tool registry +
  `AgentTools` + `register_tools`.
- **Hermetic tests** (`ap2/tests/`): (a) the stdio server advertises the
  IDENTICAL tool short-name set as `build_mcp_server` (single source of
  truth); (b) a `report_result` call exercised through the stdio server
  returns the same ack shape as the in-process handler; (c) the daemon
  builds a complete `TaskResult` from a codex-shaped `mcpToolCall`
  `AgentEvent` carrying `report_result` args. All hermetic — no network, no
  credentials, no live codex.

## Design

- **One registry, two transports.** The tool *handlers* are
  backend-neutral plain functions; only the *assembly* differs
  (`create_sdk_mcp_server` for the in-process Claude path vs the `mcp`
  package's stdio `Server` for codex). The bridge re-wraps the same
  handlers — there is exactly one definition of each tool and its schema.
- **report_result is a transport problem, not a state problem.** Because
  the handler is a thin ack and the daemon reads the call off the stream,
  the bridge only has to advertise + ack; the daemon's stream-walk
  (extended to codex's `mcpToolCall` events) does the capture. No new
  daemon↔tool state channel is required for the task-completion path.
- **Level 1 keeps Claude stable.** Claude stays in-process (lowest latency,
  no subprocess, tools close over the live `cfg`). Only codex gets the
  stdio transport. The `AgentAdapter.register_tools` interface is the
  shared seam both backends already flow through.
- **No fabricated SDK surface.** The CodexAdapter's external-MCP config
  must be written against the actually-installed `openai_codex` API,
  verified by introspection — the same discipline TB-372 had to restore.

## Verification

- `uv run --extra dev pytest -q ap2/tests/ --ignore=ap2/tests/smoke` — full suite passes, including the new stdio-bridge + codex-result-capture hermetic tests.
- `grep -rqE "stdio" ap2/mcp_stdio.py` — a stdio MCP server entrypoint module exists.
- `ap2/mcp_stdio.py` Prose: a standalone stdio MCP server entrypoint serves the SAME tool set as `ap2/tools.py:build_mcp_server` by reusing the existing neutral handlers (no forked tool definitions), advertising the identical tool short-name set. Judge confirms via Read.
- `ap2/adapters/codex.py` Prose: `CodexAdapter` registers ap2's tools with a live codex agent by configuring an external stdio MCP server (the `python -m ap2.mcp_stdio` launch command) through codex's real `openai_codex` configuration surface — using only attributes that exist on the installed module, with no fabricated SDK symbols — wired through `register_tools` / `build_tool_server`. Judge confirms via Read.
- `ap2/daemon.py` Prose: `run_task` captures `report_result` tool-call args from the codex-normalized `AgentEvent`/`mcpToolCall` stream into a valid `TaskResult` (via `_task_result_from_tool_args`), mirroring the existing Claude stream-walk; the Claude in-process tool path is unchanged. Judge confirms via Read.
- New hermetic test asserts the stdio server's advertised tool short-names equal `build_mcp_server`'s, and that a codex-shaped `mcpToolCall` event carrying `report_result` args produces a `complete` `TaskResult`.

## Out of scope

- **Running a live credentialed codex task end-to-end** — operator-owned, cannot be verified unattended (TB-122 `Manual:` trap). The sibling smoke-parametrization task ships the structure; the operator runs the live round-trip.
- **Level 2 consolidation** — moving the Claude path off the in-process server onto the shared stdio server, and the cfg/effect-channel re-plumbing that the stateful tools (`cron_propose`, `pipeline_task_start`, `status_report`, mattermost) would then require. This task keeps Claude in-process.
- Per-message backend routing or a third backend (respects goal.md backend constraints).
- Changing the AgentAdapter contract or the Claude adapter's behavior.

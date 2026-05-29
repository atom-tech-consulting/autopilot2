# Upgrade claude-agent-sdk 0.1.71 → 0.2.x (unblock Opus 4.8 thinking-block round-trip)

Tags: #autopilot #sdk #dependency #infrastructure #opus-4-8 #thinking-blocks

## Goal

The bundled `claude_agent_sdk` is pinned-by-omission at **0.1.71**
(unpinned in `pyproject.toml` L10 — whatever resolved at install
time); PyPI latest is **0.2.87**. The stale 0.1.x SDK predates Opus
4.8 and mishandles the extended-thinking + tool-use round-trip
contract: switching the daemon's agent model to `claude-opus-4-8[1m]`
(2026-05-29) produced deterministic `400 invalid_request_error`
failures — `messages.N.content.M: thinking or redacted_thinking
blocks in the latest assistant message ...` — on substantial
multi-turn task-agent runs (observed on TB-340, a multi-file fix).
4.7 is unaffected because 0.1.71 was built in its era; 4.8's
thinking-block serialization needs the 0.2.x handling.

Upgrade the SDK across the 0.1→0.2 minor boundary so Opus 4.8 (and
future models) round-trip thinking blocks correctly, unblocking the
operator-requested 4.8 switch. The bundled `claude` CLI (currently
2.1.123, spawned by the SDK) comes along with the 0.2.x wheel.

Why now: the 4.8 model switch the operator asked for is blocked by
this — every substantial task dispatched on 4.8 risks the
thinking-block 400 → retry_exhaustion → Frozen. The SDK bump is the
true prerequisite for 4.8 (the model switch was done first, cart
before horse); landing it lets 4.8 be re-enabled cleanly.
Operator-directed 2026-05-29; meta-infra dependency work with no
active focus → `--skip-goal-alignment`.

## Scope

- `pyproject.toml` L10 — pin `claude-agent-sdk` to a specific 0.2.x
  version (target the latest verified at authoring time, e.g.
  `claude-agent-sdk>=0.2.87,<0.3` — confirm the exact current latest
  via `curl -s https://pypi.org/pypi/claude-agent-sdk/json` and pin
  the floor to it). Pinning (vs leaving unbounded) prevents a silent
  future 0.3 jump from re-breaking the daemon.
- Reconcile breaking changes across the 0.1→0.2 boundary at every
  SDK consumer. The `ClaudeAgentOptions` construction + `async for
  msg in sdk.query(prompt=..., options=...)` loop appears in FOUR
  sites — they must all stay consistent:
  - `ap2/daemon.py` L215, L893 (task-agent + control-agent dispatch)
  - `ap2/verify.py` L593, L609 (prose-bullet judge)
  - `ap2/ideation_scrub.py` L321, L328 (exhaustion-language scrub)
  - `ap2/components/janitor/__init__.py` L785, L794 (per-finding judge)
  - Plus the MCP server creation in `ap2/tools.py` L677
    (`from claude_agent_sdk import create_sdk_mcp_server, tool`).
- Verify the `extra_args` plumbing still works against the new
  bundled CLI: ap2 passes `--effort`, `--model`, `--max-turns`,
  `--disallowedTools`, `--setting-sources`, `--permission-mode`,
  `--mcp-config`, `--input-format`/`--output-format stream-json` via
  the SDK's extra-args mechanism (visible in the running task-agent
  argv). If the 0.2.x SDK renamed/restructured how options or
  extra_args map to CLI flags, update the call sites accordingly.
- Confirm the message-type surface the daemon iterates
  (`AssistantMessage` / `UserMessage` / `ResultMessage` /
  `ToolUseBlock` etc. used in `daemon._run_control_agent`'s message
  loop + `_log_message`) still matches; reconcile any renamed
  classes / moved imports.
- Update any vendored version assertion / probe
  (`adhoc/probe_opus47_1m.py` or similar) if it pins the SDK version
  string.

## Design

- **Consult the migration surface before editing.** The 0.1→0.2
  boundary likely changed `ClaudeAgentOptions` field names and/or the
  query interface. Read the installed 0.2.x package's
  `__init__.py` / type stubs after install to confirm the actual
  current shape rather than guessing; the `claude-api` skill captures
  the canonical SDK usage patterns and is the right reference.
- **The running daemon is insulated until restart.** `daemon.py`
  imports `claude_agent_sdk` at process start (L1532 lazy import in
  the dispatch path, L2427 import-probe). A bad bump caught by the
  pytest verify gate fails the task cleanly (commit gated on
  `AP2_VERIFY_CMD`); the live daemon keeps its already-imported
  0.1.71 SDK until an operator `ap2 stop && ap2 start`. So this task
  cannot brick the running daemon mid-flight — the new SDK only takes
  effect on the next deliberate restart. This is the safety margin
  that makes an autonomous SDK bump acceptable.
- **Keep the model knob untouched.** This task is the SDK bump ONLY.
  Re-enabling `AP2_AGENT_MODEL=claude-opus-4-8[1m]` is a separate
  operator action AFTER this lands + the daemon is restarted onto the
  new SDK and a smoke task confirms 4.8 round-trips thinking blocks.
  Do not edit `.cc-autopilot/env` (it's operator-owned + fenced).
- **AP2_REAL_SDK smokes.** This repo sets `AP2_REAL_SDK=1`, so the
  verify suite includes real-SDK smoke tests (`ap2/tests/smoke/`).
  Those exercise the actual `sdk.query` path and are the strongest
  signal that the 0.2.x interface still works end-to-end. They must
  pass.

## Verification

- `uv run pytest -q` — full suite passes against the upgraded SDK
  (includes the AP2_REAL_SDK smokes, which exercise the live
  `sdk.query` interface).
- `grep -qE "claude-agent-sdk>=0\.2\." pyproject.toml` — the SDK
  dependency is pinned to a 0.2.x floor (no longer unbounded).
- `python3 -c "import claude_agent_sdk, importlib.metadata as m; v=m.version('claude-agent-sdk'); print(v); assert v.startswith('0.2.'), v"`
  — the installed SDK resolves to a 0.2.x version after the bump.
- `python3 -c "from claude_agent_sdk import ClaudeAgentOptions, query, create_sdk_mcp_server, tool"`
  — the symbols ap2 imports still resolve from the 0.2.x package
  (catches a renamed/moved export).
- `! grep -rnE "sdk\.ClaudeAgentOptions\(" ap2/ --include="*.py" | grep -v "tests/" | grep -qvE "."` is NOT used (placeholder) — instead:
- `ap2/daemon.py` Prose: the task-agent + control-agent dispatch
  `sdk.query(...)` calls construct `ClaudeAgentOptions` with the
  0.2.x field shape (confirmed against the installed package), and
  the message-iteration loop handles the 0.2.x message/block types.
  Judge confirms via Read of the dispatch sites + the installed SDK
  surface.
- `ap2/verify.py` + `ap2/ideation_scrub.py` + `ap2/components/janitor/__init__.py`
  Prose: all three secondary `sdk.query` consumers use the same
  reconciled 0.2.x `ClaudeAgentOptions` shape as `daemon.py` — no
  site left on the 0.1.x interface. Judge confirms via Read.
- `pyproject.toml` Prose: only the `claude-agent-sdk` dependency line
  changed (pinned to 0.2.x); no unrelated dependency edits rode
  along. Judge confirms via Read of the diff.

## Out of scope

- Re-enabling `AP2_AGENT_MODEL=claude-opus-4-8[1m]` — separate
  operator action after this lands + daemon restart + a 4.8 smoke
  task confirms thinking-block round-trip. This task only bumps the
  SDK; it does not touch `.cc-autopilot/env` (operator-owned, fenced).
- The `ap2 ack roadmap_complete` semantics fix (TB-340) — independent
  bug, separate task.
- Upgrading other dependencies (`psutil`, `mistune`, `pyyaml`) — keep
  the diff to the one dependency that's blocking 4.8.
- Restarting the daemon — operator action; the new SDK takes effect
  on the next `ap2 stop && ap2 start`, deliberately decoupled from
  this task's commit.
- Adopting any NEW 0.2.x SDK features (e.g. new options, hooks,
  streaming modes) — this is a compatibility bump to fix the
  thinking-block contract, not a feature-adoption pass. New-feature
  adoption can be proposed separately once the bump is stable.

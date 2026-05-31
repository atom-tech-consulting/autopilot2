## Goal

Axis 4 of the **Current focus: codex support through an agent adaptor layer**.
Axes 1-3 landed the backend-agnostic seam — the `AgentAdapter` ABC plus the
`AgentOptions` / `AgentTools` / `AgentResult` / `AgentUsage` / `AgentEvent`
types (`ap2/adapters/base.py`, TB-353), the canonical options + normalized
usage record (TB-354), and the `build_tool_server` / `registered_tool_names`
tool-registration surface (TB-355). The interface today has exactly one
implementation, `ClaudeCodeAdapter` (`ap2/adapters/claude_code.py`,
`backend = "claude"`). Per goal.md's axis-4 delete-test: "an abstraction with
one implementation is no actual Codex support." This task adds the second
implementation: a `CodexAdapter` (`ap2/adapters/codex.py`, `backend = "codex"`)
that drives OpenAI's `codex` CLI agent through the same `AgentAdapter` contract
— prompt assembly, tool wiring via `build_tool_server` / `register_tools`,
stream normalization to `AgentEvent`s, and result/commit extraction into a
normalized `AgentResult` / `AgentUsage`, with timeout/turn bounding owned by the
base `run_to_result`.

Why now: the interface contract (axes 1-3) is freshly landed and has only the
Claude implementation, so the abstraction is unexercised by any second backend
— every downstream axis (5 per-kind selection, 6 migrations, 7 parity tests) is
blocked on a real Codex backend existing to select, route, and test against;
without it the seam is a shell with one caller.

## Scope

- Add `ap2/adapters/codex.py` declaring `CodexAdapter(AgentAdapter)` with
  `backend = "codex"`, implementing the three abstract methods
  (`normalize_options`, `register_tools`, `run`) plus `build_tool_server`,
  mirroring the structure of `ClaudeCodeAdapter`.
- `run()` is an async generator that drives the `codex` CLI agent and yields one
  normalized `AgentEvent` per backend stream envelope, then a terminal
  `AgentEvent(type="result")` carrying an `AgentResult` (status / text / commit /
  usage), so the base `run_to_result` drains it uniformly.
- Make the `codex` SDK/CLI handle injectable (constructor arg, lazy import when
  `None`) exactly as `ClaudeCodeAdapter` injects `sdk`, so the contract test can
  run hermetically against a stub with no live `codex` process.
- Re-export `CodexAdapter` from `ap2/adapters/__init__.py` (add the import + the
  `__all__` entry).
- Add a hermetic contract test `ap2/tests/test_codex_adapter.py` driving a
  stubbed codex handle through `run` / `run_to_result`.
- Keep all option/usage normalization reading the existing `AgentOptions` /
  `AgentUsage` shapes — no new fields on the base types; backend-specific kwargs
  ride `AgentOptions.extra`.

## Design

The codex adapter mirrors `ClaudeCodeAdapter`'s three-method shape:
`normalize_options` maps the backend-neutral `AgentOptions`
(model/effort/max_turns/cwd/...) onto the codex CLI's native invocation kwargs;
`register_tools` maps `AgentTools` (allow/deny + `mcp_servers`) onto codex's
tool-exposure surface; `build_tool_server` accepts ap2's custom tool set as a
unit and records the registered short-names on `self._registered_tool_names` so
the base `registered_tool_names()` enumerates them (axis 7 reads this to assert
toolset parity). `run()` consumes the codex stream and normalizes each envelope
to an `AgentEvent` via the same compact/full/text triple the Claude path
produces. Commit/usage extraction populates the normalized `AgentResult` /
`AgentUsage` so cost guards and `ap2 status` read one shape regardless of
backend.

## Verification

- `uv run pytest -q ap2/tests/test_codex_adapter.py` — new hermetic contract
  test for `CodexAdapter` passes (stubbed codex handle, no live process).
- `uv run python -c "from ap2.adapters import CodexAdapter; from ap2.adapters.base import AgentAdapter; a=CodexAdapter(); assert isinstance(a, AgentAdapter); assert a.backend == 'codex'"`
  — adapter conforms to the ABC and reports the `codex` backend id.
- `grep -q "class CodexAdapter" ap2/adapters/codex.py` — the implementation
  module exists.
- `grep -q "CodexAdapter" ap2/adapters/__init__.py` — re-exported from the
  package surface.
- `uv run pytest -q ap2/tests/test_agent_adapter.py` — the existing axis-1/2/3
  Claude contract suite still passes (zero regression to the shared base).
- `ap2/adapters/codex.py` Prose: `CodexAdapter.run` is an async generator
  yielding one `AgentEvent` per stream envelope plus a terminal `type="result"`
  event whose `.result` is an `AgentResult`; judge confirms via Read that the
  method shape matches the ABC's `run` / `run_to_result` contract in
  `ap2/adapters/base.py`.

## Out of scope

- Repointing any production dispatch site to `CodexAdapter` (that is axis 6, one
  TB per site, starting with the ideation-scrub canary).
- Per-agent-kind backend selection + the backend-aware auth gate (axis 5).
- A live `codex` real-SDK smoke (axis 7 — gated behind the 6h `real-sdk-smoke`
  cron).

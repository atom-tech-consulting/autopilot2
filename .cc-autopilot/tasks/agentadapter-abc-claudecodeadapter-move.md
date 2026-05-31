## Goal

This task lands axis (1) of **Current focus: codex support through an agent
adaptor layer**. The focus introduces a backend-agnostic `AgentAdapter`
interface every dispatch flows through. Axis 1 is the prerequisite for all
other axes: define the `AgentAdapter` ABC — `run(prompt, tools, options)`
yielding a normalized event stream and an `AgentResult(usage, commit, ...)` —
and relocate today's `claude_agent_sdk.query()` path into a `ClaudeCodeAdapter`
that wraps the current behavior bit-for-bit, with zero observable behavior
change. Per goal.md's axis-1 delete-test: "if the Claude path isn't behind the
interface, the Codex adapter has no contract to conform to."

Why now: every dispatch site (`run_task`, `_run_control_agent`,
`_judge_prose_bullet`, `_run_scrub`, the validator-judge / janitor-judge calls)
invokes the SDK directly today, and every future feature that assumes the
Claude stream shape makes the adaptor more expensive to retrofit; landing the
ABC now — while the SDK coupling is still concentrated — is the only way to
give axes 2-7 a contract to build against.

## Scope

- Add `ap2/adapters/base.py` declaring the `AgentAdapter` ABC: an async
  `run(prompt, tools, options)` that yields normalized stream events and
  returns a terminal `AgentResult(usage, commit, ...)`, plus the abstract
  surface goal.md axis 1 names (options-normalization entry, MCP-tool
  registration hook, result/usage shape). Keep the option/result/usage types
  minimal and forward-compatible — axes 2 and 3 harden them.
- Add `ap2/adapters/claude_code.py` with `ClaudeCodeAdapter(AgentAdapter)` that
  relocates the existing `claude_agent_sdk.query()` call path bit-for-bit:
  model / effort / max_turns / timeout → `ClaudeAgentOptions`; the
  `AssistantMessage` / `ResultMessage` stream → normalized events; usage / cost
  parsing preserved exactly.
- Add `ap2/adapters/__init__.py` re-exporting `AgentAdapter`,
  `ClaudeCodeAdapter`, and the option/result types.
- Add `ap2/tests/test_agent_adapter.py`: an adapter-contract test that
  instantiates `ClaudeCodeAdapter`, asserts it conforms to `AgentAdapter`, and
  round-trips a stubbed SDK stream into a normalized `AgentResult`/usage (no
  live SDK call).

## Design

- `AgentAdapter.run()` is the single seam: callers hand it a prompt, a tool
  set, and a normalized options object; it yields normalized stream events and
  returns an `AgentResult`. This task builds the seam plus the Claude
  implementation only.
- `ClaudeCodeAdapter` is the behavior reference — it must reproduce today's
  `claude_agent_sdk.query()` path exactly so the axis-7 parity tests have a
  ground truth.
- Production dispatch sites stay on their current direct-SDK path this task;
  axis 6 repoints them one TB at a time. Because no caller is repointed, the
  full suite proves zero behavior change.

## Verification

- `uv run --extra dev pytest -q ap2/tests/ --ignore=ap2/tests/smoke` — full
  suite (the descoped per-task gate) passes; existing dispatch sites are
  unchanged (zero behavior change).
- `test -f ap2/adapters/base.py && test -f ap2/adapters/claude_code.py` — the
  adapter package modules exist.
- `grep -q "class AgentAdapter" ap2/adapters/base.py` — the backend-agnostic
  ABC is declared.
- `grep -q "class ClaudeCodeAdapter" ap2/adapters/claude_code.py` — the Claude
  adapter is declared.
- `grep -q "claude_agent_sdk" ap2/adapters/claude_code.py` — the canonical
  `sdk.query` path is relocated into the adapter.
- `uv run --extra dev pytest -q ap2/tests/test_agent_adapter.py` — the
  adapter-contract test passes (ClaudeCodeAdapter conforms to AgentAdapter; a
  stubbed SDK stream round-trips to a normalized AgentResult/usage).
- `ap2/adapters/claude_code.py` Prose: `ClaudeCodeAdapter.run` wraps the current
  `claude_agent_sdk.query` invocation (options → `ClaudeAgentOptions`,
  `AssistantMessage`/`ResultMessage` stream → normalized events, usage/cost
  parsing) bit-for-bit; judge confirms via Read that no prompt, tool-policy, or
  verification semantics changed.

## Out of scope

- Migrating production dispatch sites (`run_task`, `_run_control_agent`, the
  judges, ideation-scrub) to the adapter — that is axis 6, one TB each.
- The `CodexAdapter` (axis 4), per-kind selection / auth gate (axis 5), and the
  codex real-SDK smoke (axis 7).
- Any change to prompts, tool policy, or verification semantics.

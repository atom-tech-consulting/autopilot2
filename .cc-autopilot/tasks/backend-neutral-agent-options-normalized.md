## Goal

This task lands axis (2) of **Current focus: codex support through an agent
adaptor layer**. Land a backend-neutral options struct (model, effort /
reasoning, max_turns, timeout) and a normalized `AgentResult` / usage record on
the `AgentAdapter` interface introduced by TB-353, so the cost guards, the
`task_run_usage` emission, and `ap2 status` read one shape regardless of
backend. This directly serves the Progress signal "usage / cost / `ap2 status`
read one normalized result shape across backends". Axis-2 delete-test: "if not
normalized, every consumer of usage / result branches per-backend." This builds
on the `AgentAdapter` ABC + `ClaudeCodeAdapter` from TB-353 (hard predecessor).

Why now: today usage / cost parsing is Claude-`ResultMessage`-specific and read
at multiple consumer sites; without a normalized record, adding the Codex
backend (axis 4) forces per-backend branching in every cost guard and status
surface — the exact coupling this focus exists to remove.

## Scope

- Define a backend-neutral `AgentOptions` dataclass (model, effort / reasoning,
  max_turns, timeout) consumed by `AgentAdapter.run`.
- Define a normalized `AgentResult` + usage record (input / output tokens,
  cost, commit, terminal status) independent of the Claude `ResultMessage`
  shape.
- Have `ClaudeCodeAdapter` (from TB-353) populate the normalized options +
  result/usage from the live SDK objects.
- Migrate the existing usage / cost consumers — the per-task / control
  `*_run_usage` emission, the cost guards, and the `ap2 status` usage read — to
  read the normalized fields rather than raw SDK objects. Preserve exact emitted
  values and event-payload keys.

## Design

- `AgentOptions` and `AgentResult` live on `ap2/adapters/base.py` next to the
  ABC so every consumer imports one neutral shape.
- `ClaudeCodeAdapter` is the mapping point: live `ClaudeAgentOptions` /
  `ResultMessage` in, normalized structs out. No consumer should import a
  Claude-SDK type for usage after this task.
- Keep payload keys and emitted numeric values identical so the change is a
  pure read-path relocation (the full suite is the regression pin).

## Verification

- `uv run --extra dev pytest -q ap2/tests/ --ignore=ap2/tests/smoke` — full
  suite (the descoped per-task gate) passes; emitted usage values and event
  payload keys unchanged.
- `grep -qE "class AgentOptions|class AgentResult" ap2/adapters/base.py` — the
  normalized options + result types are declared on the interface module.
- `uv run --extra dev pytest -q ap2/tests/test_agent_adapter.py` — the contract
  test asserts `ClaudeCodeAdapter` populates the normalized `AgentResult`/usage
  from a stubbed SDK `ResultMessage`.
- `ap2/adapters/claude_code.py` Prose: `ClaudeCodeAdapter` maps live
  `ClaudeAgentOptions` / `ResultMessage` usage + cost into the normalized
  `AgentOptions`/`AgentResult`; judge confirms via Read the mapping preserves
  the field values the prior raw-SDK reads produced.
- Prose: the `task_run_usage` / control-run usage emission and the cost-guard +
  `ap2 status` usage reads consume the normalized `AgentResult` fields (not raw
  SDK attributes); judge confirms via Grep/Read that no consumer branches on a
  Claude-SDK-specific type for usage.

## Out of scope

- The `CodexAdapter` itself (axis 4) and per-kind selection (axis 5).
- Migrating production dispatch sites' call paths to the adapter (axis 6); this
  task only relocates the usage/result READ shape its consumers depend on.
- Any change to emitted event names, payload keys, or numeric values.

## Goal

Axis 6 of the **Current focus: codex support through an agent adaptor layer** —
the first dispatch-site migration, sequenced as the canary because
ideation-scrub is the smallest, one-shot site (no MCP tools). `_run_scrub` in
`ap2/ideation_scrub.py` today calls `sdk.ClaudeAgentOptions(...)` +
`sdk.query(...)` directly (around L296). This task repoints it through the
`AgentAdapter` seam: build an `AgentOptions` + `AgentTools`, resolve the adapter
for the `ideation_scrub` kind via the per-kind selector (TB-358), and drive the
run through `adapter.run_to_result(...)`, preserving the scrub's exact behavior
on Claude (same model, same prompt, same single-shot result) while making the
kind independently backend-selectable. Per goal.md's axis-6 delete-test:
"migrate none and the adapter is a shell with no caller."

Why now: TB-353-355 landed the interface, TB-357 the codex backend, TB-358
per-kind selection — but no production dispatch site routes through the adapter
yet, so the whole abstraction has zero callers; the canary proves the migration
shape (preserve-Claude-behavior + per-kind-selectable) before the verifier-judge
/ validator-judge / run_task / control-agent sites follow.

## Scope

- Repoint `_run_scrub` (`ap2/ideation_scrub.py`) to dispatch through an
  `AgentAdapter` resolved for the `ideation_scrub` kind (TB-358's selector)
  instead of calling `sdk.ClaudeAgentOptions` / `sdk.query` directly: build
  `AgentOptions(model=..., ...)` + `AgentTools(...)` and
  `await adapter.run_to_result(prompt, tools, options)`, reading the scrubbed
  text off the returned `AgentResult.text`.
- Preserve behavior on Claude bit-for-bit: same scrub model
  (`AP2_IDEATION_SCRUB_MODEL` / its cfg read), same prompt assembly, same
  single-shot output contract, same disabled-extended-thinking behavior
  (TB-294).
- Keep `_run_scrub`'s existing `sdk`-injection seam working for tests (the
  resolved adapter wraps the injected handle), so the scrub's unit tests stay
  hermetic.

## Design

ideation-scrub is the least-entangled site (one-shot, no custom MCP tools),
which is why goal.md sequences it first. The migration swaps the direct
`sdk.query` consume loop for `adapter.run_to_result`, with the adapter chosen by
the `ideation_scrub` kind's `[agent_backends]` mapping — so an operator can run
`ideation_scrub=codex` while everything else stays claude. The Claude path stays
the behavior reference: with the default all-claude map, the scrub output is
identical to today.

## Verification

- `uv run pytest -q ap2/tests/test_scrub_disable_thinking.py ap2/tests/test_scrub_exhaustion_language.py`
  — the scrub's existing tests pass against the adapter-routed path (hermetic,
  injected handle).
- `! grep -nE "sdk\.query\(|sdk\.ClaudeAgentOptions\(" ap2/ideation_scrub.py` —
  the direct SDK calls are gone from the scrub dispatch site (absence check via
  the `!` exit-inversion prefix).
- `grep -q "run_to_result" ap2/ideation_scrub.py` — the scrub dispatches through
  the adapter's drain-to-result convenience.
- `uv run pytest -q ap2/tests/test_agent_adapter.py` — the adapter contract
  suite still passes (no regression to the shared seam).
- `_run_scrub` Prose: with the default all-claude `[agent_backends]` map,
  `_run_scrub` resolves a `ClaudeCodeAdapter` for the `ideation_scrub` kind and
  produces the same scrubbed-text result as the pre-migration direct `sdk.query`
  path; judge confirms via Read that the resolver is keyed on the
  `ideation_scrub` kind and the Claude behavior is preserved.

## Out of scope

- Migrating any other dispatch site (verifier prose-judge, validator-judge /
  janitor-judge, run_task, `_run_control_agent`) — each is its own later axis-6
  TB once this canary proves the shape.
- Implementing the per-kind selector (axis 5, TB-358) or the `CodexAdapter`
  (axis 4, TB-357) — both are hard predecessors.

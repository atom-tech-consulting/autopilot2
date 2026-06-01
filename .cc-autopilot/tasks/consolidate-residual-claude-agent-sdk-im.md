## Goal

Close the **Current focus: codex support through an agent adaptor layer**
Progress signal "`claude_agent_sdk` is imported only inside
`ClaudeCodeAdapter`, not across `daemon.py` / `tools.py` / `verify.py` /
`ideation_scrub.py`." All six dispatch CALLS are now adapter-routed
(TB-360/362/363/364/365), but residual `claude_agent_sdk` IMPORTS still leak
across non-adapter source: `daemon.py` (the `import claude_agent_sdk as sdk`
handed to `status_report.configure`, and the `_import_sdk_or_die`
availability gate) and `tools.py` (`from claude_agent_sdk import tool`, the
tool-definition decorator). The dispatch migrations preserved these to keep
the injected-`sdk` hermetic-test seam (`_run_control_agent(cfg, sdk,
mcp_server, ...)` kept its signature per TB-365). This task relocates the SDK
surface so it lives only under `ap2/adapters/`, and pins the invariant with a
CI-style import-direction gate (mirroring the component focus's
`test_core_does_not_import_from_components`).

Why now: the dispatch migrations preserved the injected-`sdk` seam by keeping
`import claude_agent_sdk` in `daemon.py`/`tools.py`, so this Progress signal
is still red and any future module can silently re-introduce a direct SDK
import; landing the import-direction gate now locks in the single-backend-
surface invariant the Codex adapter depends on before more code accretes
against the Claude stream shape.

## Scope

1. Relocate the residual `claude_agent_sdk` imports out of non-adapter source:
   - `daemon.py`: the `sdk` handle currently imported and passed to
     `status_report.configure(sdk, mcp_server)` — source it from / through
     the adapter layer instead of a bare `import claude_agent_sdk as sdk`.
   - `daemon.py`: `_import_sdk_or_die` — route the availability check through
     the adapter / the backend-aware auth gate (TB-358) rather than a bare
     `import claude_agent_sdk`.
   - `tools.py`: re-export the `tool` decorator from `ap2/adapters/` (e.g.
     `from ap2.adapters import tool`) so `claude_agent_sdk` is not imported
     here directly.
2. PRESERVE the injected-`sdk` hermetic-test seam — relocate it behind the
   adapter (the adapter already accepts `sdk=`), do NOT delete it. Existing
   hermetic tests that inject a fake SDK must keep working by injecting via
   the adapter.
3. Add `ap2/tests/test_sdk_import_boundary.py` with a test
   (`test_claude_sdk_imported_only_in_adapters`) that walks every `*.py`
   under `ap2/` OUTSIDE `ap2/adapters/` and `ap2/tests/` and asserts none
   contains an `import claude_agent_sdk` / `from claude_agent_sdk` statement
   (comments/docstrings are fine — match import statements only).
4. No behavior change on Claude: prompts, tool policy, dispatch semantics,
   and emitted events are byte-for-byte unchanged.

## Design

The point is to centralize ALL `claude_agent_sdk` surface area in
`ap2/adapters/` so swapping or abstracting the backend later touches one
package. Genuine relocation only — do NOT satisfy the gate with a `# noqa`
suppression or by importing under an alias that dodges the matcher. The
`validator_judge/impl.py:510` injected-sdk import already wraps the handle in
a default `ClaudeCodeAdapter` (TB-363); if the boundary test flags it, route
that import through the adapter the same way (it is allowed to stay only if
it lives behind the adapter boundary the test enforces).

## Verification

- `uv run pytest -q ap2/tests/test_sdk_import_boundary.py` — the new import-direction gate passes (no `claude_agent_sdk` import outside `ap2/adapters/`).
- `uv run pytest -q` — full suite stays green (the injected-sdk seam relocation breaks no existing hermetic test).
- `ap2/daemon.py` Prose: no `import claude_agent_sdk` / `from claude_agent_sdk` statement remains in this file — the status-report sdk handle and the availability gate now resolve through the adapter layer; judge confirms via Grep.
- `ap2/tools.py` Prose: the `tool` decorator is imported from `ap2/adapters` (a re-export), not directly from `claude_agent_sdk`; judge confirms via Read.

## Out of scope

- Abstracting the tool-DEFINITION mechanism (the `@tool` schema shape) for a
  non-Claude backend — this task only relocates the import; the CodexAdapter
  tool-wiring (TB-357) already covers registration.
- Per-message or in-task backend routing (a focus non-goal, goal.md
  L127-128).
- Deleting the injected-`sdk` test seam.
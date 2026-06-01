## Goal

Finish the goal.md `Current focus: codex support through an agent adaptor layer`
by closing its last startup-gate edge case. The daemon-start *credential* check in
`ap2/cli_daemon.py` is already backend-aware: it walks `cfg.get_agent_backend(kind)`
over `AGENT_KINDS` and requires OAuth only for kinds resolving to `claude` and
`OPENAI_API_KEY` only for kinds resolving to `codex`. The sibling
*SDK-availability* gate, however, is unconditional: `daemon.main_loop` calls
`_import_sdk_or_die()` (daemon.py:1926) and then `load_claude_sdk()`
(daemon.py:1936) on every start regardless of the backend map. A pure-codex
configuration (every agent kind selected to `codex`) therefore still hard-fails
at daemon start with "claude-agent-sdk not installed", even though no kind will
ever dispatch through the Claude SDK. This violates the focus constraint that
"the daemon-start gate requires creds for each backend the map references" — the
availability gate should reference the same resolved backend set the credential
gate already computes. Make the SDK-availability gate backend-aware so the Claude
SDK is required only when at least one kind resolves to `claude`, preserving
today's behavior bit-for-bit for the all-claude default.

Why now: the SDK-import gate is the one remaining unconditional Claude dependency
at startup now that the dispatch path carries no direct `sdk.query` / `import
claude_agent_sdk` — so a pure-codex install, the extreme the per-kind selection
axis exists to support, still cannot start the daemon, which is the literal
failure mode this task closes.

## Scope

- In `ap2/daemon.py`, gate the startup SDK load behind the resolved per-kind
  backend set instead of running it unconditionally:
  - Resolve each kind via `cfg.get_agent_backend(kind)` over `AGENT_KINDS` (the
    existing `ap2/cli_daemon.py` credential gate already does this, normalizing
    any non-`codex` id to `claude` via its `_effective` helper). Compute the set
    of referenced effective backends once.
  - Only call `_import_sdk_or_die()` / `load_claude_sdk()` (and only assign the
    `sdk` handle threaded to `status_report.configure`, `run_task`,
    `_run_control_agent`) when `claude` is in the referenced set. When no kind
    resolves to `claude`, skip the Claude SDK import so a pure-codex install
    starts cleanly.
  - When the Claude SDK is not loaded, any consumer that previously received the
    `sdk` handle must tolerate `None` (the Claude dispatch paths are unreachable
    in a pure-codex config, so the handle is never used; keep the injected-SDK
    hermetic-test seam working for mixed/all-claude configs).
- Preserve `_import_sdk_or_die`'s existing message + `sys.exit(1)` behavior for
  the case where `claude` IS referenced but the SDK is missing.

## Design

- The credential gate in `ap2/cli_daemon.py` already encodes the canonical
  "resolve each kind → effective backend" logic. Factor or mirror that resolution
  so the availability gate and the credential gate agree on which backends the
  map references (a single shared helper that returns the referenced
  effective-backend set is cleaner than duplicating the comprehension).
- Behavior parity for existing installs: the all-claude default resolves to
  `{"claude"}`, so the SDK is still imported and still hard-fails when missing —
  zero observable change for every current operator. Only a config that resolves
  to `{"codex"}` (no claude kinds) takes the new skip path.

## Verification

- `uv run pytest -q ap2/tests/test_tb368_backend_aware_sdk_gate.py` — new test file: with a pure-codex backend map (all `AGENT_KINDS` resolve to `codex`) and `load_claude_sdk` monkeypatched to raise `ImportError`, the daemon-start SDK-availability path proceeds WITHOUT raising `SystemExit`; with the all-claude default and the same `ImportError`, it still exits non-zero.
- `uv run pytest -q ap2/tests/` — full unit suite passes (no regression in the existing `test_sdk_import_boundary.py` import-boundary pins or the `test_cli_daemon.py` credential-gate tests).
- `ap2/daemon.py` Prose: the startup SDK-availability gate (`_import_sdk_or_die` / the `load_claude_sdk()` call in `main_loop`) is guarded by the resolved per-kind backend set (via `cfg.get_agent_backend` over `AGENT_KINDS`, or a shared helper) so `load_claude_sdk()` runs only when at least one kind resolves to `claude`; judge confirms via Read.

## Out of scope

- A symmetric codex-CLI-binary presence check at daemon start (e.g.
  `shutil.which("codex")` when a codex kind is referenced). The `OPENAI_API_KEY`
  credential is already gated for codex kinds; CLI-presence is a thinner
  follow-up and is deliberately excluded to keep this task narrow.
- Any change to per-kind selection, the `[agent_backends]` table, or the
  credential gate itself — those already exist; do not modify them.
- Adding a third backend or per-message routing (focus non-goals, goal.md
  L127-128).
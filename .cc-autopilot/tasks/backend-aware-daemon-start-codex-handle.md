## Goal

This task advances the **Current focus: codex support through an agent adaptor layer** by completing axis 5's daemon-start gate symmetrically across backends. Axis 5 (goal.md L169-175) makes "the daemon-start credential check backend-aware, requiring creds for each backend the map references". The credential gate already covers both backends (`OPENAI_API_KEY` for codex kinds, `ap2/cli_daemon.py` L129) and the existing Claude-SDK-*availability* gate `daemon._load_claude_sdk_if_referenced(cfg)` uses `ap2.adapters.referenced_backends(cfg)` to skip the Claude SDK import for a pure-codex map. But that availability gate is **claude-only**: nothing verifies the codex handle is present. The `CodexAdapter` lazily imports its handle (`import codex_sdk as codex` in `CodexAdapter._get_codex()`, `ap2/adapters/codex.py` L248-252) only at first dispatch. So a pure- or mixed-codex map with `OPENAI_API_KEY` set but the codex handle NOT installed passes both existing gates, starts cleanly, then hard-fails with a cryptic `ImportError` deep in the first codex run — the exact mirror of axis-5's own delete-test failure mode ("codex hard-fails the OAuth-only gate"). This task adds the symmetric codex-side availability probe at daemon start so the failure surfaces at startup with a clear, actionable message instead of mid-run.

Why now: the Claude-side half of the daemon-start availability gate is in HEAD and explicitly left the codex-side symmetric check out of scope; this is the fresh, direct follow-up that closes the last concrete axis-5 edge case — without it a pure-codex install fails late and cryptically at first dispatch rather than fast at startup, defeating the focus's promise that switching a kind to codex is a config change, not a debugging session.

## Scope

- Add a daemon-start codex-handle-availability probe symmetric to the existing
  Claude-SDK availability gate. It should consult
  `ap2.adapters.referenced_backends(cfg)` (the same helper the Claude gate and
  the credential gate agree on) and only run when the resolved set contains
  `"codex"`. When codex is referenced and the codex handle is NOT importable,
  exit non-zero (`sys.exit`/`SystemExit`) with a clear message that (a) names the
  missing codex handle, (b) lists the codex-backed kinds, and (c) hints how to
  remediate — matching the tone of the existing credential-gate remediation in
  `ap2/cli_daemon.py`.
- Wire the probe into the daemon startup path next to the existing Claude-SDK
  availability gate so both backend gates run before the tick loop.
- Keep the codex handle injectable/monkeypatchable for hermetic testing, exactly
  as the existing Claude-SDK gate keeps `ap2.adapters.load_claude_sdk` patchable.
- An all-claude map (today's default) must be completely unaffected: the codex
  probe is skipped when `referenced_backends(cfg)` does not contain `"codex"`,
  so current operators see zero behavior change.

## Design

- Determine the exact probe by reading how `CodexAdapter._get_codex()` resolves
  its handle (the lazy `import codex_sdk`); mirror that resolution so the gate
  and the adapter agree on what "codex is available" means. Prefer factoring the
  handle-load into a patchable module-level seam in `ap2/adapters/` (sibling to
  `load_claude_sdk`) so both the adapter and the new gate resolve through one
  point and tests can force an `ImportError` without a real codex install.
- Add the gate as a function symmetric to the existing Claude-SDK gate
  (e.g. a sibling `_require_codex_handle_if_referenced(cfg)` in `ap2/daemon.py`):
  compute `referenced_backends(cfg)`; if `"codex"` is absent, return immediately
  (no probe); if present, attempt the codex-handle load and on `ImportError`
  call `sys.exit(1)` after printing the remediation message.
- Call the new gate from the same startup location `main_loop` already calls the
  Claude-SDK availability gate, so the two backend gates sit side by side and
  both run before the tick loop begins.
- Reuse the existing `referenced_backends` / `AGENT_KINDS` machinery and the
  credential-gate message style; introduce no new env knobs and no new config
  surface.

## Verification

- `uv run pytest -q ap2/tests/test_tb369_codex_availability_gate.py` — new test module passes.
- `uv run pytest -q` — full suite passes (no regressions).
- New test pins: a pure-codex backend map (every `AP2_AGENT_BACKEND_<KIND>=codex`) with the codex-handle import forced to fail raises `SystemExit` with a non-zero code from the daemon-start codex-availability gate (mirror of `test_all_claude_default_still_dies_without_sdk` in `ap2/tests/test_tb368_backend_aware_sdk_gate.py`, but for the codex backend).
- New test pins: the all-claude default (no `AP2_AGENT_BACKEND_*` overrides) does NOT raise from the codex-availability gate even when the codex handle is unavailable — current operators are unaffected.
- New test pins: a codex-referencing map with the codex handle importable does NOT raise from the codex-availability gate (happy path).
- `ap2/tests/test_tb369_codex_availability_gate.py` Prose: the new gate function consults `ap2.adapters.referenced_backends(cfg)` to decide whether to probe the codex handle, symmetric to the existing Claude-SDK gate; judge confirms via Grep/Read that the gate is keyed off the resolved backend set rather than an unconditional codex import.

## Out of scope

- Probing the codex CLI *binary* on `$PATH` separately from the importable handle — mirror exactly whatever `CodexAdapter._get_codex()` resolves (handle import); do not invent a second, divergent presence concept.
- Any change to the existing credential gate or Claude-SDK availability gate beyond adding the sibling codex probe alongside them.
- Changing `CodexAdapter` dispatch behavior, prompt assembly, or tool wiring — this task only adds a startup-time availability check.

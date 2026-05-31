## Goal

Axis 5 of the **Current focus: codex support through an agent adaptor layer**.
With the `CodexAdapter` (TB-357, axis 4) implementing the same `AgentAdapter`
contract as `ClaudeCodeAdapter`, ap2 has two real backends but no operator-facing
way to choose between them per agent kind. This task adds the selection surface
goal.md scopes: an `[agent_backends]` config table mapping each agent kind
(`task`, `ideation`, `status_report`, `cron`, `mattermost`, `verifier_judge`,
`ideation_scrub`, `validator_judge`, `janitor_judge`) to a backend id (every
kind defaulting to `claude`), with `AP2_AGENT_BACKEND_<KIND>` env overrides, plus
a backend-aware daemon-start auth gate. A small `select_adapter(kind, cfg)`-style
resolver returns the right adapter instance for a kind. The daemon-start
credential check (`_require_oauth_token` in `ap2/cli_daemon.py`) becomes
backend-aware: it requires OAuth for any kind mapped to `claude` and OpenAI/codex
credentials for any kind mapped to `codex`, so switching a kind to codex no
longer hard-fails the OAuth-only gate.

Why now: TB-357 lands a second backend that nothing can route to — without
per-kind selection, switching a kind's backend needs a code edit and codex
hard-fails the OAuth-only daemon-start gate, so the abstraction can't actually
drive an agent kind; this closes goal.md's axis-5 delete-test.

## Scope

- Add an `[agent_backends]` config section (per-kind to backend id; default every
  kind to `"claude"`) read through the existing structured-config path, with
  `AP2_AGENT_BACKEND_<KIND>` env overrides honored via the established override
  layer.
- Add a backend resolver (e.g. `ap2/adapters/select.py`
  `select_adapter(kind, cfg) -> AgentAdapter`) returning a `ClaudeCodeAdapter` or
  `CodexAdapter` instance for the kind, defaulting to claude on an unmapped /
  unknown kind.
- Make the daemon-start auth gate backend-aware: extend `_require_oauth_token`
  (`ap2/cli_daemon.py`) so it requires `CLAUDE_CODE_OAUTH_TOKEN` only when one or
  more kinds resolve to `claude`, and requires the codex/OpenAI credential when
  one or more kinds resolve to `codex`; clear error naming which kind needs which
  credential.
- No production dispatch site is repointed to use the resolver yet — that is
  axis 6 (the canary, the first consumer, lands separately). This task ships the
  selection + auth machinery and its tests only.

## Design

Selection is fixed per kind at dispatch time (goal.md constraint: no per-message
/ in-task routing). The resolver reads the merged `[agent_backends]` config
(file + `AP2_AGENT_BACKEND_<KIND>` overrides + the all-`claude` default) and
instantiates the matching adapter. The auth gate walks the resolved backend set
and requires exactly the credentials that set implies, so an all-claude install
behaves identically to today (OAuth required, no OpenAI cred needed) while a
mixed map adds the codex credential requirement only for the codex-backed kinds.

## Verification

- `uv run pytest -q ap2/tests/test_agent_backend_selection.py` — new test
  covering: the default resolves every kind to `claude`; an
  `AP2_AGENT_BACKEND_TASK=codex` override resolves `task` to a `CodexAdapter`;
  an unknown kind falls back to claude.
- `uv run pytest -q ap2/tests/test_cli_daemon.py` — extended auth-gate tests
  pass: an all-claude map requires only OAuth; a codex-mapped kind requires the
  codex/OpenAI credential.
- `grep -rq "AGENT_BACKEND" ap2/` — the per-kind backend-selection namespace is
  wired into source.
- `select_adapter` Prose: the resolver named in Scope returns a
  `ClaudeCodeAdapter` for a claude-mapped kind and a `CodexAdapter` for a
  codex-mapped kind; judge confirms via Read that it instantiates the adapter
  classes from `ap2.adapters` keyed on the per-kind config value.
- `ap2/cli_daemon.py` Prose: the daemon-start auth gate requires OpenAI/codex
  credentials when any kind resolves to the `codex` backend and preserves the
  existing `CLAUDE_CODE_OAUTH_TOKEN` requirement for claude-backed kinds; judge
  confirms via Read of the modified `_require_oauth_token` path.

## Out of scope

- Repointing any dispatch site through the resolver (axis 6 — the ideation-scrub
  canary is the first consumer).
- Implementing the `CodexAdapter` itself (axis 4, TB-357 — a hard predecessor).
- The adapter-contract parity suite + codex smoke (axis 7).
## Attempts

### 2026-05-31 — error
(no summary)
- **error:** Exception: Claude Code returned an error result: success
- **stderr_tail:** 
- **Debug dumps:** `prompt: .cc-autopilot/debug/20260531T180251Z-TB-358.prompt.md`, `stream: .cc-autopilot/debug/20260531T180251Z-TB-358.stream.jsonl`, `messages: .cc-autopilot/debug/20260531T180251Z-TB-358.messages.jsonl`

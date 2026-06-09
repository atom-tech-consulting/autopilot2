# Docs: document the agent-backend layer (Claude Code default + Codex) across architecture.md, howto.md, README

Tags: #autopilot #docs #codex #backend #architecture

## Goal

The codex-support arc (shipped 2026-06-06; goal.md "## Shipped focus" + the
"Pluggable agent backend (default Claude Code)" constraint) added a
backend-agnostic `AgentAdapter` layer — every agent dispatch now flows through
`select_adapter(kind, cfg)` → a `ClaudeCodeAdapter` (default) or a
`CodexAdapter` — but the docs were never updated. `ap2/architecture.md` still
describes agents as direct `claude_agent_sdk` `query()` calls (no adapter /
backend mention at all), `ap2/howto.md` documents only the codex *test guard*
(TB-375, not the feature), and `README.md` mentions neither. Document the
backend layer so an operator/agent can understand and configure it.

Per the doc-surface split: `ap2/architecture.md` = technical design (the
backend layer's shape); `ap2/howto.md` = operation manual (how to configure /
select a backend + auth); `README.md` = operator quickstart blurb.

## Scope

- **architecture.md — design.** Add an "## Agent backends" section: the
  `AgentAdapter` abstraction (`ap2/adapters/`); the two adapters
  (`ClaudeCodeAdapter` = default, `sdk.query()` against the bundled Claude Code
  binary; `CodexAdapter` = OpenAI `openai-codex` SDK); `select_adapter(kind,
  cfg)` resolution order (`AP2_AGENT_BACKEND_<KIND>` env override >
  `[agent_backends]` TOML table > `DEFAULT_AGENT_BACKEND` = claude; an unknown
  value degrades to claude); per-kind selection across the agent kinds; and the
  codex adapter's stdio-MCP bridge (since `create_sdk_mcp_server` is
  Claude-specific). Update the existing "## Agent kinds", "### Shared SDK
  plumbing", and "## Custom MCP tools" sections that currently assert agents
  call `claude_agent_sdk` directly, to reflect the adapter indirection.
- **howto.md — operation.** Document the operational config: the
  `[agent_backends]` table + `AP2_AGENT_BACKEND_<KIND>` env override (with the
  resolution order), per-backend auth (Claude: `CLAUDE_CODE_OAUTH_TOKEN`;
  Codex: a ChatGPT-login session at `$CODEX_HOME` / `~/.codex/auth.json`, or
  `OPENAI_API_KEY` — presence-only), and the daemon-start auth gate (start
  refuses unless creds are present for every backend the map references). Add
  `[agent_backends]` to the "## Config keys (TOML)" section.
- **README — quickstart.** One short blurb noting the backend is pluggable:
  Claude Code by default, Codex selectable per agent kind, each bringing its
  own auth.
- Treat `OPENAI_API_KEY` / `~/.codex/auth.json` / OAuth tokens as secrets —
  describe presence and location, never print or commit contents.
- Documentation only — no code changes.

## Design

- Source of truth for the mechanism: `ap2/adapters/select.py`
  (`select_adapter` + resolution order), `ap2/config.py`
  (`agent_backends_config`, the `[agent_backends]` table +
  `AP2_AGENT_BACKEND_<KIND>` precedence), and the daemon-start auth gate
  (`_codex_credentials_present`). Quote the resolution order exactly; do not
  invent knob names.
- Keep the split clean per the doc roles: design → architecture.md, config /
  auth ops → howto.md, quickstart → README.

## Verification

- `grep -qiE 'AgentAdapter|## Agent backends' ap2/architecture.md` — architecture.md documents the backend layer.
- `grep -qE 'AP2_AGENT_BACKEND|agent_backends' ap2/howto.md` — howto.md documents the backend config knobs.
- `grep -qiE 'backend|codex' README.md` — README notes the pluggable backend.
- `ap2/architecture.md` Prose: a "## Agent backends" section accurately describes the `AgentAdapter` layer, the two adapters (Claude default / Codex), and `select_adapter`'s resolution order (env override > `[agent_backends]` table > claude default); the Agent-kinds / SDK-plumbing sections no longer imply agents call `claude_agent_sdk` directly without the adapter. Judge confirms via Read.
- `ap2/howto.md` Prose: the operational backend config — `[agent_backends]` / `AP2_AGENT_BACKEND_<KIND>`, per-backend auth, and the daemon-start auth gate — is documented. Judge confirms via Read.

## Out of scope

- The component-model refresh of architecture.md (module map → `ap2/components/`, daemon-loop tick phases, registry `contributions(point)`, communication component, judges-as-adapters) — separate follow-up task.
- Any code change to the backend layer; this is documentation only.

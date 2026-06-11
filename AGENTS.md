# AGENTS.md — ap2 operator guide (Codex / agentskills.io runtimes)

This repository ships **autopilot v2 (ap2)**, a self-driving task daemon.
This file is the Codex / standard-runtime operator reference: it points a
fresh agent session at the operator skills so it can drive the board. (On
the Claude side the equivalent entry point is the top-level `ap2` skill;
both runtimes share the same `skills/*` operator manual.)

## Operator skills

ap2's operator manual is published as [agentskills.io](https://agentskills.io)
`SKILL.md` bundles under `skills/` in this repo. `ap2 sandbox sync-assets`
mirrors them into the runtime skills directories:

- Claude Code: `~/.claude/skills/`
- Codex / standard runtimes: `~/.agents/skills/`

Read the `SKILL.md` for the domain you're working in before acting:

- `ap2` — operator overview + board-driving workflow
- `ap2-task` — task-agent contract (briefings, RESULT blocks, fix-shapes)
- `ap2-board-ops` — operator CLI verbs + custom MCP tools
- `ap2-config` — configuration knobs + config keys
- `ap2-observability` — event schema, `ap2 logs`, the stats dashboard
- `migrate-to-ap2` — onboarding an existing repo onto ap2

## Discovery pointer

`ap2 sandbox sync-assets` also writes an auto-managed `skills-discovery`
stanza into the runtime's global instructions file (`~/.codex/AGENTS.md`
for Codex, `~/.claude/CLAUDE.md` for Claude) so a fresh session finds the
deployed skills without a hand-edit. The stanza is delimited by
`<!-- BEGIN ap2-managed: skills-discovery -->` / `<!-- END ... -->` markers
and is rewritten idempotently on every sync (repeated runs converge — no
duplicate stanza).

## First move

Run `ap2 --project <path> status` to read live daemon + board state before
making any changes — board state changes continuously while the daemon
ticks, so never infer it from memory or session history.

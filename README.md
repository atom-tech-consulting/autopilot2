# autopilot2

An autonomous development loop. You declare what success looks like once (in a
`goal.md`); ap2's daemon then drives your project toward it — **proposing**
tasks, **running** each as a fresh agent, **verifying** the result, **committing**
on success, and **recovering** failures — across many cycles, mostly unattended.

Each unit of work is a fresh agent dispatch through a pluggable backend —
[Claude Code][sdk] `query()` by default, with OpenAI Codex selectable per agent
kind (each backend brings its own auth). Shared state lives on disk, so the
daemon never accumulates context and sessions don't degrade as work piles up.

[sdk]: https://github.com/anthropics/claude-agent-sdk-python

## How it works

You write `goal.md` once — a Mission, a `## Done when` checklist, and a
`## Current focus`. The daemon reads it every cycle and runs this loop:

```
   goal.md          you declare what "done" looks like
      │
      ▼
   ideation         proposes goal-aligned tasks ─► Backlog (awaiting review)
      │                                                │
      │                                  ap2 approve ──┘
      ▼
   dispatch         a fresh agent (Claude Code / Codex) does one task
      │
      ▼
   verify           auto-checks the task's `## Verification` acceptance bullets
      │
      ├── fail ─► retry ─► (exhausted) Frozen ─► you: ap2 unfreeze
      ▼
   Complete         committed to git; watch via `ap2 status` / the web UI
```

You stay in the loop only for judgment calls — approving proposals, triaging a
frozen task, steering `goal.md`. The rest runs unattended; relax individual
gates (e.g. auto-approve) per surface as you build trust.

## Install

Requires Python 3.11+ and an Anthropic OAuth token (`claude setup-token`
to obtain one — the daemon reads `CLAUDE_CODE_OAUTH_TOKEN` from the env).

```bash
# As a uv tool (recommended — isolates ap2's deps from your projects)
uv tool install git+https://github.com/atom-tech-consulting/autopilot2

# Or in a virtualenv
pip install git+https://github.com/atom-tech-consulting/autopilot2

# Editable, for development
git clone https://github.com/atom-tech-consulting/autopilot2
cd autopilot2
uv sync && uv pip install -e .
```

This installs the `ap2` console script. Add the Codex backend with the `[codex]`
extra (e.g. `uv tool install 'autopilot2[codex] @ git+…'`).

## Quickstart

```bash
cd /path/to/your/repo
ap2 init        # scaffolds goal.md, TASKS.md, and .cc-autopilot/

# 1. Declare the goal — the step that matters most. Edit goal.md: a Mission
#    line, a `## Done when` checklist, and a `## Current focus`. ap2 reads it
#    every cycle to decide what to propose and when the goal is met.
$EDITOR goal.md

export CLAUDE_CODE_OAUTH_TOKEN=...   # required — obtain via `claude setup-token`
ap2 start                            # daemon starts in the background
```

Then get work onto the board — two ways:

**(a) Let ap2 propose it from `goal.md`** (the autonomous path):

```bash
ap2 ideate          # run an ideation cycle now (it also fires on its own)
ap2 status          # proposals land in Backlog, marked pending-review
ap2 approve TB-1    # approve one → the daemon dispatches, verifies, commits
```

**(b) Or author a task yourself.** The briefing must carry the full structure
the verifier expects — `## Goal` (with a `Why now:` line), `## Scope`,
`## Design`, `## Verification` (auto-verifiable bullets only), and
`## Out of scope`. The title comes from the `# H1`:

```bash
cat > /tmp/refactor-foo.md <<'EOF'
# Refactor the foo helper

Tags: #refactor

## Goal
Extract the inline string parsing in `foo()` into a tested helper.

Why now: the parser is duplicated across three call sites and silently drops
malformed input — this closes that correctness gap before more callers copy it.

## Scope
- `src/foo.py` (extract the parser), `tests/test_foo.py` (cover it).

## Design
Move the parse block into `parse_foo(s) -> Foo`; callers use the helper.

## Verification
- `uv run pytest -q tests/test_foo.py` — the helper's tests pass.

## Out of scope
- Refactoring the downstream callers.
EOF
ap2 add --briefing-file /tmp/refactor-foo.md --skip-goal-alignment
```

> `--skip-goal-alignment` lets the example run without matching your `goal.md`.
> For a goal-aligned task, drop it and make the `## Goal` body cite your
> `## Current focus`. The `ap2-task` skill and `ap2/architecture.md` document
> the full briefing + `## Verification` bullet rules.

Then watch it run:

```bash
ap2 status                   # board state + daemon liveness
ap2 logs -n 20               # tail recent events
open http://127.0.0.1:8729/  # bundled read-only web UI
```

Stop with `ap2 stop`; pause/resume without stopping via `ap2 pause` / `ap2 resume`.

`ap2 start` brings up the read-only web UI in the same process and tears it
down when the daemon stops. Override the port with `AP2_WEB_PORT`; opt out
entirely with `AP2_WEB_DISABLED=1` (CI/headless). The standalone `ap2 web`
command stays available for browsing past events when the daemon is not
running.

For long-running work (>5 min) and OS-level isolation, see the
[sandbox runbook](sandboxed-user-setup.md) — the daemon is designed to
run as a separate OS user (`claude-agent`) so its tools can't reach your
home, keychain, or other repos.

## What's in this repo

```
ap2/                          # the package — daemon, CLI, MCP tools, tests
├── README.md                 # operator reference + full CLI / event schema
├── architecture.md           # design rationale, agent kinds, verification model
├── AGENTS.md                 # Codex operator reference → ~/.agents/AGENTS.md (shipped as package data)
└── skills/                   # the operator manual — auto-triggered SKILL.md bundles (shipped as package data)
    ├── ap2/                  # /ap2 <project> — daemon snapshot + reading order
    ├── ap2-task/             # task-agent contract + briefing/verification authoring
    ├── ap2-board-ops/        # operator CLI verbs + custom MCP tools
    ├── ap2-config/           # configuration knobs + config keys
    ├── ap2-observability/    # event schema, ap2 logs, ap2 status components, stats
    ├── ap2-failure-recovery/ # how the daemon self-heals + operator triage
    ├── ap2-ideation-goals/   # authoring goal.md + the ap2 audit walk
    └── migrate-to-ap2/       # /migrate-to-ap2 — convert legacy TODO.md → TASKS.md
sandboxed-user-setup.md       # OS-level sandbox-user runbook (repo root)
```

## Documentation

- **[ap2/README.md](ap2/README.md)** — operator quickstart, full CLI reference,
  configuration knobs, event schema.
- **[ap2/architecture.md](ap2/architecture.md)** — design rationale, the
  daemon loop, agent kinds, two-tier verification, sandbox model.
- **[sandboxed-user-setup.md](sandboxed-user-setup.md)** — runbook for
  setting up the `claude-agent` sandbox user.
- **[ap2/skills/](ap2/skills/)** — the operator manual, published as
  auto-triggered agentskills.io `SKILL.md` bundles (CLI verbs, config knobs,
  event schema, the task-agent contract, failure recovery, goal authoring). The
  bundles ship as installed package data, so `ap2 sandbox sync-assets` deploys
  them into the runtime skills roots after any install — including a bare `uv
  tool install` — and a Claude Code or Codex session running inside an
  ap2-managed project picks them up automatically.

## Tests

See [`ap2/README.md#tests`](ap2/README.md#tests) for the canonical test-suite
guide (default suite + real-SDK smokes).

## License

Licensed under the **PolyForm Noncommercial License 1.0.0** — see
[LICENSE](LICENSE). This is a source-available, noncommercial license: you may
read, run, modify, and share the code for any noncommercial purpose, but it is
**not** OSI-approved open source and does not permit commercial use. See
<https://polyformproject.org/licenses/noncommercial/1.0.0> for the full terms.

> **Note on the committed `.cc-autopilot/` tree.** This repository ships its own
> `.cc-autopilot/` directory (board, tasks, progress, events) because ap2
> dogfoods itself — that is ap2's *own* self-management state, not a template
> for your project. In a fresh checkout you can reset it to an empty scaffold
> with `ap2 init`; it is not part of any consumer's own project.

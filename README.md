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
   ideation         proposes goal-aligned tasks ─► Backlog
      │
      ▼
   auto-approve     ON by default — proposals dispatch unattended
      │             (opt out to gate each one on `ap2 approve`)
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

By default the loop runs unattended end-to-end: ideation proposals are
auto-approved and dispatched without you. You stay in the loop only for the
judgment calls that remain — triaging a frozen task, steering `goal.md`. If you
want a human check before each dispatch, re-insert a manual approval gate by
opting out of auto-approve (see the quickstart below).

## Agent skills (agent-first operation)

ap2 is **agent-first**: its operator manual ships as auto-triggered
agentskills.io `SKILL.md` bundles, not just CLI man pages. The intended way to
drive the board is to talk to a **Claude Code or Codex** session — it loads the
right skill on its own and runs the verbs for you, so you never memorize CLI
flags or briefing structure.

The skills live as installed package data, so one command deploys them:

```bash
ap2 sandbox sync-assets        # copies the SKILL.md bundles into the runtime skills roots
```

Once `sync-assets` has run — after any install, including a bare `uv tool
install` — a Claude Code or Codex session opened inside an ap2-managed project
picks the bundles up automatically. No per-task setup, no flag-hunting: you ask
in plain language and the agent reads the matching skill and operates ap2 for
you. For example:

- *"What's frozen and why?"* → the agent loads `ap2-failure-recovery`, reads the
  board, and explains the freeze + the fix-shape.
- *"Queue a task to extract the parser in foo.py."* → it loads `ap2-task`,
  authors a briefing with the structure the verifier expects, and runs `ap2 add`.
- *"Is the daemon healthy?"* → it loads `ap2-observability` and reads
  `ap2 status` / the event stream for you.

The bundles cover the whole operator surface — daemon snapshot (`/ap2`), the
task-agent contract (`ap2-task`), board ops (`ap2-board-ops`), config
(`ap2-config`), observability (`ap2-observability`), failure recovery
(`ap2-failure-recovery`), goal authoring (`ap2-ideation-goals`), and legacy
migration (`migrate-to-ap2`). See [What's in this repo](#whats-in-this-repo) for
the per-bundle map and [ap2/skills/](ap2/skills/) for the bundles themselves.

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

> **Caution — auto-approve is ON by default.** A bare `ap2 start` is autonomous:
> ideation proposals are approved and dispatched on their own, and each agent
> edits files in your repo and runs shell commands **unattended**. The no-sandbox
> quickstart below works, but on it the daemon runs as **your own user** with no
> OS isolation. For unattended or long-running use, run ap2 under the
> separate-user [sandbox runbook](sandboxed-user-setup.md) (its own OS user, tool
> isolation). First time out, keep a human in the loop with the review gate or
> dry-run shown below.

```bash
cd /path/to/your/repo
ap2 init        # scaffolds goal.md, TASKS.md, and .cc-autopilot/

export CLAUDE_CODE_OAUTH_TOKEN=...   # required — obtain via `claude setup-token`
ap2 start                            # daemon starts in the background

# Deploy the operator skills so you can drive ap2 agent-first (see "Agent skills"
# below) — a Claude Code or Codex session in this project then picks them up.
ap2 sandbox sync-assets
```

Now do the rest **agent-first**: open a Claude Code or Codex session in the repo
and let it drive.

- **Declare the goal** — the step that matters most. Ask the agent to draft
  `goal.md` (a Mission line, a `## Done when` checklist, a `## Current focus`)
  from a sentence or two about what you're building; it loads `ap2-ideation-goals`
  and writes it in the shape ap2 reads each cycle. Review and tweak.
- **Let ap2 propose work** — the autonomous path. `ap2 ideate` runs an ideation
  cycle now (it also fires on its own); proposals land in Backlog, and because
  auto-approve is **ON by default** they're approved and dispatched without you.
- **Or queue a one-off task** — just ask the agent ("queue a task to …"); it
  loads `ap2-task` and authors a briefing in the structure the verifier expects,
  then runs `ap2 add`. No template to copy by hand.

To keep a human review gate instead of autonomous dispatch, opt out before you
start (both knobs hot-reload):

```bash
# Require a manual approval per proposal (opt OUT of autonomous dispatch):
export AP2_AUTO_APPROVE_DISABLED=1        # or [components.auto_approve] disabled = true

# …or just watch the decisions first, acting on none (monitor-only):
#   [components.auto_approve] dry_run = true   # emits would_auto_approve, dispatches nothing
```

With the gate on, `ap2 status` marks each proposal pending-review and you
approve one with `ap2 approve TB-N` → the daemon then dispatches, verifies,
commits.

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

For unattended or long-running work (>5 min) and OS-level isolation, run ap2 as
a dedicated, credential-isolated OS user (`claude-agent`) rather than your own
account — its shell tools then can't reach your home, keychain, or other repos.
The [sandbox setup guide](sandboxed-user-setup.md) walks through it end to end
(the `ap2 sandbox` provisioning verbs, keeping the daemon running, and verifying
the isolation).

## Codex backend

Every agent dispatch uses the Claude Code backend (`query()`) by default, authed
by the `CLAUDE_CODE_OAUTH_TOKEN` from Install. ap2 can also route individual
agent kinds to **OpenAI Codex**. Install the `[codex]` extra, then pick the
backend per kind:

```bash
# Install the optional backend
uv tool install 'autopilot2[codex] @ git+https://github.com/atom-tech-consulting/autopilot2'

# Route a kind to codex via env override (highest precedence)…
export AP2_AGENT_BACKEND_TASK=codex          # task agents run on codex; others stay claude

# …or persist it in .cc-autopilot/config.toml:
#   [agent_backends]
#   task = "codex"
```

Selection is per **agent kind** (`task`, `ideation`, `status_report`, `cron`,
`mattermost`, plus the judge/scrub kinds); any unset kind falls back to
`claude`. Auth follows the resolved backend: claude-backed kinds need
`CLAUDE_CODE_OAUTH_TOKEN`; codex-backed kinds need **either** `OPENAI_API_KEY`
(metered OpenAI billing) **or** a codex ChatGPT-login session on disk
(`$CODEX_HOME/auth.json`, default `~/.codex/auth.json`). The daemon-start auth
gate checks whichever credentials your backend set implies. Keep the default
Claude backend unless you specifically want OpenAI models for a kind, or want to
split work across both providers.

## Components

Most of the daemon tick is built from **components** — opt-in subpackages
discovered through a registry (`ap2/components/<name>/manifest.py`, no hardcoded
list in core). These seven are exactly the names `ap2 status`'s `## Components`
block enumerates, each with its own enable/disable knob:

- **ideation** — proposes goal-aligned tasks from `goal.md` into Backlog (`Phase.IDEATION`; `ideation_*` events; `ap2 ideate`).
- **auto_approve** — promotes Backlog proposals to dispatch unattended; ON by default, opt out with `AP2_AUTO_APPROVE_DISABLED` (the `auto-approve:` status line).
- **auto_unfreeze** — re-dispatches a frozen task when its briefing carries a known fix-shape, instead of waiting for `ap2 unfreeze` (`Phase.PRE_DISPATCH`).
- **cron** — the scheduler: fires due jobs and emits the `cron_*` lifecycle events (the `cron: N jobs` line in `ap2 status`).
- **janitor** — a repo-hygiene scan run as a cron job; surfaces findings in `events.jsonl`.
- **communication** — owns inbound + outbound chat; the **Mattermost handler** answers operator ops in a wired-up channel, and outbound `ap2.notify` deliveries flow here (always-on).
- **attention** — emits operator-attention signals (`task_stuck` / `task_frozen` / …) — the "decisions needed" lines in `ap2 status` (always-on).

A few more loop participants are core stages and surfaces (not registry
components), but you meet them in `ap2 status` and the web UI all the same:

- **dispatch** — runs each ready task as a fresh agent (`run_task`); the Active line, `task_solve` / `task_complete` events.
- **verifier** — runs the task's `## Verification` bullets (shell bullets + prose judge) plus the project-wide test gate; `task_verify` events.
- **operator queue** — applies operator-staged board ops each tick (the `operator_queue_drained` lines).
- **status report** — a cron job (behind the `cron` component) that posts a board snapshot (the `status-report` job and its events).
- **web UI** — the read-only dashboard at `http://127.0.0.1:8729/` (the `web:` line; `AP2_WEB_DISABLED=1` to opt out).

See [ap2/architecture.md](ap2/architecture.md) for the full component model
(the registry, manifests, and tick phases behind these names).

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

Licensed under the **Functional Source License, Version 1.1, MIT Future
License** (`FSL-1.1-MIT`) — see [LICENSE](LICENSE). This is a source-available
license: you may use, copy, modify, and redistribute the code for any purpose
**except a Competing Use** (offering it to others as a commercial product or
service that substitutes for ap2). Internal use, non-commercial education and
research, and professional services are expressly permitted. Two years after
each version is released, that version converts to the **MIT license**. See
<https://fsl.software> for background.

> **Note on the committed `.cc-autopilot/` tree.** This repository ships its own
> `.cc-autopilot/` directory (board, tasks, progress, events) because ap2
> dogfoods itself — that is ap2's *own* self-management state, not a template
> for your project. In a fresh checkout you can reset it to an empty scaffold
> with `ap2 init`; it is not part of any consumer's own project.

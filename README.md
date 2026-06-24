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
> dry-run shown in path (a) below.

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
ap2 status          # proposals land in Backlog, then auto-approve + dispatch
```

Auto-approve is **ON by default**, so proposals are approved and dispatched on
their own — there is no manual `ap2 approve` step in the default flow. To keep a
human review gate instead, opt out before you start (both knobs hot-reload):

```bash
# Require a manual approval per proposal (opt OUT of autonomous dispatch):
export AP2_AUTO_APPROVE_DISABLED=1        # or [components.auto_approve] disabled = true

# …or just watch the decisions first, acting on none (monitor-only):
#   [components.auto_approve] dry_run = true   # emits would_auto_approve, dispatches nothing
```

With the gate on, `ap2 status` marks each proposal pending-review and you
approve one with `ap2 approve TB-N` → the daemon then dispatches, verifies,
commits.

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

## Sandbox setup

For unattended or long-running use, run ap2 as a dedicated, credential-isolated
OS user rather than your own account. Two `ap2 sandbox` subcommands do the
provisioning:

```bash
# 1. Create the locked-down OS user (the positional defaults to `claude-agent`).
#    Prints the exact sudo plan and prompts before running it, then prompts to
#    install CLAUDE_CODE_OAUTH_TOKEN into the user's ~/.zshenv.
ap2 sandbox user-setup claude-agent

# 2. Clone your repo into that user's ~/repos/ — the isolated working copy the
#    daemon drives (it commits there, never in your own tree).
ap2 sandbox project-setup /path/to/your/repo --user claude-agent
```

`user-setup` provisions a passwordless, login-disabled account whose home holds
none of your keys, keychain, or other repos, so an agent's shell tools can't
reach beyond the sandbox. `project-setup` gives that user its own clone. The
companion verbs — `user-audit`, `project-audit`, `install-token`, `install-mm`,
`install-channel`, `sync-assets` — verify the isolation and deploy
credentials/assets (`ap2 sandbox --help` lists them all). The end-to-end runbook
(launchd/systemd units, resource limits, the audit checklist) lives in
[sandboxed-user-setup.md](sandboxed-user-setup.md).

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

The daemon loop runs a handful of cooperating components — the same names you
see in `ap2 status` and the web UI:

- **ideation** — proposes goal-aligned tasks from `goal.md` into Backlog (`ap2 ideate`; `ideation_*` events).
- **dispatch** — runs each ready task as a fresh agent (`run_task`); the Active line in `ap2 status`, `task_solve` / `task_complete` events.
- **verifier** — runs the task's `## Verification` bullets (shell bullets + prose judge) plus the project-wide test gate; `task_verify` events.
- **auto-approve** — promotes Backlog proposals to dispatch unattended (the `auto-approve:` status line; opt out to gate each on `ap2 approve`).
- **operator queue** — applies operator-staged board ops each tick; the `operator_queue_drained` lines in `ap2 status` / events.
- **status report** — cron-scheduled board-snapshot agent (the `status-report` job and its events).
- **janitor** — cron-scheduled repo-hygiene scan; surfaces findings in `events.jsonl`.
- **web UI** — the read-only dashboard at `http://127.0.0.1:8729/` (the `web:` line; `AP2_WEB_DISABLED=1` to opt out).
- **Mattermost handler** — answers inbound chat ops when a channel is wired up (the communication component).

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

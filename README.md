# autopilot2

External Python daemon that drives a Claude Code project through a list of
tasks. Each unit of work runs as a fresh [Claude Agent SDK][sdk] `query()`
call; shared state lives on disk. The daemon never accumulates context, so
sessions don't degrade as work piles up.

[sdk]: https://github.com/anthropics/claude-agent-sdk-python

## Install

Requires Python 3.11+ and an Anthropic OAuth token (`claude setup-token`
to obtain one — the daemon reads `CLAUDE_CODE_OAUTH_TOKEN` from the env).

```bash
# As a uv tool (recommended — isolates ap2's deps from your projects)
uv tool install git+https://github.com/lzhang/autopilot2

# Or in a virtualenv
pip install git+https://github.com/lzhang/autopilot2

# Editable, for development
git clone https://github.com/lzhang/autopilot2
cd autopilot2
uv sync && uv pip install -e .
```

This installs the `ap2` console script.

## Quickstart

```bash
cd /path/to/your/repo
ap2 init                                    # scaffold TASKS.md + .cc-autopilot/
ap2 add "Refactor the foo helper" -s Backlog -d "Pull out the inline string parsing"

export CLAUDE_CODE_OAUTH_TOKEN=...          # required
ap2 start                                   # daemon runs in the background
ap2 status                                  # board state + daemon liveness
ap2 logs -n 20                              # tail recent events
```

Stop with `ap2 stop`. Pause without stopping with `ap2 pause` / `ap2 resume`.

For long-running work (>5 min) and OS-level isolation, see the
[sandbox runbook](plan/sandboxed-user-setup.md) — the daemon is designed to
run as a separate OS user (`claude-agent`) so its tools can't reach your
home, keychain, or other repos.

## What's in this repo

```
ap2/                          # the package — daemon, CLI, MCP tools, tests
├── README.md                 # operator reference + full CLI / event schema
├── architecture.md           # design rationale, agent kinds, verification model
└── howto.md                  # in-sandbox quick reference (installed via
                              #   `ap2 sandbox install-howto`)
plan/sandboxed-user-setup.md  # OS-level sandbox-user runbook
skills/                       # optional Claude Code slash commands
├── ap2/                      # /ap2 <project> — daemon snapshot
├── ap2-task/                 # /ap2-task <project> "<title>" — add to backlog
└── migrate-to-ap2/           # /migrate-to-ap2 — convert legacy TODO.md → TASKS.md
```

## Documentation

- **[ap2/README.md](ap2/README.md)** — operator quickstart, full CLI reference,
  configuration knobs, event schema.
- **[ap2/architecture.md](ap2/architecture.md)** — design rationale, the
  daemon loop, agent kinds, two-tier verification, sandbox model.
- **[plan/sandboxed-user-setup.md](plan/sandboxed-user-setup.md)** — runbook
  for setting up the `claude-agent` sandbox user.
- **[ap2/howto.md](ap2/howto.md)** — what the daemon-spawned agent sees when
  it runs inside an ap2-managed project.

## Tests

```bash
# Default: ~349 tests, fast, no API cost.
uv run pytest -q ap2/tests/

# Real-SDK smokes: opt-in. ~30s + a few cents per run.
AP2_REAL_SDK=1 uv run pytest ap2/tests/smoke/ -v -s
```

## License

MIT — see [LICENSE](LICENSE).

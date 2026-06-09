# autopilot2

External Python daemon that drives a Claude Code project through a list of
tasks. Each unit of work runs as a fresh agent dispatch through a pluggable
backend — [Claude Code][sdk] `query()` by default, with OpenAI Codex
selectable per agent kind (each backend brings its own auth). Shared state
lives on disk, so the daemon never accumulates context and sessions don't
degrade as work piles up.

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
ap2 init                                              # scaffold TASKS.md + .cc-autopilot/

# Add a task. `--briefing-file` is required (TB-135): the file holds the
# task's Goal/Scope/Verification — the auto-verifier reads it back later.
cat > /tmp/refactor-foo.md <<'EOF'
# Refactor the foo helper

## Goal
Pull out the inline string parsing.

## Verification
- `uv run pytest -q` — full suite passes.
EOF
ap2 add "Refactor the foo helper" -s Backlog --briefing-file /tmp/refactor-foo.md

export CLAUDE_CODE_OAUTH_TOKEN=...                    # required
ap2 start                                             # daemon runs in the background
ap2 status                                            # board state + daemon liveness
ap2 logs -n 20                                        # tail recent events
open http://127.0.0.1:8729/                           # bundled read-only web UI (TB-130)
```

Stop with `ap2 stop`. Pause without stopping with `ap2 pause` / `ap2 resume`.

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
└── howto.md                  # in-sandbox quick reference (deployed via
                              #   `ap2 sandbox sync-assets`)
sandboxed-user-setup.md       # OS-level sandbox-user runbook (repo root)
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
- **[sandboxed-user-setup.md](sandboxed-user-setup.md)** — runbook for
  setting up the `claude-agent` sandbox user.
- **[ap2/howto.md](ap2/howto.md)** — what the daemon-spawned agent sees when
  it runs inside an ap2-managed project.

## Tests

See [`ap2/README.md#tests`](ap2/README.md#tests) for the canonical test-suite
guide (default suite + real-SDK smokes).

## License

All rights reserved — see [LICENSE](LICENSE).

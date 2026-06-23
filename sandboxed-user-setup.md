# Sandboxed OS user for Claude autopilot daemons

**Status:** maintained deployment runbook — this is how ap2 daemons are
provisioned in production (the daemon backing this repo runs as the
`claude-agent` user per the phases below). Sections marked *needs human
execution* require `sudo` and should be run interactively by the repo
owner, not by autopilot.

## Goal

One shared OS user (`claude-agent`) hosts every Claude autopilot daemon on
this machine. Each project gets its own isolated clone under the shared
user's home. The trust boundary is "human vs agent" (strong), not "agent A
vs agent B" (left intentionally weak — the human wrote both projects).

The SDK is configured with `permission_mode="bypassPermissions"`, so OS-level
isolation is the actual trust boundary: anything the agent does is bounded
by what that user can read and write.

## Why one shared user, not one per project?

- The trust concern is human-vs-agent, not agent-A-vs-agent-B.
- One set of provisioning steps, one credential deny list, one
  launchd/systemd wiring to maintain.
- If a specific project handles credentials the others shouldn't see,
  provision a dedicated user for it (same runbook, different name).

## Why a separate clone, not a shared directory?

- `bypassPermissions` means any filesystem mistake lands on disk. A shared
  working directory exposes the human's uncommitted work to an errant
  `git reset --hard` or `rm -rf .`.
- Separate clones preserve the "a mistake is recoverable" invariant — the
  agent can at worst damage its own clone, which is reseedable from the
  human's clone.
- Human visibility is handled separately (Phase 4) by making the agent's
  clone group-readable.

## What's already in place (code-level)

The daemon passes `setting_sources=["project"]` to `ClaudeAgentOptions`, so
only project-level `.claude/settings.json` hooks load; the user-level
cc-perms hook does not apply to daemon sessions. No code change needed here.

## Helper CLI

Exposed as an `ap2 sandbox` subcommand group so the provisioning logic
lives with the daemon code. `ap2/README.md`'s "Sandbox subcommands" table
is the authoritative reference; the quick map:

```
ap2 sandbox user-audit [user]                        # credential deny-list check
ap2 sandbox user-setup [user] [-y]                   # create the OS user (prompts before sudo)
ap2 sandbox install-token [user]                     # write CLAUDE_CODE_OAUTH_TOKEN → ~user/.zshenv
ap2 sandbox install-mm [user]                        # write MATTERMOST_URL + MATTERMOST_TOKEN → ~user/.zshenv
ap2 sandbox install-channel <project> <channel>      # resolve #channel → ID, write to <project>/.cc-autopilot/env
ap2 sandbox sync-assets [user] [--sbuser] [--apply]  # deploy <repo>/skills/* into the runtime skills roots (+ Codex AGENTS.md)
ap2 sandbox project-setup <source-repo> [--user u]   # clone the source repo into ~user/repos/
ap2 sandbox project-audit <project-path> [--user u]  # verify a sandbox clone is correctly isolated
```

`[user]` defaults to `claude-agent`. `user-setup`, `project-setup`, and the
`install-*` verbs print the exact `sudo` commands they will run and prompt
for approval before executing; `-y` (where supported) skips the prompt for
scripted use. `sync-assets` is dry-run by default — pass `--apply` to copy.

## Credential deny list (shared, verified by `user-audit`)

`claude-agent`'s home must not contain:

```
~/.ssh/id_*               # SSH private keys
~/.netrc                  # HTTP auth tokens
~/.aws/                   # AWS creds
~/.config/gcloud/         # Google Cloud creds
~/.docker/config.json     # registry tokens
~/.config/gh/             # GitHub CLI creds
~/.npmrc (with _authToken)
```

Its environment must not expose:

```
GH_TOKEN, GITHUB_TOKEN, AWS_*, GOOGLE_APPLICATION_CREDENTIALS,
ANTHROPIC_API_KEY (unless the SDK explicitly requires it),
MATTERMOST_TOKEN (only if the daemon uses mattermost — scope to a bot token).
```

## Phase 0 — one-time user creation (needs human execution)

```bash
python -m ap2 sandbox user-setup          # shows sudo plan, prompts, runs
python -m ap2 sandbox user-audit          # verify it's clean
```

### macOS (dscl)

`user-setup` picks the next unused UID >500 and prints:

```bash
sudo dscl . -create /Users/claude-agent
sudo dscl . -create /Users/claude-agent UserShell /bin/zsh
sudo dscl . -create /Users/claude-agent RealName "Claude Autopilot Agent"
sudo dscl . -create /Users/claude-agent UniqueID <computed>
sudo dscl . -create /Users/claude-agent PrimaryGroupID 20   # staff
sudo dscl . -create /Users/claude-agent NFSHomeDirectory /Users/claude-agent
sudo createhomedir -c -u claude-agent
# no password — login only via `sudo -u claude-agent`
```

### Linux (useradd)

```bash
sudo useradd --create-home --shell /bin/bash claude-agent
sudo passwd -l claude-agent   # lock the account
```

## Phase 1 — resource caps (needs human execution)

### macOS (launchd)

Per-project plist with `SoftResourceLimits` and `HardResourceLimits`.
Template: `adhoc/com.local.claude-agent.<project>.plist` (create per-project).
For interactive runs:

```bash
sudo -u claude-agent zsh -c 'ulimit -n 1024; ulimit -v 4194304; python -m ap2 start'
```

### Linux (systemd)

Per-project unit `/etc/systemd/system/claude-agent-<project>.service`:

```ini
[Unit]
Description=Claude autopilot daemon for <project>
After=network.target

[Service]
Type=simple
User=claude-agent
Group=claude-agent
WorkingDirectory=/home/claude-agent/repos/<project>
ExecStart=/home/claude-agent/repos/<project>/.venv/bin/python -m ap2 start
MemoryMax=4G
TasksMax=256
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

## Phase 2 — per-project clone (run once per project)

```bash
python -m ap2 sandbox project-setup /Users/lzhang/dev/atom/claude
```

This does, as `claude-agent` via `sudo -u`:

1. `mkdir -p ~/repos`
2. Verify the agent can read the source; stop with a hint if not.
3. `git clone --origin upstream <src> ~/repos/<name>` (local clone uses
   hardlinks where possible — cheap on disk).
4. `git remote set-url --push upstream /dev/null` (upstream is read-only).
5. `git init --bare ~/repos/<name>-local.git`
6. `git remote add local ~/repos/<name>-local.git`
7. Runs `project-audit` to verify.

The daemon also carries `disallowed_tools=["Bash(git push*)"]` — belt and
suspenders.

## Phase 3 — per-project venv (needs human execution)

```bash
sudo -u claude-agent -i
cd ~/repos/<project>
uv sync --extra ap2      # installs claude-agent-sdk
```

## Phase 4 — shared state between human + daemon

The daemon owns `TASKS.md`, `.cc-autopilot/progress.md`, `events.jsonl`, and
the briefings directory. Human needs visibility.

### Option A — group-readable clone (recommended)

- Daemon runs with `umask 027`; group gets read.
- macOS: `staff` is shared. Linux: `gpasswd -a lzhang claude-agent`.
- Human reads `/Users/claude-agent/repos/<project>/TASKS.md` directly.
- To push tasks in: commit to `~/repos/<project>-local.git` from the
  human's own clone, or message `@claude-bot add task: …`.

### Option B — pushed mirror branch

- Daemon pushes state to an `autopilot-state` branch on the project remote.
- Downside: noisy commit history, extra push in the loop.

### Option C — read-only HTTP view

- Static-file server exposes `.cc-autopilot/` on localhost.
- Downside: extra moving part; no value if (A) works.

**Start with A.**

## Phase 5 — per-project smoke test

```bash
python -m ap2 sandbox project-audit ~/repos/<project>
python -m ap2 sandbox user-audit

sudo -u claude-agent -i
cd ~/repos/<project>
python -m ap2 start
python -m ap2 add "Smoke test: write hello to adhoc/hello.txt"
sleep 60
python -m ap2 status
cat adhoc/hello.txt
python -m ap2 stop

# Escape test — agent should not reach the human's home.
ls -la /Users/lzhang/.ssh 2>&1 | head -3   # expect: Permission denied
```

## Resolved design decisions

- **Daemon credentials live in `~claude-agent/.zshenv`, not per-project.**
  The shared user holds `CLAUDE_CODE_OAUTH_TOKEN` (via `install-token`) and,
  for projects that use the Mattermost integration, `MATTERMOST_URL` +
  `MATTERMOST_TOKEN` (via `install-mm`). Per-project secrets that should
  NOT be visible across projects (e.g. a project-scoped Mattermost bot
  token) get a dedicated sandbox user via this same runbook with a
  different name.
- **macOS and Linux are both supported first-class.** `user-setup` /
  `project-setup` detect the platform and emit `dscl`/`createhomedir` on
  macOS and `useradd`/`passwd -l` on Linux. The Phase 1 daemon-launch
  wiring is platform-specific (launchd plist vs. systemd unit); pick the
  one matching your target.
- **Per-project clones live under `~claude-agent/repos/<project>/`.**
  Project names must be unique within that directory; if you run two repos
  with the same basename, rename the clone or provision a dedicated
  sandbox user. The daemon does not auto-namespace.

# Sandboxed OS user for the ap2 daemon

**Status:** draft runbook. Sections marked *needs human execution* require
`sudo` and should be run interactively by the repo owner, not by autopilot.

## Goal

Run the ap2 autopilot daemon under a dedicated macOS/Linux user (`ap2-agent`),
isolated from the human developer account. The SDK is configured with
`permission_mode="bypassPermissions"`, so OS-level isolation is the trust
boundary: anything the agent does is bounded by what that user can read and
write.

## Why a separate user?

- `bypassPermissions` skips all permission prompts. Without OS isolation,
  the agent has whatever the human user has — SSH keys, cloud tokens, shell
  history, other projects.
- With a separate user:
  - No read access to `~/.ssh`, `~/.netrc`, `~/.aws`, `~/.config/gcloud`,
    `~/.zshrc`, browser cookies, Keychain, etc.
  - No write access to anything outside the agent's home and its working
    copy of the repo.
  - A mistake like `rm -rf .` affects only the agent's clone, which is a
    local copy that can be reseeded from a bare remote.

## What's already in place

Before doing any OS work, the daemon is already safe against one failure
mode: **the cc-perms hook is effectively off for ap2 daemon sessions.** The
daemon passes `setting_sources=["project"]` to `ClaudeAgentOptions`, so only
project-level `.claude/settings.json` hooks load; the cc-perms hook is wired
in user-level `~/.claude/settings.json` and does not apply here. Item (6) of
the TB-57 scope is therefore a no-op on the code side — this doc simply
records the invariant.

## Pre-flight checklist (for the ap2-agent user)

When the user is created, its home directory **must not contain**:

```
~/.ssh/id_*          # any private SSH keys
~/.netrc             # HTTP auth tokens
~/.aws/              # AWS creds
~/.config/gcloud/    # Google Cloud creds
~/.docker/config.json # registry tokens
~/.github_token, ~/.gh, ~/.config/gh/  # GitHub CLI creds
~/.npmrc (with _authToken)
```

Environment must not expose:

```
GH_TOKEN, GITHUB_TOKEN, AWS_*, GOOGLE_APPLICATION_CREDENTIALS,
ANTHROPIC_API_KEY (unless explicitly needed by the SDK),
MATTERMOST_TOKEN (only if mattermost integration is desired — and in that
case scope it to a bot token, not a personal token).
```

`adhoc/setup_sandbox_user.sh` verifies these via a non-destructive audit.

## Phase 1 — create the user (needs human execution)

### macOS (dscl)

Choose a UID above 500 that isn't in use:

```bash
NEXT_UID=$(dscl . -list /Users UniqueID | awk '{print $2}' | sort -n | tail -1)
NEW_UID=$((NEXT_UID + 1))

sudo dscl . -create /Users/ap2-agent
sudo dscl . -create /Users/ap2-agent UserShell /bin/zsh
sudo dscl . -create /Users/ap2-agent RealName "ap2 Autopilot Agent"
sudo dscl . -create /Users/ap2-agent UniqueID "$NEW_UID"
sudo dscl . -create /Users/ap2-agent PrimaryGroupID 20   # staff
sudo dscl . -create /Users/ap2-agent NFSHomeDirectory /Users/ap2-agent
sudo createhomedir -c -u ap2-agent
# no password — login only via `sudo -u ap2-agent`
```

### Linux (useradd)

```bash
sudo useradd --create-home --shell /bin/bash ap2-agent
sudo passwd -l ap2-agent   # lock the account; use sudo -u to enter
```

## Phase 2 — resource caps (needs human execution)

### macOS (launchd)

Put the daemon under launchd control with `SoftResourceLimits` and
`HardResourceLimits` in the plist. Template:
`adhoc/com.local.ap2-agent.plist` (to be created). For a human-interactive
run, just use `ulimit`:

```bash
sudo -u ap2-agent zsh -c 'ulimit -n 1024; ulimit -v 4194304; python -m ap2 start'
```

### Linux (systemd)

Service unit file `/etc/systemd/system/ap2-agent.service`:

```ini
[Unit]
Description=ap2 autopilot daemon
After=network.target

[Service]
Type=simple
User=ap2-agent
Group=ap2-agent
WorkingDirectory=/home/ap2-agent/claude
ExecStart=/home/ap2-agent/claude/.venv/bin/python -m ap2 start
MemoryMax=4G
TasksMax=256
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

## Phase 3 — dedicated clone + read-only remote

Per the safety principle "a mistake is recoverable," the daemon should push
only to a local bare repo, never to the canonical remote.

```bash
# As the human, once:
sudo -u ap2-agent mkdir -p /Users/ap2-agent/claude
sudo -u ap2-agent git clone --origin upstream \
    git@github.com:owner/claude.git /Users/ap2-agent/claude

# Replace the `origin` remote with a local bare repo the daemon can push to.
sudo -u ap2-agent git -C /Users/ap2-agent/claude init --bare \
    /Users/ap2-agent/claude-local.git
sudo -u ap2-agent git -C /Users/ap2-agent/claude remote add \
    local /Users/ap2-agent/claude-local.git
# Keep `upstream` read-only — disable push URL.
sudo -u ap2-agent git -C /Users/ap2-agent/claude remote set-url --push \
    upstream /dev/null
```

The daemon still carries `disallowed_tools=["Bash(git push*)"]` — belt and
suspenders.

## Phase 4 — skills + venv (needs human execution)

```bash
# As ap2-agent:
sudo -u ap2-agent -i
cd ~/claude
./update-skills.sh           # creates ~/.claude/venvs/cc-perms/
uv sync --extra ap2           # installs claude-agent-sdk
```

## Phase 5 — shared state between human + daemon

The daemon owns `TASKS.md`, `.cc-autopilot/progress.md`, `events.jsonl`, and
the briefings directory. The human needs visibility. Three options, pick one:

### Option A — group-readable clone (simplest, recommended)

- Daemon owns files, `umask 027` so group has read.
- Human user is in the daemon's group (`staff` on macOS is shared; on Linux
  add with `gpasswd -a lzhang ap2-agent`).
- Human reads `/Users/ap2-agent/claude/TASKS.md` directly.
- **Downside:** human can't add tasks to the daemon's board without a write
  channel. Use mattermost (`@claude-bot add task: …`) or push to
  `claude-local.git` from the human's own clone.

### Option B — pushed mirror branch

- Daemon pushes its state (TASKS.md, progress.md, events.jsonl as commits)
  to a branch on the human's remote repo, e.g. `autopilot-state`.
- Human pulls to see progress.
- **Downside:** noisy commit history, extra push step in the daemon loop.

### Option C — read-only HTTP view

- A tiny static-file server exposes the daemon's `.cc-autopilot/` and
  `TASKS.md` over localhost HTTP (read-only).
- Human opens `http://localhost:8765/progress.md` in a browser.
- **Downside:** extra moving part; no value if option A works.

**Recommendation: start with A. It's the cheapest and preserves the
isolation boundary (the human still can't write to the daemon's files).**

## Phase 6 — smoke test (needs human execution)

```bash
sudo -u ap2-agent -i
cd ~/claude

# Pre-flight check
./adhoc/setup_sandbox_user.sh audit

# Start the daemon, watch one task run
python -m ap2 start
python -m ap2 add "Smoke test: write hello to adhoc/hello.txt"
sleep 60
python -m ap2 status
cat adhoc/hello.txt   # should exist
python -m ap2 stop

# Escape test: confirm the daemon can't touch the human's home.
# (Reviewing the events.jsonl for any attempts is optional; the OS enforces.)
ls -la /Users/lzhang/.ssh 2>&1 | head -3   # expect: Permission denied
```

## Open questions / decisions still owed

1. Should the daemon have a Mattermost/Anthropic token? If yes, scope those
   tokens to the ap2-agent user's keychain/shell env only — not shared.
2. Target OS for production: macOS dev box or a Linux server? Runbook
   covers both; preflight script only implements macOS paths today.
3. Should `adhoc/setup_sandbox_user.sh` be idempotent (re-runnable) or
   one-shot? Idempotent is nicer but twice the code.

## Related tasks

- TB-47 scaffolded the daemon with `bypassPermissions` in mind.
- TB-48 added retry / timeout bounds — important here because a poisoned
  task can't spin forever burning the daemon user's CPU.
- TB-54/55/56 are e2e tests that run in-process with a fake SDK; they do
  NOT validate the OS sandbox (by design). This runbook covers that gap
  for the real deployment.

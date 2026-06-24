# Sandbox setup — running ap2 as an isolated OS user

ap2 is autonomous by default: once it's running, agents edit files in your repo
and run shell commands **unattended**. The quickstart in the [README](README.md)
runs the daemon as *your own user*, which is fine for trying it out — but for
unattended or long-running use you don't want an agent's shell tools to be able
to touch your SSH keys, your keychain, or your other repos.

This guide sets ap2 up the way it's meant to run in production: as a dedicated,
locked-down OS user named `claude-agent`, working on its own clone of your repo.
If anything goes wrong, the blast radius is that one clone — never your real
account.

You don't have to memorize any of the steps below. The `ap2 sandbox` subcommands
do the work; each one that needs `sudo` **prints exactly what it will run and
asks before doing it**.

## The model in one minute

- **A separate OS user (`claude-agent`)** runs the daemon. Its home has none of
  your credentials, so the trust boundary is simply *you vs. the agent* — and
  the OS enforces it. (ap2 runs the agent SDK with permissions bypassed, so this
  OS-level boundary is the real one.)
- **A separate clone** under that user's home is what the daemon edits and
  commits to. Your own working copy is never touched, so a bad `git reset` or
  `rm` can only damage the reseedable clone.
- **One shared user is fine for many projects.** The concern is human-vs-agent,
  not project-A-vs-project-B (you wrote both). If one project handles secrets the
  others shouldn't see, run this same guide again with a different user name.

## Before you start

- ap2 installed and on your `PATH` (`ap2 --help` works) — see the README's
  Install section.
- `sudo` access on this machine (creating a user needs it).
- The repo you want ap2 to drive, plus your `CLAUDE_CODE_OAUTH_TOKEN`
  (`claude setup-token`).
- macOS or Linux — both are first-class; the only platform-specific part is how
  you keep the daemon running (Step 6).

Everything below uses the default user name `claude-agent`. Every command takes
an optional `[user]` if you want a different name.

## Step 1 — create the sandbox user

```bash
ap2 sandbox user-setup          # shows the exact sudo plan, prompts, then runs
ap2 sandbox user-audit          # confirms the home holds none of your secrets
```

`user-setup` creates a passwordless, **login-disabled** account (you reach it
only via `sudo -u claude-agent`) on the right platform automatically — `dscl` +
`createhomedir` on macOS, `useradd` + `passwd -l` on Linux. In the same run it
offers to install your `CLAUDE_CODE_OAUTH_TOKEN` into the user's `~/.zshenv` so
the daemon can authenticate, **and deploys ap2's operator skills** into the
user's skills roots — so a Claude Code or Codex session in the project drives
ap2 agent-first (see the README's **Agent skills** section). `user-audit` is your
safety check — it verifies the deny list in
[Credential deny list](#credential-deny-list) below.

> After an ap2 upgrade brings new skill bundles, re-sync them with
> `ap2 sandbox sync-assets <user> --apply` (dry-run without `--apply`).

> Already have the user, or want to (re)install credentials later?
> `ap2 sandbox install-token` writes `CLAUDE_CODE_OAUTH_TOKEN`, and
> `ap2 sandbox install-mm` writes the Mattermost URL + token, both into
> `~claude-agent/.zshenv`.

## Step 2 — clone your project into the sandbox

```bash
ap2 sandbox project-setup /path/to/your/repo
```

This creates the isolated working copy the daemon drives, under
`~claude-agent/repos/<name>/`. As the `claude-agent` user it:

1. makes `~/repos` and verifies the user can read your source repo,
2. clones it (`--origin upstream`, using hardlinks where possible — cheap on
   disk),
3. **sets the `upstream` push URL to `/dev/null`** so the daemon can never push
   back to your repo,
4. adds a local bare remote (`~/repos/<name>-local.git`) for the daemon's own
   history, and
5. runs `project-audit` to confirm the clone is correctly isolated.

The daemon also refuses `git push` at the tool level — belt and suspenders.

> If two of your repos share a basename, rename the clone or use a separate
> sandbox user — clones aren't auto-namespaced.

## Step 3 — verify the isolation

```bash
ap2 sandbox project-audit ~claude-agent/repos/<project>   # clone is isolated
ap2 sandbox user-audit                                     # home is clean

# Escape test — the agent's user must NOT be able to read your home:
sudo -u claude-agent ls -la /Users/<you>/.ssh 2>&1 | head -3   # expect: Permission denied
```

If the audits pass and the escape test is denied, the sandbox is sound.

## Step 4 — run it (and keep it running)

The daemon runs **as `claude-agent`**, so `ap2` must be installed for *that*
user — the copy in your own `~/.local` isn't reachable from the sandbox (and
reaching into your home would defeat the isolation). Install it once inside the
sandbox user's home:

```bash
sudo -u claude-agent -i
uv tool install git+https://github.com/atom-tech-consulting/autopilot2
which ap2          # note this path — you'll need it for the service unit below
```

Then a quick interactive run, from that same shell:

```bash
cd ~/repos/<project>
ap2 start
ap2 status          # board + daemon liveness
ap2 stop
```

For unattended operation, run the daemon under your init system with a memory
cap so a runaway can't take the box down.

**macOS (launchd)** — a per-project `LaunchDaemon` plist with
`SoftResourceLimits` / `HardResourceLimits`. For a one-off capped run:

```bash
sudo -u claude-agent zsh -c 'ulimit -n 1024; ulimit -v 4194304; ap2 start'
```

**Linux (systemd)** — `/etc/systemd/system/claude-agent-<project>.service`:

```ini
[Unit]
Description=Claude autopilot daemon for <project>
After=network.target

[Service]
Type=simple
User=claude-agent
Group=claude-agent
WorkingDirectory=/home/claude-agent/repos/<project>
ExecStart=/home/claude-agent/.local/bin/ap2 start    # use the `which ap2` path from above
MemoryMax=4G
TasksMax=256
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

## Seeing what the daemon is doing

The daemon owns `TASKS.md`, `.cc-autopilot/progress.md`, `events.jsonl`, and the
briefings — all in the sandbox clone, which you'll want to read from your own
account. The simplest setup (recommended) is a **group-readable clone**:

- the daemon runs with `umask 027`, so the group gets read access;
- on macOS the shared `staff` group already covers it; on Linux, add yourself
  with `sudo gpasswd -a <you> claude-agent`;
- then read `~claude-agent/repos/<project>/TASKS.md` (and the web UI at
  `http://127.0.0.1:8729/`) directly.

To hand work *to* the daemon, commit to its local bare remote
(`~claude-agent/repos/<project>-local.git`) from your own clone, or — if you've
wired up the Mattermost integration — message the bot (`add task: …`).

## Credential deny list

`user-audit` enforces this — the sandbox user's home must **not** contain:

```
~/.ssh/id_*               # SSH private keys
~/.netrc                  # HTTP auth tokens
~/.aws/                   # AWS creds
~/.config/gcloud/         # Google Cloud creds
~/.docker/config.json     # registry tokens
~/.config/gh/             # GitHub CLI creds
~/.npmrc (with _authToken)
```

…and its environment must **not** expose:

```
GH_TOKEN, GITHUB_TOKEN, AWS_*, GOOGLE_APPLICATION_CREDENTIALS,
ANTHROPIC_API_KEY (unless the SDK explicitly requires it),
MATTERMOST_TOKEN (only if the daemon uses Mattermost — scope it to a bot token).
```

The daemon's own credentials live in `~claude-agent/.zshenv`
(`CLAUDE_CODE_OAUTH_TOKEN`, and optionally the Mattermost URL + token), shared
across that user's projects. Project-scoped secrets that must not be visible
across projects belong to a **dedicated** sandbox user — run this guide again
with a different name.

## The `ap2 sandbox` verbs at a glance

`[user]` defaults to `claude-agent`. The `user-setup`, `project-setup`, and
`install-*` verbs print the exact `sudo` they'll run and prompt first (`-y`
skips the prompt where supported). `sync-assets` is dry-run until `--apply`.

```
ap2 sandbox user-setup [user] [-y]                   # create the OS user
ap2 sandbox user-audit [user]                        # credential deny-list check
ap2 sandbox install-token [user]                     # CLAUDE_CODE_OAUTH_TOKEN → ~user/.zshenv
ap2 sandbox install-mm [user]                        # MATTERMOST_URL + token  → ~user/.zshenv
ap2 sandbox install-channel <project> <channel>      # resolve #channel → ID, write to project env
ap2 sandbox sync-assets [user] [--apply]             # deploy skills/* + AGENTS.md into the sandbox
ap2 sandbox project-setup <source-repo> [--user u]   # clone your repo into ~user/repos/
ap2 sandbox project-audit <project-path> [--user u]  # verify a clone is isolated
```

`ap2/README.md`'s "Sandbox subcommands" table is the authoritative reference.

## Why it's built this way

- **One shared user, not one per project.** The trust concern is human-vs-agent;
  one set of provisioning steps, one deny list, one daemon-launch wiring to
  maintain. Carve out a dedicated user only when a project holds secrets the
  others shouldn't see.
- **A separate clone, not a shared directory.** With permissions bypassed, any
  filesystem mistake lands on disk. A separate clone keeps "a mistake is
  recoverable" true — the worst case is reseeding the agent's clone from yours.
  Your visibility is handled by the group-read setup above, not by sharing the
  working tree.
- **No code changes needed.** The daemon loads only project-level
  `.claude/settings.json` (`setting_sources=["project"]`), so your user-level
  Claude Code hooks don't apply to daemon sessions.

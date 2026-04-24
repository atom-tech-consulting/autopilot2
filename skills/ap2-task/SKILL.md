---
name: ap2-task
description: "Add a task to an ap2 sandbox project's Backlog using its `ap2 add` command (locks TASKS.md, issues a fresh TB-N, no ID collisions). Auto-detects whether to run as the sandbox user directly or escalate via sudo from the main user."
user_invocable: true
---

<command-name>ap2-task</command-name>

# ap2-task — add a task to a sandbox project

Wrap `ap2 sandbox add` for the common case: drop a free-text task into a project's Backlog. The daemon's ideation/reflector loop picks it up on the next cycle.

## Usage

```
/ap2-task <project> <content>
```

- `<project>`: bare name like `stoch` (resolved to `/Users/claude-agent/repos/<name>`) or an absolute path containing `.cc-autopilot/`.
- `<content>`: free text describing the work. Can be a single line or a multi-line block. Convention:
  - First line / first sentence → task **title**.
  - Remainder → task **description**.
  - Any `#hashtag` tokens are pulled out as **tags**.

If no project arg is given, list `~claude-agent/repos/*` and ask which one.

## Why this skill exists (and not just direct `ap2 add`)

Editing `TASKS.md` directly from the human's clone causes ID collisions and merge conflicts with the daemon's in-flight edits — we hit that exact bug earlier in stoch (TB-15 README clashed with daemon's TB-15 strategy report). `ap2 add` runs *inside the sandbox*, takes the `TASKS.md.lock`, and issues IDs from the daemon's authoritative state.

## Steps

### 1. Resolve `PROJECT_ROOT`

- Bare name → `/Users/claude-agent/repos/<name>`.
- Absolute path → use as-is, but verify `<path>/.cc-autopilot/` exists.
- Verify the sandbox clone exists with `test -d <PROJECT_ROOT>/.cc-autopilot`. If it doesn't, stop and report — point the user at `/setup-project` + `ap2 sandbox project-setup`.

### 2. Parse `<content>` into title / description / tags

- **Title**: the first non-empty line, or the first sentence if the content is a single block. Keep under ~70 chars; trim trailing punctuation. Imperative verb preferred ("Add X", "Write Y", "Fix Z"). If the content is too vague to derive a clear title, ask once for a title.
- **Description**: the full content (yes, including the title line). Pass via `-d`. If the content is a single short line that's basically the title, omit `-d`.
- **Tags**: extract any `#word` tokens from the content, lowercased, with the `#` stripped (the CLI re-adds it). If none are present, infer 1-2 from project context (e.g. `#docs`, `#data`, `#engine`, `#strategy`, `#metrics`, `#reporting`, `#infra`, `#risk`, `#validation`, `#tooling`, `#cli`). Don't over-tag — 1-2 is plenty.

### 3. Detect who's running this skill

Run `whoami` (or read `$USER`). The result determines whether sudo is needed:

- **`claude-agent`** → already inside the sandbox; call `ap2` directly. No sudo.
- **anything else** (typically `lzhang`) → escalate via `sudo -u claude-agent`.

### 4. Compose and run the command

`ap2` is on lzhang's PATH at `/Users/lzhang/.local/bin/ap2`; claude-agent inherits the same install via the editable uv tool venv (the daemon process itself runs out of `/Users/lzhang/.local/share/uv/tools/claude-automation/bin/python`). Use the absolute path either way to avoid PATH ambiguity under sudo.

**Sandbox user (no sudo):**

```
/Users/lzhang/.local/bin/ap2 --project <PROJECT_ROOT> add "<title>" -s Backlog -t <tag1> <tag2> -d "<description>"
```

**Main user (sudo to claude-agent):**

```
sudo -u claude-agent /Users/lzhang/.local/bin/ap2 --project <PROJECT_ROOT> add "<title>" -s Backlog -t <tag1> <tag2> -d "<description>"
```

Quote the title and description with double quotes. If the description contains double quotes, escape them (`\"`) or wrap the `-d` value in single quotes. Keep the whole invocation on one line — no heredocs, no line breaks.

Run it via Bash directly. Expected single-line output: `added TB-N to Backlog`. If sudo prompts for a password and the call fails, fall back to printing `! sudo …` for the human to execute.

### 5. Report the result

Briefly state:
- The new **TB-N** (parse from the command's stdout).
- That the daemon will auto-promote it from Backlog → Ready → Active on the next loop tick (typically <1 minute when Ready is empty).
- Suggest `/ap2 <project>` if they want to verify.

Do **not** edit `TASKS.md` or `CLAUDE.md` in the human's local clone — the sandbox is the source of truth for the board now.

## Default section

Backlog. Don't dump tasks straight into Ready — Ready means "briefing prepared", and we're not preparing a briefing here. The ideation/reflector flow handles promotion.

If the user explicitly says "ready" or "frozen" in their request, override accordingly.

## Examples

```
/ap2-task stoch Write a CONTRIBUTING.md covering setup, tests, code style. #docs
```

→ resolves to `/Users/claude-agent/repos/stoch`, title "Write CONTRIBUTING.md", description = full content, tags = `[docs]`. Run as lzhang:

```
sudo -u claude-agent /Users/lzhang/.local/bin/ap2 --project /Users/claude-agent/repos/stoch add "Write CONTRIBUTING.md covering setup, tests, code style" -s Backlog -t docs -d "Write a CONTRIBUTING.md covering setup, tests, code style. #docs"
```

Run as claude-agent (no sudo, same flags):

```
/Users/lzhang/.local/bin/ap2 --project /Users/claude-agent/repos/stoch add "Write CONTRIBUTING.md covering setup, tests, code style" -s Backlog -t docs -d "Write a CONTRIBUTING.md covering setup, tests, code style. #docs"
```

```
/ap2-task stoch Add a `--quiet` flag to backtest CLI that suppresses INFO logs. Useful for CI runs where summary.json is the only output we need.
```

→ title "Add --quiet flag to backtest CLI", tags inferred as `[cli]`, description = full content.

## Rules

- **Sandbox is canonical.** Never write to `TASKS.md` in the human's local clone — even if the daemon has uncommitted changes, the sandbox's state is what matters.
- **One task per invocation.** No batching. Re-run for additional tasks.
- **No briefing files.** This skill creates Backlog entries only; briefings are created later by the daemon's prep step (or `/tb prep TB-N` if you want to do it manually).
- **Single line.** Keep the command on one line — no heredocs, no line continuations. Long descriptions still go on one line; quote-escape as needed.
- **Run sudo directly when invoking from the main user.** Call it through Bash like any other command. Only fall back to handing the `! sudo …` form to the user if sudo prompts for a password and the call fails.

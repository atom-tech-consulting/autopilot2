---
name: ap2-task
description: "Add a task to an ap2 sandbox project's Backlog by authoring a briefing markdown file and passing it to `ap2 add --briefing-file` (locks TASKS.md, issues a fresh TB-N, no ID collisions). Auto-detects whether to run as the sandbox user directly or escalate via sudo from the main user."
user_invocable: true
---

<command-name>ap2-task</command-name>

# ap2-task — add a task to a sandbox project

Wrap `ap2 add` for the common case: drop a briefing-backed task into a project's Backlog. The daemon's ideation/reflector loop picks it up on the next cycle.

## Usage

```
/ap2-task <project> <content>
```

- `<project>`: bare name like `stoch` (resolved to `~$AP2_SANDBOX_USER/repos/<name>`, where `AP2_SANDBOX_USER` defaults to `claude-agent`) or an absolute path containing `.cc-autopilot/`.
- `<content>`: free text describing the work. Can be a single line or a multi-line block. You will SHAPE this into a briefing markdown file before invoking `ap2 add`:
  - First line / first sentence → briefing **H1** (the task title).
  - Remainder → briefing's `## Goal` / `## Scope` prose.
  - Any `#hashtag` tokens go on a `Tags:` line.

If no project arg is given, list `~$AP2_SANDBOX_USER/repos/*` and ask which one.

## Why this skill exists (and not just direct `ap2 add`)

Editing `TASKS.md` directly from the human's clone causes ID collisions and merge conflicts with the daemon's in-flight edits. `ap2 add` runs *inside the sandbox*, takes the `TASKS.md.lock`, and issues IDs from the daemon's authoritative state.

## ap2 add now requires a briefing file (TB-135)

`ap2 add` no longer accepts free-form `-t`/`-d`/positional title args by themselves. You **must** supply a briefing one of three ways — title and tags are parsed from the briefing's H1 and an optional `Tags:` line:

- `--briefing-file <path>` — point the flag at a markdown file you wrote.
- `--briefing-file -` — read the briefing from stdin (this skill's default — no temp file).
- `ap2 add` with no args + `$EDITOR` set — git-commit-style: opens `$EDITOR` against the template and uses the saved buffer. Aborting the editor (empty save, unchanged template, non-zero exit) makes `ap2 add` exit non-zero. **This skill's flow uses `--briefing-file -` instead** so it works headless without an interactive terminal; the editor flow is for human operators at a tty.

The pre-TB-135 auto-fill skeleton is gone — without a real `## Verification` section the per-task verifier had no scope-specific scoring and tasks "passed" on the regression gate alone (TB-131 hit this on 2026-04-30).

**The canonical briefing template lives in `ap2/init.py:BRIEFING_TEMPLATE`** — H1, optional `Tags:` line, `## Goal`, `## Scope`, `## Design`, `## Verification`, `## Out of scope`. The daemon's per-task verifier (TB-69) reads `## Verification` to score the task's commit, so include at least one concrete shell or prose bullet there beyond the project-wide regression gate.

## Steps

### 1. Resolve `PROJECT_ROOT`

- Bare name → resolve via `eval echo "~${AP2_SANDBOX_USER:-claude-agent}/repos/<name>"`.
- Absolute path → use as-is, but verify `<path>/.cc-autopilot/` exists.
- Verify the sandbox clone exists with `test -d <PROJECT_ROOT>/.cc-autopilot`. If it doesn't, stop and report — point the user at `ap2 sandbox project-setup`.

### 2. Author the briefing markdown

Compose a briefing buffer matching the template in `ap2/init.py:BRIEFING_TEMPLATE`. Minimum required:

```markdown
# <one-line task title>

Tags: #area #kind

## Goal

<one paragraph: what success looks like, why this matters>

## Scope

- <file or module to change>

## Verification

- `uv run pytest -q` — full suite passes
- <one or more concrete shell or prose bullets the daemon's verifier
  (TB-69) can score against the task's commit diff>
```

Conventions:

- **Title (H1)**: `# Title` — the first H1 is parsed as the task title. Keep under ~70 chars, imperative verb preferred ("Add X", "Write Y"). Do NOT prefix with `TB-N`; the daemon allocates the ID. (If you copy from a daemon-prepped briefing where the H1 is already `TB-N — Title`, the parser strips `TB-N — ` for you.)
- **Tags line**: optional `Tags: #cli #helpers` (case-insensitive prefix). Tokens are `#`-prefixed words OR comma-separated bare words; both shapes round-trip onto the rendered task line. If you also pass `-t #extra` to `ap2 add`, those are appended (deduped) on top of the briefing's tags.
- **Verification**: at least one bullet beyond the project-wide pytest gate. Shell bullets (backtick-prefixed) run automatically; prose bullets are judged by an SDK call against the diff. Empty `## Verification` means the verifier scores nothing scope-specific.
  - **Auto-verifiable bullets only (TB-138).** Every `## Verification` bullet must be auto-verifiable — one of: (1) a backticked shell command the verifier can `/bin/sh -c`, (2) a unit/e2e test name the regression gate covers, or (3) a prose claim that names a concrete file/symbol an SDK judge can `Read`/`Grep` against the diff. **No `Manual:` bullets.** No "operator runs X live and observes Y" — the verifier runs unattended and cannot observe out-of-band actions.
  - Canonical conversion (TB-122): the briefing originally had `- Manual: kick a long-running task on stoch, mention @claude-bot status → handler replies in <30s`. The verifier (correctly) couldn't evaluate it; 5/6 bullets passed but the manual one kept failing → `retry_exhausted` and the task got re-frozen despite the implementation being complete. The fix: stub a slow SDK reply in an e2e test, enqueue a Mattermost mention, assert the handler's `mattermost_reply` event lands within 30s of the mention timestamp — pins the same responsiveness claim end-to-end.
  - If a behavior genuinely cannot be auto-verified (rare), put it in `## Out of scope`. Do **not** invent a separate "manual checklist" section — if the daemon can't gate on it, it's out of scope.

### 3. Pass the briefing to `ap2 add`

Two delivery paths — pick whichever is cleaner for the invocation context:

- **`--briefing-file -` (stdin, no temp file):** pipe the briefing buffer into the CLI from the same shell command.
- **`--briefing-file <path>`:** write the briefing to a temp file first (e.g. `mktemp`), then point the flag at it. Useful when the buffer is too big for a here-doc, or when sudo's argv quoting makes stdin awkward.

### 4. Detect who's running this skill

Run `whoami` (or read `$USER`). The result determines whether sudo is needed:

- **Same as `$AP2_SANDBOX_USER` (default `claude-agent`)** → already inside the sandbox; call `ap2` directly. No sudo.
- **anything else** (typically the human user) → escalate via `sudo -u $AP2_SANDBOX_USER`.

### 5. Compose and run the command

Resolve the `ap2` binary with `command -v ap2` (or pin to its absolute path if that's been published in your environment). The sandbox user should have `ap2` on its PATH too — typically the same `uv tool install` your human user did, run inside the sandbox shell.

**Sandbox user, stdin-fed briefing (no sudo):**

```
printf '%s' "$BRIEFING" | ap2 --project <PROJECT_ROOT> add -s Backlog --briefing-file -
```

**Main user, sudo to the sandbox user, file-fed briefing:**

```
TMP=$(mktemp) && printf '%s' "$BRIEFING" > "$TMP" && \
  sudo -u "${AP2_SANDBOX_USER:-claude-agent}" -- "$(command -v ap2)" \
    --project <PROJECT_ROOT> add -s Backlog --briefing-file "$TMP" \
  && rm -f "$TMP"
```

Quote the briefing payload carefully — it can contain backticks, dollar signs, and other shell metacharacters. Prefer single-quoted shell variables (`$'…'` if you need escapes) or write to a temp file rather than inlining the briefing into the command line.

Expected single-line output: `TB-N (queued; will land at next tick)`. If sudo prompts for a password and the call fails, fall back to printing `! sudo …` for the human to execute.

### 6. Report the result

Briefly state:
- The new **TB-N** (parse from the command's stdout).
- That the daemon will pick the queued add up on its next tick (typically <1 minute), then auto-promote Backlog → Ready → Active.
- Suggest `/ap2 <project>` if they want to verify.

Do **not** edit `TASKS.md` or `CLAUDE.md` in the human's local clone — the sandbox is the source of truth for the board now.

## Default section

Backlog. Don't dump tasks straight into Ready — Ready means "briefing prepared", and your briefing is a *seed* (the daemon's prep step may still flesh out `## Design`, etc.). The ideation/reflector flow handles promotion.

If the user explicitly says "ready" or "frozen" in their request, override accordingly via `-s Ready` / `-s Frozen`.

## Examples

```
/ap2-task stoch Write a CONTRIBUTING.md covering setup, tests, code style. #docs
```

→ resolves to `~claude-agent/repos/stoch` (assuming `AP2_SANDBOX_USER` unset), authors a briefing whose H1 is "Write CONTRIBUTING.md covering setup, tests, code style" with `Tags: #docs`, and pipes it through stdin:

```
printf '%s' '# Write CONTRIBUTING.md covering setup, tests, code style

Tags: #docs

## Goal

Document the project'"'"'s setup, test commands, and code-style expectations
so new contributors can ramp without DM'"'"'ing the maintainers.

## Verification

- `uv run pytest -q` — full suite passes
- `test -f CONTRIBUTING.md` — file landed at project root
- prose: CONTRIBUTING.md covers (a) install / setup, (b) running tests,
  (c) code style + lint hooks
' | sudo -u claude-agent -- "$(command -v ap2)" --project ~claude-agent/repos/stoch add -s Backlog --briefing-file -
```

## Structured task metadata: `@<key>:<value>` codespans (TB-132)

Some structured fields live on the rendered task line as backtick codespans, parallel to `#tags`. Single rule: any backtick span starting with `#` is a tag, any starting with `@` is structured metadata. Currently consumed:

- **`@blocked:<csv>`** — comma-separated blocker tokens. Each is either a `TB-N` task id or a `<scheme>:<value>` external blocker (currently `pid:<N>@<TS>`). Auto-promotion skips a task as long as any blocker token is unsatisfied. Pass via `ap2 add --blocked TB-5,TB-7` — the CLI writes the codespan; **do NOT** stuff `(blocked on: ...)` into the briefing or the description. The parser no longer regexes prose for that phrase, so writing it as descriptive text is harmless (and writing it as the only blocker carrier silently does nothing on new tasks).

The format extends naturally to `@priority:high`, `@owner:alice`, `@due_date:2026-05-15`, etc. without expanding the parser regex — `Task.meta` is a free-form dict on the parsed task. Add a CLI surface (or another writer) per-field as the need arises.

A pre-TB-132 transition fallback still parses `(blocked on: ...)` from descriptions for tasks authored before the codespan format landed, so existing tasks aren't broken. New tasks should always go through `--blocked`.

## Rules

- **Sandbox is canonical.** Never write to `TASKS.md` in the human's local clone — even if the daemon has uncommitted changes, the sandbox's state is what matters.
- **One task per invocation.** No batching. Re-run for additional tasks.
- **Briefing required (TB-135).** Every `ap2 add` call needs `--briefing-file`. The minimum useful briefing is H1 + a `## Verification` section with at least one bullet beyond the regression gate. Skipping the briefing is now a hard error, not a free pass to a skeleton.
- **Single line for tags / titles / blocked.** Tag tokens, the H1 itself, and `--blocked` values must be single-line — embedded newlines break the line-oriented parser (TB-134). For richer prose, put it inside the briefing's `## Goal` / `## Scope` sections.
- **Blockers go in `--blocked`, not in the description (TB-132).** Pass `--blocked TB-5,TB-7` to declare structured blockers; the CLI emits a `@blocked:TB-5,TB-7` codespan on the task line. Don't write `(blocked on: ...)` into the briefing prose — TB-132 ended that path because it collided with descriptive text (TB-121 self-blocked on the literal phrase).
- **Run sudo directly when invoking from the main user.** Call it through Bash like any other command. Only fall back to handing the `! sudo …` form to the user if sudo prompts for a password and the call fails.

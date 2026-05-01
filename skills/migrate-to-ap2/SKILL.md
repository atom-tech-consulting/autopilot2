---
name: migrate-to-ap2
description: "Migrate an existing project to ap2: convert legacy TODO.md / tasks.md to canonical TASKS.md (5-section + TB-N IDs), then run `ap2 init` and `ap2 doctor`."
user_invocable: true
---

<command-name>migrate-to-ap2</command-name>

# Migrate to ap2

Use this skill when an existing project has prior task tracking (e.g. `TODO.md`, `tasks.md`, ad-hoc lists in `README`) that needs to land in ap2's canonical layout. For a fresh project with no prior state, just run `ap2 init && ap2 doctor` directly — no skill needed.

This skill is the only step that requires LLM judgment: classifying loose bullet points into Backlog vs. Done, assigning meaningful titles, picking tags. The deterministic scaffolding lives in `ap2 init` and the readiness check in `ap2 doctor`.

## Usage

```
/migrate-to-ap2
```

Run in the root of the project.

## Flow

### 1. Detect legacy task sources

Check, in order:
- `TASKS.md` already exists → if it has the 5-section format, migration is already done; skip to step 4.
- `TODO.md` at project root.
- `tasks.md`, `tasks/` directory, or a "## TODO" / "## Tasks" section in `README.md`.
- Issue lists in any project doc.

If none found, treat the project as a fresh slate; skip to step 3.

### 2. Translate to canonical TASKS.md

Build a `TASKS.md` with this exact 5-section shape (this is the same template `ap2 init` would write, but you'll be filling it):

```markdown
# Tasks

## Active

## Ready

## Backlog

## Complete

## Frozen
```

Rules for translating items:

- **One bullet per task.** Multi-line items collapse to a single line; the briefing is where details live.
- **Section assignment:**
  - Done / completed items → `## Complete`, with `- [x]` checkbox.
  - Items that are concrete enough to start tomorrow → `## Backlog`. Don't put anything in `## Ready` — `Ready` requires a briefing file, which the daemon's prep step writes when it auto-promotes.
  - Pure ideas / "maybe someday" → `## Backlog` with `#proposed` tag (or `## Frozen` if the user wants them parked).
- **TB-N IDs:** assign sequentially starting from `TB-1`. Even completed items get IDs, so the audit trail is consistent. Keep IDs stable — once assigned, never renumber.
- **Task line format** (matches `TASK_LINE_RE` in `ap2/board.py`):
  ```
  - [ ] **TB-1** **One-line title in Title Case** `#tag1` `#tag2` — Single-line description.
  ```
  - **Title** is bold-bold (i.e. `**TB-1**` then `**title**` back-to-back, no annotation between). The parser is strict about this; sha annotations or other inserts strand the task silently.
  - Tags are optional, backtick-wrapped, `#`-prefixed. Use them for area (`#infra`, `#data`), epic, or `#proposed` for ideated-but-not-yet-confirmed work.

If a legacy item is too vague to assign a meaningful title or section, drop it and surface the omission to the user at the end — don't silently mangle it into something else.

### 3. Run `ap2 init`

```bash
ap2 init
```

This is idempotent and won't clobber the `TASKS.md` you just wrote. It fills in everything else:
- `.cc-autopilot/.gitignore` (full template)
- root `.gitignore` (`TASKS.md.lock`)
- `.cc-autopilot/tasks/`
- `.cc-autopilot/progress.md` (if missing)
- `## Autopilot` section in `CLAUDE.md` (appends if absent)

### 4. Run `ap2 doctor`

```bash
ap2 doctor
```

This reports the project skeleton state, sandbox user state, and whether the sandbox clone exists. Read the output and surface any `FAIL` lines to the user with the recommended next step (which `ap2 doctor` already prints).

### 5. Remind the user to commit

After scaffolding succeeds, list the paths that should be tracked:

- `.cc-autopilot/cron.yaml` (created on first daemon start; commit then)
- `.cc-autopilot/tasks/` (briefings — populated by the daemon's prep step on auto-promote)
- `.cc-autopilot/progress.md`
- `TASKS.md`
- `CLAUDE.md` (the new Autopilot section)

If any of the legacy task sources (`TODO.md`, etc.) became redundant, ask the user whether to `git rm` them. Don't delete on your own.

## Adding new tasks after migration: briefing required (TB-135)

Once the migration lands, **future** `ap2 add` calls require a briefing
file — the pre-TB-135 auto-fill skeleton is gone. Three ways to author
the briefing (whichever fits the invocation context):

- `ap2 add --briefing-file <path>` — point at a markdown file you wrote.
- `ap2 add --briefing-file -` — read the briefing from stdin.
- `ap2 add` (no args, with `$EDITOR` set) — git-commit-style: opens
  `$EDITOR` against the canonical template and uses the saved buffer.

The canonical template is at `ap2/init.py:BRIEFING_TEMPLATE` — H1 (title),
optional `Tags:` line, `## Goal`, `## Scope`, `## Design`, `## Verification`,
`## Out of scope`. The daemon's per-task verifier (TB-69) reads
`## Verification` to score the task's commit, so include at least one
concrete shell or prose bullet beyond the project-wide regression gate —
without it, the verifier has nothing scope-specific to score against.

**Auto-verifiable bullets only (TB-138).** Every `## Verification` bullet
must be auto-verifiable — one of: (1) a backticked shell command the verifier
can run, (2) a unit/e2e test name the regression gate covers, or (3) a prose
claim that names a concrete file/symbol an SDK judge can confirm against the
diff. **No `Manual:` bullets** — the per-task verifier runs unattended and
cannot observe a live operator action. If a behavior genuinely cannot be
auto-verified, it belongs in `## Out of scope`, not in the gating section.

Already-migrated tasks on disk are unaffected: the briefing requirement
gates only **future** `add_*` calls.

## Rules

- **Non-destructive on TASKS.md.** If the file already exists with content, never overwrite — show the user the old content vs. the proposed migration and let them decide.
- **Idempotent.** Re-running on an already-migrated project should detect that and skip steps 1-2.
- **Project-local.** Only modify files in the current project directory.
- **No sudo.** Sandbox-user setup is out of scope for this skill — `ap2 doctor` will tell the user what to run.
- **Briefing-required for future adds (TB-135).** The migration converts existing items in place; new tasks added after migration must be briefing-backed (`--briefing-file <path|->`, or `ap2 add` with `$EDITOR` set). The `/ap2-task` skill handles the briefing-authoring end-to-end.

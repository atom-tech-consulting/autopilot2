# Refresh /ap2 + /ap2-task skills; add deploy script to ~/.claude

## Goal

Two things:

(1) Audit and update `skills/ap2/SKILL.md` and `skills/ap2-task/SKILL.md` against the current code. Many fixes have landed (TB-131 operator queue, TB-132 codespan blockers, TB-134 single-line constraint, TB-135 require --briefing-file, TB-138 auto-verifiable bullets when it ships) that the slash-command skills should reflect — operator and orchestrator behavior is partly steered by what these docs claim.

(2) Add a `scripts/deploy-skills.sh` (or `make deploy-skills`) that idempotently syncs `<repo>/skills/*` into `$HOME/.claude/skills/`. Today the operator/orchestrator runs `/ap2` and `/ap2-task` from `~/.claude/skills/` — those are deployed copies, not symlinks. Without an explicit sync step, repo edits drift away from what the live slash commands see.

## Why

Skills in this repo:
- `skills/ap2/SKILL.md` — operator-facing "/ap2 status / recent" skill.
- `skills/ap2-task/SKILL.md` — operator-facing "/ap2-task add" skill.
- `skills/migrate-to-ap2/SKILL.md` — migration helper.

Live deployed copies (used by Claude Code at runtime):
- `$HOME/.claude/skills/ap2/`
- `$HOME/.claude/skills/ap2-task/`

Drift problem: every time the briefing-author rules change (e.g. "must use --briefing-file" from TB-135, "operator queue is now the path" from TB-131, "no Manual: bullets" from TB-138), the deployed skills stay frozen until someone manually copies. We've already burned operator confusion on this — TB-135 SKILL update only landed in the repo; the deployed copy on the operator's machine still suggested `-d "..."` for hours after.

## Scope

- (1) Audit pass on `skills/ap2/SKILL.md` and `skills/ap2-task/SKILL.md` against current behavior. Specific items to verify:
  - `ap2-task`: must mention `--briefing-file` is required (TB-135), single-line constraint on `-t` / `-d` (TB-134), `--blocked` flag for codespan blockers (TB-132), the operator queue's "queued; will land at next tick" output (TB-131), and the auto-verifiable Verification rule (once TB-138 ships).
  - `ap2`: must mention the new `pending: N operator ops` line in `ap2 status`, the `web:` URL line, and reflect any current command surface changes (`ap2 unfreeze`, `ap2 backlog`, `ap2 delete` are now queue-routed).
- (2) Wherever the skills include sample command invocations, update them to use the new flag names and output formats.
- (3) New `scripts/deploy-skills.sh` (executable, gitignored output unchanged): rsync-style copy from `<repo>/skills/*` to `$HOME/.claude/skills/`, with `--delete` on a per-skill subdir basis (so renamed files don't linger). Default to dry-run when run without args, apply with `--apply`. Print a one-line diff summary per skill.
- (4) Optional: a one-shot `ap2 sandbox sync-skills` CLI that wraps the script, so operators don't need to remember the path.

## Verification

- `uv run pytest -q ap2/tests/` — full regression gate passes (gating)
- `bash scripts/deploy-skills.sh` (dry-run, no args) exits 0 and prints a per-skill diff summary; nothing under `$HOME/.claude/skills/` is mutated.
- `bash scripts/deploy-skills.sh --apply` exits 0 and after running, `diff -r skills/ap2 $HOME/.claude/skills/ap2` reports no differences (and same for ap2-task, migrate-to-ap2).
- `grep -qE "(--briefing-file|queued; will land)" skills/ap2-task/SKILL.md` — the operator-facing skill mentions the post-TB-131/TB-135 surface.
- `grep -qE "(pending: |operator ops|web:)" skills/ap2/SKILL.md` — the status-skill mentions the post-TB-130/TB-131 status output.
- New shell test (or pytest with `subprocess.run`): invoking the deploy script with `--apply` against a temp `$HOME` produces an exact mirror of `<repo>/skills/`.
- The diff updates either `pyproject.toml` `[project.scripts]` or `ap2 sandbox` to expose the sync as a CLI subcommand (if option 4 is taken). Tests pin the entry-point.

## Out of scope

- Symlink-based deployment instead of copy (could replace the script later; copy is simpler and matches Claude Code's expectation that skill files are real on disk).
- A pre-commit hook that fails the commit when `skills/` is edited but the deploy script wasn't run — that's worth filing separately if drift continues to be a problem.
- Auto-deploy on `ap2 start` — too magical; operators should know when their slash commands change.
- Migrating taskboard skill (`~/.claude/skills/taskboard/`) — that's a global skill not part of this repo.

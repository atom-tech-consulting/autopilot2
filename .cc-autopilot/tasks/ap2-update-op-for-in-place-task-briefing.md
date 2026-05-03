# TB-153 — `ap2 update` op for in-place task / briefing edits

## Goal

Add an `update` op to the operator queue, plus an `ap2 update TB-N` CLI surface, for in-place edits to an existing task's `title`, `tags`, `description`, `@blocked:<csv>` meta codespan, and/or briefing file. Routed through the same `operator_queue_append` → drain path as `add_*` / `delete` / `unfreeze` / `approve` (TB-131 / TB-142) so it never lands inside a task agent's snapshot window.

Today the only way to "edit" a queued task is delete + re-add, which:
- Allocates a new TB-N (orphans every prior reference in `operator_log.md`, `events.jsonl`, `progress.md`, status reports, MM threads, the pending-review reminder cron).
- Re-slugs the briefing file (orphans git history of `.cc-autopilot/tasks/<slug>.md`).
- Forces the operator to re-supply title, full description, tags, briefing — even for a one-tag fix.
- Reads as `task_deleted` + `task_added` in the audit trail rather than a diff.

`update` keeps TB-N stable, briefing slug stable, section/position stable, and emits a `task_updated` event with a `fields=[...]` diff.

## Two surfaces, one queued op

1. **`ap2 update TB-N` CLI** — new `cmd_update` in `ap2/cli.py`. Reuses `cmd_add`'s briefing-resolution flow (`--briefing-file <path|->` or `$EDITOR` fallback). Flags:
   - `--title <str>`
   - `--tags <csv>`
   - `--blocked <csv>` (sets the `@blocked:<csv>` meta codespan)
   - `--description <str>`
   - `--clear-tags` (explicit clear, distinct from omitted = unchanged)
   - `--clear-blocked` (explicit clear)
   - `--no-verify` / `--verify` (briefing verification, mirrors `cmd_add`)
   - `--force` (override the per-target Active / Pipeline-Pending fence — board-line fields only; see below)
   - Omitted flag = field unchanged.
   - Calls `do_operator_queue_append({"op":"update", "task_id":"TB-N", ...})`.

2. **`operator_queue_append` MCP op** — extend `_OP_VALUES` in `ap2/tools.py` with `"update"`, so @claude-bot can route operator MM requests like "add `@blocked:review` to TB-150" or "rewrite TB-150's briefing" through the same drain.

## Queue-append side (`do_operator_queue_append`, runs synchronously under lock)

- Validate target TB-N exists on the board; else emit `operator_queue_error` (same shape as `delete` / `unfreeze`).
- **Per-target fence** (mirrors `delete`'s fence — `ap2/tools.py` `_apply_operator_op` delete branch):
  - Refuse if the **target's** section is `Active` or `Pipeline Pending` without `--force`. Other tasks running is fine; the fence is per-task, not directory-wide.
  - `--force` is allowed for board-line fields only (`title` / `tags` / `@blocked` / `description`) — those don't touch the agent's dispatched briefing.
  - **Briefing-content edits to a running task are hard-refused** with no `--force` escape, since the agent may re-read its briefing mid-run via `Read` and TB-110's snapshot may hash the file. (Deferred-draft handling is carved out as a follow-up; see Out of scope.)
- Briefing path resolution: if `briefing` arg present, write to the **existing** `task.briefing` path (slug-stable — preserves git history of the briefing file). For tasks with no briefing yet (legacy / pre-TB-135), allocate a slug from the **current** title.
- Append queue record; emit `operator_queue_append` event.

## Drain side (`_apply_operator_op`, runs between tick stages)

- New `op == "update"` branch.
- Add `Board.update(task_id, **fields)` helper in `ap2/board.py`: mutates the matching `Task` dataclass in place (only fields present in `fields` get changed) and re-renders that section. `meta["blocked"]` round-trip reuses `_normalize_blocked_meta` — same path `add_*` runs.
- Emit `task_updated` event with `fields=[...]` diff (e.g. `fields=["tags","blocked"]` or `fields=["briefing"]`).
- Append one-line audit via `_append_operator_audit_line`.

## Design decisions (locked in this thread)

- **Q1 / Active fence:** per-target, mirrors `delete`. `--force` for board-line fields; hard-refuse for briefing-content edits to a running task.
- **Q2 / briefing-only updates:** allowed. Emit `task_updated` with `fields=["briefing"]`. Useful audit signal even when the task line didn't change.
- **Q3 / clearing fields:** explicit `--clear-tags` / `--clear-blocked` flags. Empty-string `--tags ""` is ambiguous (typo vs intentional) — explicit flag wins. Omitted ≠ cleared.
- **Q4 / title change → briefing slug:** keep existing slug. Renaming would orphan briefing-file git history at the seam; not worth it for cosmetic title changes.

## Out of scope (follow-up if the fence bites)

Deferred-draft briefing edits for running tasks: write `<slug>.md.draft` at queue-append time, swap to live on a `task_complete` / `task_failed` hook for that TB-N. ~50 LOC + new event type + completion-hook wiring + a `pending_briefing_drafts` index. Fence is ~5 LOC and covers the 90% case (edits target Backlog / Ready / Frozen). Carve a TB-15X for the deferral path only if the fence actually bites in practice.

## Acceptance

- Unit tests, one per op-field path: `title`, `tags`, `blocked`, `description`, `briefing`, `--clear-tags`, `--clear-blocked`.
- Fence tests:
  - Refuse `update` on Active task without `--force`.
  - With `--force` on Active task: board-line fields succeed, briefing-content edit still refused.
  - Update on Backlog / Ready (non-dispatched) / Frozen task succeeds without `--force`.
- End-to-end:
  - `ap2 update TB-X --tags foo,bar` then `ap2 status` shows the change.
  - `ap2 update TB-X --briefing-file new.md` then `git log` on the briefing path shows continuous history (no rename seam).
  - `task_updated` event emitted with correct `fields=[...]` diff and audit line in `operator_log.md`.
- `operator_queue_append` MCP op accepts `"update"` and round-trips through the drain identically to the CLI path.

## Files to touch

- `ap2/cli.py` — new `cmd_update` (mirror `cmd_add` argparse + briefing resolution).
- `ap2/tools.py` — extend `_OP_VALUES`, add queue-append validation + fence + briefing-write path, add drain-side `update` branch in `_apply_operator_op`, audit-line + event emission.
- `ap2/board.py` — `Board.update(task_id, **fields)` helper.
- Tests: extend `tests/test_operator_queue.py` (or equivalent) with the cases above; CLI test for `cmd_update` argparse.
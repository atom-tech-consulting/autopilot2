# Commit insights/_index.md after ideation-driven regeneration

Tags: #ideation #insights #commit-cohesion

## Goal

`ap2/ideation.py::_run_ideation` regenerates `.cc-autopilot/insights/_index.md` *before* taking the pre-snapshot used by the post-run state-commit diff. The regenerated index is therefore part of the snapshot baseline, not the diff, and never lands in the `state: ideation` commit. The file sits dirty in the worktree until the next regeneration, which is a no-op when the rendered content is unchanged — so the dirt persists indefinitely.

This violates the invariant that daemon-owned state files in `_STATE_FILE_NAMES ∪ _STATE_DIRS` get committed coherently with the agent run that produced them — exactly the property TB-111/TB-112 introduced for linear rollback. If an operator runs `ap2 rollback` past the point where the dirty index was generated, the rollback target will be lying about what `insights/_index.md` looked like at that snapshot.

Why now: live evidence in the `post-train` sibling repo. `_index.md` is dirty there with 8 bullets matching already-committed insight files. The smoking-gun chronology: `ideation_empty_board` event at `2026-05-06T21:59:22Z` (which fires immediately before `maybe_regenerate_index`), `_index.md` mtime `21:59:22Z` (exact match), `state: ideation` commit `55707f6` at `22:07:20Z` lists `ideation_state.md`, the new TB-43 briefing, `CLAUDE.md`, `TASKS.md` — but not `_index.md`. autopilot2 itself does not exhibit the bug today only because nothing has been adding insight files; it would surface here the moment ideation starts producing them. Goal anchor: the `## Done when` bullet "Failure recovery (verification fails, retries exhaust, daemon restart, cron drift, agent timeouts) is fully automatic" — rollback cohesion depends on every state file being committed coherently.

## Scope

- `ap2/ideation.py` — the `_run_ideation` function (lines ~426–552 today). Move `insights.maybe_regenerate_index(cfg)` (currently lines 467–472) to run *after* `pre_snapshot = _daemon._snapshot_state_paths(cfg)` (line 517), so the regeneration is visible to the post-run diff.

## Design

The fix is a reordering, not a structural change. The regeneration must still run before the ideation agent starts (Step 0.5 of `ap2/ideation.default.md` reads the index for grounding), but it must run *after* the pre-snapshot so the diff captures it. New order:

1. `ideation_empty_board` event (unchanged)
2. `pre_snapshot = _daemon._snapshot_state_paths(cfg)`  ← moved up
3. `insights.maybe_regenerate_index(cfg)` (with the existing try/except)  ← moved down
4. `_daemon._run_control_agent(...)` — ideation agent runs
5. `mark_run(...)`
6. `touched = _changed_state_paths(pre_snapshot, _snapshot_state_paths(cfg))` — now includes `_index.md` when regenerated
7. `_commit_state_files(cfg, "state: ideation", paths=touched)` — index rides along

The TB-126 comment at line 512–516 ("snapshot the state surface before ideation runs so the post-run state commit only stages paths ideation actually touched") still applies — the index regeneration is conceptually part of "what this ideation cycle did", so it belongs in the same commit.

The TB-89 comment at line 464–466 ("Step 0.5 of `ap2/ideation.default.md` reads `_index.md` for grounding") still holds — the regeneration happens before the agent starts, just sequenced differently relative to the snapshot.

No change to `insights.py`, no change to `_snapshot_state_paths` / `_changed_state_paths` / `_commit_state_files`, no change to `_STATE_DIRS`. The `.cc-autopilot/insights/` dir is already in `_STATE_DIRS` (`ap2/daemon.py:1685`); the snapshot already hashes it.

## Verification

- `uv run pytest -q` — full suite passes
- `uv run pytest -q ap2/tests/test_ideation_defaults.py` — ideation tests still pass
- prose: in `ap2/ideation.py::_run_ideation`, the `insights.maybe_regenerate_index(cfg)` call is now sequenced *after* the `pre_snapshot = _daemon._snapshot_state_paths(cfg)` line and before `_daemon._run_control_agent(...)`. The TB-89 comment block is preserved (or updated) and the try/except wrapping the regeneration call is preserved.
- prose: a regression test exists (new or extended in `ap2/tests/`) that exercises `_run_ideation` against a working tree where ideation regenerates `_index.md`, and asserts `_index.md` appears in the `touched` paths passed to `_commit_state_files` (or equivalent — the test may stub the SDK call and inspect the diff list directly). The test fails on the pre-fix ordering and passes on the post-fix ordering.

## Out of scope

- Changing `_STATE_DIRS` or the snapshot/diff machinery itself.
- Backfilling already-dirty `_index.md` files in consumer repos like `post-train` — operators can `git add` + commit those manually, or they will be picked up the next time the index actually changes.
- Auditing other call sites of `_snapshot_state_paths` for the same ordering bug — only `_run_ideation` interleaves a deterministic state-file write with the snapshot today; cron uses agent-driven writes only.
- Changing the task-completion commit path (`_task_state_paths`) to include `insights/` — task agents that write insight files commit them with their own source-code commit, and that path is not affected by this bug.

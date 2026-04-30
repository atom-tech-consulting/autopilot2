# TB-127 — Verifier mis-reads retry diff; fails already-committed task work

## Goal

On retry of a task whose original run already committed an implementation (e.g. TB-122 at 5ebfae8, TB-123 at a2e3d6a), the per-task verifier (verify.py _judge_prose_bullet) sees only the retry's bookkeeping diff (retry_state.json + TASKS.md section move) and hallucinates 'no new tests added' / 'diff contains no changes to file X' — even though the implementation is in HEAD from the first run. Result: every retry fails verification, retries exhaust, task gets Frozen. Hit on TB-122 and TB-123 on 2026-04-30 (3 retries × 2 tasks before freezing). Fix options: (a) the verifier should diff against the original successful commit (or pre_run_head before the *first* attempt), not the latest retry's bookkeeping diff. (b) detect 'task already complete in HEAD by commit-subject convention TB-N: ...' and short-circuit verification with a pass + auto-move-to-Complete.

## Scope

- (file / module to change)

## Design

(filled in by /tb prep or by the ideation agent)

## Verification

Concrete acceptance criteria the daemon's per-task verifier (TB-69)
runs after the agent's commit. Shell-command bullets (backtick-fenced
at the start of the bullet) are run automatically; prose bullets are
judged by an SDK call against the diff.

- `uv run pytest -q` — full suite passes
- (additional shell or prose bullets)

## Out of scope

- (filled in)

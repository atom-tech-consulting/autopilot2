# TB-126 — Narrow state-commit git-add to operation-touched paths

## Goal

_commit_state_files (daemon.py:1526-1573) blindly 'git add --' every path in _STATE_FILE_NAMES + _STATE_DIRS, then commits whatever is staged. That bundles unrelated changes into each state commit. Real example: 4fc7b3e ('state: TB-122 → Backlog') also picked up TB-125's briefing (auto-set-git-identity-in-sandbox-project.md), even though TB-125 had nothing to do with TB-122's rollback. The current design assumes everything in .cc-autopilot/tasks and .cc-autopilot/insights is fair game for any state commit, which trades semantic precision for code simplicity. Fix: thread a 'paths' allowlist through _commit_state_files callers (move_to_*, _handle_failure, cron tick, etc.) so each commit only stages files the current operation touched. Briefings the operation didn't write should not ride along. Bonus: makes 'git log -- <file>' meaningfully blame the right commit when reverting/bisecting.

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

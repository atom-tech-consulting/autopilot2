# TB-125 — Auto-set git identity in sandbox project-setup; doctor check for it

## Goal

First daemon tick that commits state fails with 'Author identity unknown' because fresh sandbox-user clones inherit no git user.name/user.email (global unset for claude-agent; repo-local unset on clone). Fix has two parts: (1) ap2 sandbox project-setup should set repo-local git config user.name='ap2 daemon' and user.email='ap2-daemon@localhost' (or operator-overridable values) right after cloning, so the daemon's first commit succeeds. (2) ap2 doctor's project_audit should check for git user.name/user.email and FAIL with a one-line fix command if either is missing — same pattern as the other readiness checks. Repro: clone a fresh sandbox project, start daemon, watch first cron status-report tick emit state_commit_error with the 'tell me who you are' fatal.

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

# Progress

## [2026-04-30] TB-124: Doctor probes user env via bash -i; misses zsh-only .zshenv exports
- **Commit:** `9ab75ae`
- **Summary:** Replaced hard-coded `bash` in doctor's env probes (sandbox.user_audit + doctor._ap2_installed_for_user) with the user's pw_shell via a new `_user_login_shell()` helper, so `~/.zshenv` exports (CLAUDE_CODE_OAUTH_TOKEN, PATH from `uv tool install`) are visible to the probe. Full suite passes (472 tests).
- **Files:** ap2/sandbox.py, ap2/doctor.py, ap2/tests/test_sandbox.py, ap2/tests/test_doctor.py
- **Tests:** pass

## [2026-04-30] TB-125: Auto-set git identity in sandbox project-setup; doctor check for it
- **Commit:** `d563f41`
- **Summary:** project-setup now sets repo-local git user.name='ap2 daemon' / user.email='ap2-daemon@localhost' (overridable via --git-name/--git-email) right after the clone so the daemon's first state commit doesn't fatal 'Author identity unknown'; project_audit FAILs with a one-line fix command when either field is unset. 478 tests pass.
- **Files:** ap2/sandbox.py, ap2/cli.py, ap2/tests/test_sandbox.py
- **Tests:** pass

## [2026-04-30] TB-126: Narrow state-commit git-add to operation-touched paths
- **Commit:** `a6fc894`
- **Summary:** Threaded a `paths` allowlist through every _commit_state_files caller (run_task, pipeline-pending sweep, _recover_orphans, run_cron, _maybe_ideate) so each `state:` commit stages only files the current operation touched; added `_filter_state_paths` (defense-in-depth), `_task_state_paths` (shared run_task path-set), and `_snapshot_state_paths` + `_changed_state_paths` (snapshot/diff for control-agent callers). 481 tests pass.
- **Files:** ap2/daemon.py, ap2/ideation.py, ap2/tests/e2e/test_daemon_commit.py
- **Tests:** pass

## [2026-04-30] TB-127: Verifier mis-reads retry diff; fails already-committed task work
- **Commit:** `09831b1`
- **Summary:** Per-task verifier now resolves task_id → commit-subject `<task_id>:` → that commit's diff (with HEAD fallback) so retries of already-committed tasks judge against the real implementation diff instead of the daemon's bookkeeping diff. New `_find_task_commit` + `_git_show_for_task` helpers in verify.py; daemon._maybe_per_task_verify plumbs task.id through. 9 new unit tests + 1 e2e regression pin; 491/491 tests pass.
- **Files:** ap2/verify.py, ap2/daemon.py, ap2/tests/test_verify_retry_diff.py, ap2/tests/e2e/test_verify_per_task.py
- **Tests:** pass

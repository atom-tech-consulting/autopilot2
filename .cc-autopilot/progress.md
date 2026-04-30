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

# Progress

## [2026-04-30] TB-124: Doctor probes user env via bash -i; misses zsh-only .zshenv exports
- **Commit:** `9ab75ae`
- **Summary:** Replaced hard-coded `bash` in doctor's env probes (sandbox.user_audit + doctor._ap2_installed_for_user) with the user's pw_shell via a new `_user_login_shell()` helper, so `~/.zshenv` exports (CLAUDE_CODE_OAUTH_TOKEN, PATH from `uv tool install`) are visible to the probe. Full suite passes (472 tests).
- **Files:** ap2/sandbox.py, ap2/doctor.py, ap2/tests/test_sandbox.py, ap2/tests/test_doctor.py
- **Tests:** pass

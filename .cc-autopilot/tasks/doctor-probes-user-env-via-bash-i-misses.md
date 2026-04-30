# TB-124 — Doctor probes user env via bash -i; misses zsh-only .zshenv exports

## Goal

Doctor's user_audit (sandbox.py:155-173) and _ap2_installed_for_user (doctor.py:50) both shell out via 'sudo -u <user> -i bash -c ...'. bash login shells don't source ~/.zshenv, so credentials installed by 'ap2 sandbox install-token' (which writes to ~/.zshenv per sandbox.py:471-487) are invisible to the probe — producing false 'CLAUDE_CODE_OAUTH_TOKEN unset' WARN and false 'ap2 not on $PATH' FAIL even when the user's actual login shell (zsh) sees them fine. Fix: probe via the user's pw_shell (or sh -c 'echo $SHELL' inside a non-interactive sudo -u, then re-exec with -i), or source both rc paths explicitly. Either way, doctor's verdict should reflect what the daemon will see when started from the user's normal shell, not what bash sees.

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

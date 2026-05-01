# Verifier: run shell bullets via /bin/bash, not /bin/sh

## Goal

Change `verify._run_shell_bullet` to invoke shell bullets via `/bin/bash` instead of the platform default `/bin/sh`, so common bash-only constructs (process substitution `<(...)`, double-bracket conditionals `[[ ... ]]`, ANSI-C quoting `$'...'`, arrays, `set -o pipefail`) parse cleanly and don't fail at the shell-parser stage.

## Why

`subprocess.run(cmd, shell=True)` on POSIX uses `/bin/sh`. On macOS that's bash-in-POSIX-mode, which disables process substitution; on Debian-family Linux it's dash, which doesn't have process substitution at all. Bullets authored by humans or LLMs invariably default to bash mental models — `<(python3 -c ...)` is the canonical "compare against a script's output" idiom, and it shows up unprompted.

We just shipped fixes for five briefings (TB-142/143/144/145/146) where bash-only bullets caused state_violation-style retry exhaustion against tasks whose implementations were correct. Each was a self-inflicted briefing-author mistake, but the underlying surface keeps producing them: the verifier is the only consumer that runs sh, and every other developer-facing tool (Bash tool in Claude Code, manual operator shells, CI scripts) runs bash. Aligning the verifier with bash eliminates the surprise.

Verified working in a side-by-side test on this repo: the same `<(...)` bullet that produces `/bin/sh: -c: line 0: syntax error near unexpected token '('` under sh runs to a clean exit-0 under bash.

## Scope

(1) `ap2/verify.py:_run_shell_bullet` (around line 220): pass `executable="/bin/bash"` to `subprocess.run`. One-line change. The rest of the call (cwd, capture_output, text=True, timeout) stays as-is.

(2) Add a comment block at the call site explaining why we override the platform default — future maintainers shouldn't revert to "more portable sh" without seeing the rationale.

(3) Sanity-check there's no other `subprocess.run(..., shell=True)` in the verification path that should also pick up the override. The shared shell-bullet helper is the only place; double-check by grep.

## Verification

- `uv run pytest -q ap2/tests/` — full regression gate passes (gating)
- `grep -qE 'executable\s*=\s*"/bin/bash"' ap2/verify.py` — bash override present at the call site.
- New unit test in `test_verify_per_task.py` (or `test_verify_retry_diff.py`): a shell bullet using `<(echo a)` (process substitution) returns `pass` from `_run_shell_bullet` (would error under sh; pin via the test that bash is now used).
- New unit test: a shell bullet using `[[ -f ap2/verify.py ]]` returns `pass`. POSIX sh would parse `[[` as a command name and exit 127; bash treats it as a conditional.
- New unit test: a deliberately broken bullet (e.g., `python3 -c 'raise SystemExit(1)'`) still returns `fail` with non-zero exit — bash isn't covering for an actual command-error path.
- The diff includes the comment block at the call site explaining the override.

## Out of scope

- Adding a fallback to sh when bash is unavailable. macOS and every common Linux ship `/bin/bash` by default; CI environments that don't would already be broken in many other ways (most operator scripts assume bash). Document the dependency, don't soften.
- Changing `subprocess.run(shell=True)` callers elsewhere in the codebase (cli, daemon, tools — these have their own use cases and aren't in the verification path).
- Linting briefings for bash-only constructs ahead of time. Once bash is the runtime shell, the lint isn't needed; bullet authors can use bash freely.

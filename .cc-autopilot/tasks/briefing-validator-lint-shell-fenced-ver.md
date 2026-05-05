# Briefing validator: lint shell-fenced `## Verification` bullets for known pitfalls

## Goal

Current focus: ideation quality. The "Shell-bullet pitfalls to AVOID" subsection of `ap2/ideation.default.md` (TB-76) documents three pitfalls — bare `python` instead of `uv run python` / `python3`, bare path as command (e.g. `\`reports/foo/README.md\`` makes the shell try to execute the markdown file), and `grep <pattern> <directory>` without `-r` (exits 2). The rule lives only in the ideation prompt; no validator catches the pitfalls before TB-N is allocated. Result: TB-156 (60c60ff), TB-165 (26ac188), and TB-166 (efe1996) each had to ship an explicit follow-up commit fixing the same `grep -nE` → `grep -rnE` typo. That's 3 same-shape verification-fail events in 4 days. This task closes the gap by mirroring the TB-76 pitfall list into `_validate_briefing_structure` so the same typo can't slip past queue-append.

Why now: closes a documented, repeatedly-observed verification-fail pattern (TB-156 / TB-165 / TB-166 — 3 tasks, identical pitfall, each cost a retry round + follow-up commit). The pitfall list is already prose-documented; mechanical enforcement at queue-append is the natural next step. Without it, every future briefing remains one typo away from re-running the same retry loop.

## Scope

- `ap2/tools.py` — extend `_validate_briefing_structure` with a shell-bullet pitfall scan: walk `## Verification` bullets, match each backtick-fenced command at the start of a bullet, run a small set of regex checks against the command body. On any match, return a structured error message naming the offending bullet + the pitfall + the canonical fix.
- New helper `_lint_shell_bullet(cmd: str) -> str | None` in `ap2/tools.py` (or a sibling module) returning `None` on pass / a one-sentence pitfall description on fail. Pitfall set:
  - `^python(\s|$)` — suggest `uv run python` or `python3`.
  - `^grep\s+(?:-[A-Za-z]+\s+)*[^|]+\s+\S+\.cc-autopilot/?\s*$|^grep\s+(?:-[A-Za-z]+\s+)*[^|]+\s+\S*ap2/?\s*$` — broader: any `grep` whose final positional arg ends with `/` AND whose flag set lacks `r`/`R`. Use `shlex.split` to tokenize so the check is robust to quoting.
  - `^[\w./-]+\.(md|json|yaml|toml|txt)\s*$` — bare path-with-known-suffix as command. Suggest `test -f <path>` or wrap in `bash -c`.
- `ap2/tests/test_tools.py` — new tests covering each pitfall (positive + negative case for each), plus `do_operator_queue_append` add-op rejection on a briefing containing a pitfall bullet.
- `ap2/ideation.default.md` — append a one-liner under the existing "Shell-bullet pitfalls to AVOID" section noting "the queue-append validator now rejects these (TB-172)".

## Design

Extension fires after the TB-164 Why-now check, before final `return None`. Reuse `parse_verification_section` to get the bullets, then for each bullet extract the leading backticked command:

1. Strip leading list marker (`-`, `*`, `+`).
2. Match `\`([^\`]+)\`` at the start of the bullet body. If absent, skip — prose / test-name bullets aren't shell-fenced and aren't subject to this lint.
3. Pass the captured command string to `_lint_shell_bullet`.
4. On non-None return, build error message: "shell-fenced verification bullet `\`<cmd>\`` matches a known pitfall (TB-76 / TB-172): <pitfall description>. Fix: <suggestion>."

`_lint_shell_bullet` uses `shlex.split` for tokenization where possible; falls back to regex for edge cases. Skip cases that look like multi-statement compound commands (anything containing `&&`, `||`, `;`, `|`) — those are intentionally complex and out-of-scope for this lint pass.

Edge case: `\`grep -q "AP2_FOO"\`` (no positional path arg) is a valid stdin-grep usage and should pass. The lint should only flag `grep ... <path>` where `<path>` is a directory.

Failure mode: false positives on legitimate bullets. Tune the regex set so the false-positive rate stays effectively zero on the existing `.cc-autopilot/tasks/*.md` corpus — verify by running the lint over the existing briefings as a one-off in a test (`test_lint_shell_bullet_zero_false_positives_on_existing_corpus`).

## Verification

- `uv run pytest -q ap2/tests/` — full suite passes
- `uv run pytest -q ap2/tests/test_tools.py -k shell_bullet` — new pitfall tests pass
- `grep -rnE "_lint_shell_bullet|TB-172" ap2/tools.py` — new helper + cross-reference present
- New unit test `test_lint_shell_bullet_rejects_bare_python` drives `_lint_shell_bullet("python -c 'print(1)'")` and asserts a non-None pitfall description mentioning "uv run" or "python3"
- New unit test `test_lint_shell_bullet_rejects_grep_no_r_on_directory` drives `_lint_shell_bullet('grep -nE "foo" ap2/')` and asserts a non-None pitfall description mentioning "-r" or "directory"
- New unit test `test_lint_shell_bullet_accepts_grep_with_r_on_directory` drives `_lint_shell_bullet('grep -rnE "foo" ap2/')` and asserts None
- New unit test `test_lint_shell_bullet_accepts_uv_run_python` drives `_lint_shell_bullet('uv run python -c "print(1)"')` and asserts None
- New unit test `test_validate_briefing_structure_rejects_pitfall_bullet` drives `_validate_briefing_structure` with a briefing whose `## Verification` carries `- \`python -c 'print(1)'\` — runs` and asserts a non-None error string mentioning "shell-fenced" or "pitfall"
- New unit test exercises `do_operator_queue_append` with a briefing containing a pitfall bullet and asserts the call returns `_err(...)`
- New unit test `test_lint_shell_bullet_zero_false_positives_on_existing_corpus` walks every `.cc-autopilot/tasks/*.md`, extracts shell-fenced verification bullets, and asserts every existing bullet passes the lint (i.e. no flagged false positives on already-shipped briefings)
- `grep -rnE "TB-172" ap2/ideation.default.md` — confirms the prompt cross-reference landed

## Out of scope

- Catching every conceivable shell pitfall — only the three TB-76-documented ones (bare `python`, grep-no-r-on-dir, bare-path-as-command). Adding more risks false positives.
- Linting compound commands (`&&` / `||` / `;` / `|` chains) — too easy to misclassify; deferred until a real failure surfaces.
- Migrating existing briefings on disk — verified zero false positives on the current corpus is enough; no rewrites needed.
- Replacing the runtime verifier's bullet execution — the lint is pre-allocation-only; the verifier still runs bullets at task-completion time.

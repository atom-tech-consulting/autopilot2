# Add verification-bullet shell-syntax conventions to ideation.default.md (recursive grep needs -I, !-prefix, -r, Prose:, grep -c) so ideation stops emitting broken bullets

Tags: #autopilot #ideation #prompts #briefing #verification #robustness

## Goal

Teach the daemon's ideation agent the load-bearing verification-bullet shell-syntax
rules by adding a concise conventions block to `ap2/ideation.default.md`, so it stops
proposing briefings with shell bullets that false-fail the verifier. Operator-filed
meta-infra robustness fix; no goal.md focus anchor (filed `--skip-goal-alignment`).

Why now: these bullet-syntax pitfalls are documented for the operator (in the
operator skills) but the daemon's ideation agent reads only `ap2/ideation.default.md`
(`setting_sources=["project"]`, no skills), so it keeps re-deriving the same broken
shapes — most recently a recursive `grep -rn` scrub bullet that matched binary
`__pycache__/*.pyc` files (whose `co_filename` embeds the absolute build path),
false-failing and freezing the task across multiple retries. The fix belongs in the
daemon's own prompt, where the ideation agent will actually see it.

## Scope

- Add a tight "Verification-bullet shell-syntax rules" block to
  `ap2/ideation.default.md` (in the briefing-authoring guidance, near the existing
  bullet/Verification material). Keep it a concise checklist (prompt budget matters),
  covering the recurring failure shapes:
  - **Recursive grep over source dirs MUST use `-I`** (skip binary files) — plain
    `grep -rn PAT dir/` matches binary `__pycache__/*.pyc` whose `co_filename`
    embeds absolute paths, exits 0, and false-fails a `! grep` bullet regardless of
    source. Use `grep -rnI` (or `--include='*.py'` / `--exclude-dir=__pycache__`).
  - **`!`-prefix for "should NOT match"** absence checks (bare `grep` exits 1 on zero
    matches → verifier reads non-zero as fail).
  - **`-r` for directory grep** (grep refuses a dir arg without it).
  - **No literal backticks inside a shell bullet** (the verifier's markdown-fence
    extraction truncates at the first backtick) — use `.` as a stand-in.
  - **`Prose:` prefix for judge-evaluated bullets** (a bullet whose first content is
    a backtick-fenced path gets mis-classified as shell and exec'd).
  - **Multi-file counts: `grep -hE PAT files | wc -l`**, not `grep -cE` (which emits
    per-file `file:N` lines).
- Do NOT bloat the prompt with the full rationale — a scannable rule list with a
  one-line why each.

## Design

- `ap2/ideation.default.md` is the ideation agent's canonical prompt (the daemon
  inlines it); this is the only surface that reaches the daemon's ideation agent, so
  the rules must live here (not only in the operator skills / memory).
- Mirror the operator-side pitfalls knowledge but condensed; the operator skill can
  keep the long form.
- **Execution discipline.** Run any verification commands in the FOREGROUND; do NOT
  `run_in_background` + poll. Iterate against targeted tests; the daemon verifier runs
  the full suite after you report. Keep tool calls bounded.

## Verification

- `grep -qE "grep -rnI|skip binary|__pycache__" ap2/ideation.default.md` — the recursive-grep binary-skip rule is present in the ideation prompt.
- `grep -qE "Prose:" ap2/ideation.default.md` — the judge-bullet `Prose:` convention is documented.
- `uv run --extra dev pytest -q ap2/tests/ --ignore=ap2/tests/smoke` — the full suite (incl. any ideation-prompt structure test) stays green.
- `ap2/ideation.default.md` Prose: a verification-bullet shell-syntax conventions block instructs recursive greps to use `-I` (skip binary `.pyc`), the `!`-prefix for absence checks, `-r` for directory greps, no literal backticks, and `Prose:` for judge bullets; judge confirms via Read.

## Out of scope

- The auto-unfreeze self-heal fix-shape for this pitfall (sibling task).
- The operator-facing skills / memory copies (already carry the long form).
- Changing the verifier's bullet-extraction or grep behavior.

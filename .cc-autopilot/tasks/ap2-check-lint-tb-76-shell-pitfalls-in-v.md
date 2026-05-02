# TB-150 — `ap2 check`: lint TB-76 shell pitfalls in `## Verification` bullets

## Why
The ideation prompt's "Shell-bullet pitfalls to AVOID (TB-76)" section
enumerates three known authoring mistakes that fail the per-task
verifier with `exit 127`/`126` even when the underlying work is correct:

1. Bare `python ...` (daemon env has `uv run python`, `python3`,
   `.venv/bin/python` but typically not bare `python` on PATH).
2. Bare path as command, e.g. ``- `reports/foo/README.md` `` — shell
   tries to *execute* the markdown file (exit 126). Should be
   ``- `test -f reports/foo/README.md` `` for existence checks.
3. Multi-line shell bullets — verifier passes the whole bullet to
   `bash -c` as one argument; line breaks fragment it.

`ap2 check` already has the briefing-lint pattern for `Manual:`
bullets (TB-138, `_check_briefings_manual_bullets` at
`ap2/check.py:140-178`). Extend the same pattern to catch these
shapes before an operator approves the briefing.

This is purely additive — no behavioral change to the verifier or
to the daemon. Strictly an author-time safety net.

## Scope
1. New helper `_check_briefings_shell_bullets(cfg)` in `ap2/check.py`
   that walks `.cc-autopilot/tasks/*.md`, slices each briefing's
   `## Verification` section (reuse `_VERIFICATION_HEADER_RE` /
   `_NEXT_SECTION_RE`), iterates list bullets, and for each bullet
   that contains a backtick-fenced shell snippet (``- `<cmd>` ...``):
     - **bare-python**: emit a `warning` if the snippet's first
       token is exactly `python` (not `python3`, not `uv`, not
       `.venv/bin/python`). Recommend `uv run python` or `python3`.
     - **path-as-command**: emit a `warning` if the snippet is a
       single token that ends in `.md`, `.csv`, `.json`, `.yaml`,
       `.yml`, `.txt` AND does not start with a known command
       (`test`, `cat`, `grep`, `wc`, `head`, `tail`, etc.).
       Recommend `test -f <path>` for existence.
     - **multi-line**: emit a `warning` if the bullet's
       backtick-fenced snippet contains a literal newline byte
       (matched via `re.DOTALL` on the fenced span).
   Each pitfall hit emits a single `Issue` with the briefing
   filename + a one-line message naming the pitfall and the
   recommended replacement; severity `warning` (non-fatal, same as
   the Manual: lint).
2. Wire `_check_briefings_shell_bullets` into `check_project()` in
   `ap2/check.py` alongside the other lints (after
   `_check_briefings_manual_bullets`).
3. Tests in `ap2/tests/test_check.py`:
   - one test per pitfall: write a synthetic briefing into a tmp
     project's `.cc-autopilot/tasks/`, run `check_project(cfg)`, and
     assert exactly one warning whose `message` contains the pitfall
     keyword (e.g. "bare `python`", "path as command", "multi-line").
   - one test that a clean briefing (only auto-verifiable shapes:
     `uv run pytest -q`, `test -f path`, `grep -q ...`) emits zero
     shell-bullet warnings.
   - one test that a bullet outside `## Verification` (e.g. inside
     `## Scope` or `## Out of scope`) is ignored — only the
     verification section is gated.

## Verification
- `uv run pytest -q ap2/tests/test_check.py` — full check suite passes.
- `uv run pytest -q ap2/tests/` — full ap2 regression gate passes.
- `grep -q "_check_briefings_shell_bullets" ap2/check.py` — new
  helper is wired into the module.
- `grep -q "_check_briefings_shell_bullets" ap2/check.py` AND
  `grep -nE "issues.extend\(_check_briefings_shell_bullets\(cfg\)\)" ap2/check.py`
  — helper is invoked from `check_project`.
- prose: `_check_briefings_shell_bullets` in `ap2/check.py` walks
  `.cc-autopilot/tasks/*.md`, slices the `## Verification` body using
  the same header regexes the `Manual:` lint uses, and emits exactly
  one `Issue(severity="warning", ...)` per bullet that hits one of
  the three pitfalls (bare `python`, path-as-command, multi-line
  fenced snippet).
- prose: `ap2/tests/test_check.py` adds at least four new tests:
  one per pitfall (each asserts a single matching warning is
  produced) and one negative case (clean briefing emits zero
  shell-bullet warnings).

## Out of scope
- Auto-fixing the briefing — operator decides how to rewrite.
- Hardening the verifier itself (already covered by TB-147).
- Linting non-shell bullets (prose / test-name shapes); those are
  handled by the per-task judge.
- Migrating existing in-tree briefings — this lint surfaces the
  warnings; operators clean up at their own pace.

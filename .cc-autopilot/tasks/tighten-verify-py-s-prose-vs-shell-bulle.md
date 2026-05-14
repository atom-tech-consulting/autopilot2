# Tighten `verify.py`'s prose-vs-shell bullet classifier; codify `Prose:` prefix convention

Tags: `#autopilot` `#code-quality` `#code-cleanness` `#operator-surface` `#verifier` `#regression-pin`

## Goal

Advance goal.md's **Current focus: code quality** focus on two axes — (2) **Operator-facing documentation** (briefing-bullet authoring conventions are a load-bearing operator surface) and (4) **Code cleanness** (an upstream verifier defect with four recurring downstream symptoms). Close the design hole in `ap2/verify.py:178-225`'s `parse_verification_section` that has caused **four briefing-bullet verification_failed retry cascades in the 2026-05-12 → 2026-05-13 window alone** (TB-204, TB-206, TB-207, TB-209) — work that was correctly implemented but couldn't pass because the verifier mis-classified verification bullets. The current classifier rule is: "if a list item's first inline child is a codespan (backtick-fenced inline code), classify the bullet as SHELL — execute its codespan as a command; else classify as PROSE — send to the SDK judge." That rule is too aggressive: TB-209's last bullet was intended as judge-routed prose but opened with `` `ap2/tests/test_coverage_drift.py` `` (a backtick-fenced file path used as the bullet's grammatical subject) → mis-classified as shell → verifier ran the bare path as a command → `Permission denied`, exit 126, three retry attempts before retry-exhaust → Frozen.

The fix is structural: codify the `Prose:` prefix convention that's organically appeared in operator-authored briefings as the hard override for prose classification, AND add a heuristic fallback that recognizes judge-indicator phrases in the bullet body. Backward-compatible — existing well-formed bullets stay green; the trap goes away.

Why now: this is the n=4 incident. The pattern is no longer coincidence. Operator authoring discipline isn't sufficient (every operator-written briefing this session passed pre-approval review; the failures came from ideation-authored briefings that re-derive the trap from training-data conventions). The classifier is the upstream cause — fix the upstream cause, and the four (and counting) downstream symptoms stop. Goal.md L80-83's code-cleanness axis names the fix-the-classifier branch directly: structural smells that operators have to work around belong upstream of the workaround.

## Scope

(1) Update `ap2/verify.py:178-225`'s `parse_verification_section` (the AST walker that produces `VerifyBullet(kind=...)` records):

  - **Hard override**: if a list item's text (after the first inline child) begins with the literal prefix `Prose:` (case-sensitive, single colon, optional whitespace after), classify the bullet as `prose` regardless of whether the first inline child is a codespan. The `Prose:` prefix is the operator-authored signal "this is judge-routed, don't try to exec."

  - **Heuristic fallback**: for codespan-leading bullets WITHOUT a `Prose:` prefix, scan the bullet's full text for any of the judge-indicator phrases — `"Judge confirms"`, `"(judged by"`, `"judge confirms"`, `"the SDK against the diff"`, `"judged via"` — and classify as `prose` if any match. This catches the TB-209-shape case where ideation wrote a prose bullet with a backtick-fenced filename lead.

  - Existing well-formed bullets — those that lead with a SHELL command in a codespan and have no `Prose:` prefix and no judge-indicator phrases — continue to be classified as shell. Behavior is unchanged for the common case.

(2) Add a regression-pin test module `ap2/tests/test_verify_classifier.py` (or extend the closest existing test file for `parse_verification_section`). Pin all four observed-in-the-wild failure shapes — TB-204/TB-206/TB-207/TB-209 — as parametrized cases:

  - TB-204-shape: `[ "$(grep -lE 'pat' dir/ 2>/dev/null | wc -l)" -ge N ]` — directory-arg grep without `-r`. (Not actually a classifier bug — the bullet WAS shell-classified correctly; the script body failed at runtime. This test asserts the classifier still correctly classifies it as shell; the work to fix the bullet content was operator-side.)
  - TB-206-shape: `! grep "absent string" file` — `!` prefix bullet (correctly shell-classified). Same: assert classification is correct; the absence-check was operator-side.
  - TB-207-shape: literal backtick inside a bullet (`[ "$(grep -cE '^\| \`pat' file)" ... ]`) — currently breaks bullet extraction at the markdown-fence boundary. **Assert that the classifier either extracts the full intended command OR explicitly reports a malformed-bullet error in `VerifyBullet` rather than silently truncating to a half-command.** This is a real classifier bug — the markdown-extract should preserve embedded backticks via the AST walker (`mistune` already handles double-backtick codespans for literal-backtick content).
  - TB-209-shape: prose bullet leading with `` `path/to/file.py` `` (backtick-fenced filename) — under the new classifier, this should be classified as `prose` IF the bullet contains a judge-indicator phrase OR has a `Prose:` prefix. Pin both branches.

(3) Update `ap2/howto.md`'s briefing-authoring guidance to name the convention. Add a section (or extend an existing one if it covers briefing Verification authoring) that explicitly states:
  - Prose bullets must be prefixed with `Prose:` for explicit classification.
  - Shell bullets must NOT contain literal backticks in their command body (use `.` regex any-char as the markdown-fence-character workaround).
  - Absence-check shell bullets must use the `!` exit-inversion prefix.
  - Directory-walking grep must use `-r`.

(4) Don't change the `_run_shell_bullet` or `_judge_prose_bullet` execution paths — only the classifier (parse step). The two execution paths are well-tested and behavior at execution stays unchanged.

(5) Don't add a new ap2 CLI `lint-briefing` subcommand or pre-flight validator. The classifier-fix + docs convention close the recurring class without adding a parallel verification surface (`test_docs_drift.py` + the new pytest fixture in TB-204 + the new classifier tests are the gates; no fourth gate needed).

## Design

The "`Prose:` prefix" convention is already organically present in operator-authored briefings (TB-206, TB-207, TB-209 operator-fix briefings all use it). The classifier change codifies what operators were already doing. Ideation will pick up the convention from the docs update.

The heuristic-fallback list (`Judge confirms`, `(judged by`, etc.) is conservatively scoped — five strings, all already-present in observed prose bullets. Adding to the list later is cheap.

The TB-207-shape fix is the deepest one — `mistune`'s codespan AST already handles literal backticks via the double-backtick form (`` ` `cmd` ` `` → codespan with raw `\`cmd\``). The current `_list_item_leading_codespan` strips the wrapping but the issue may be in earlier markdown-fence stripping that pre-processes the bullet before AST parsing. Read the actual error mode in `.cc-autopilot/debug/20260513T011559Z-TB-207.stream.jsonl` to identify the exact step that truncated, and patch there.

Why pin all four failure shapes (not just the classifier-bug-driven two): regression-pin discipline. The classifier change is one-way — once it ships, the only thing that'll cause these classes to fail again is a regression in the parse step. The four parametrized tests are exactly the right anti-drift gate.

Operator-facing docs: this is goal.md L65-72's "operator-facing documentation" axis. The howto update names the convention; operators (human or ideation) writing future briefings have a single canonical reference for the bullet shape.

## Verification

- `uv run pytest -q ap2/tests/` — full suite green (exit 0); no existing test changes behavior unexpectedly.
- `uv run pytest -q ap2/tests/test_verify_classifier.py` — the new module's tests all pass (exit 0); minimum 6 tests across the four shape classes (2 for TB-207 — fixed and previously-broken; 2 for TB-209 — `Prose:` prefix branch and judge-indicator branch).
- `! grep -nE "Permission denied" .cc-autopilot/debug/*.stream.jsonl 2>/dev/null | head -1 | grep -q .` — exits 0 (no new TB-209-shape `Permission denied` errors in debug dumps after this task's commit; the `head -1 | grep -q .` shape only fires if there's at least one match, and `!` inverts).

  Note this is a soft signal — pre-existing debug dumps in `.cc-autopilot/debug/` may contain the old failures. The judge confirms via reading: "no debug dumps generated AFTER this task's commit timestamp contain `Permission denied` errors traced to bullet-classification."

- `grep -nE "^def parse_verification_section" ap2/verify.py` — exits 0; the function still exists (sanity).
- `grep -nE "Prose:" ap2/howto.md` — exits 0 with at least 2 matches (the documented convention appears in the howto with an explanatory paragraph and at least one example).
- `grep -nE "judge.indicator|judge_indicator|JUDGE_INDICATOR" ap2/verify.py` — exits 0 (the heuristic-fallback constant or function is present and named consistently for greppability).
- Prose: the four parametrized test cases in `test_verify_classifier.py` each include a docstring or comment naming the TB-N that motivated them (TB-204 / TB-206 / TB-207 / TB-209). Judge confirms via `Read` of the test bodies — the TB-N traceability matters for future debugging.
- Prose: `ap2/howto.md`'s new convention section names all four pitfalls (no `-r` on dir grep, missing `!` on absence checks, literal backtick in shell bullets, `Prose:` prefix for judge bullets) with one-line guidance per pitfall. Judge confirms via `Read`.

## Out of scope

- Adding a `lint-briefing` ap2 CLI verb or a pre-add validator (separate surface — the classifier-fix + docs convention is the structural fix).
- Modifying the prose bullet judge prompt (`_judge_prose_bullet`) — execution-side, orthogonal.
- Modifying `_run_shell_bullet`'s subprocess invocation — execution-side, orthogonal.
- Auto-rewriting existing briefings to add `Prose:` prefixes or strip literal backticks (briefings are ideation-authored or operator-authored; retroactive rewrites are a separate cleanup if needed).
- Migrating from `mistune` to a different markdown parser.
- Adding a "shell vs prose" `kind:` field to the briefing markdown itself (e.g. `shell:` / `prose:` as YAML-front-matter or per-bullet attributes) — operator-facing markdown should stay markdown.

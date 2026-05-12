# TB-204 — Extract canonical-valid briefing fixture for tests

Tags: `#autopilot` `#tests` `#code-quality` `#reusability` `#briefing`

## Goal

Close the reusability failure mode goal.md L74-78 names — "the same bug appearing at multiple call sites because logic was copy-pasted instead of shared" — on the new **current focus: code quality** focus's (3) **Code reusability** axis: the canonical-valid briefing template (the structurally-complete `## Goal` + `Why now:` line + `## Scope` + `## Design` + `## Verification` + `## Out of scope` scaffold that satisfies `_validate_briefing_structure` post-TB-154/TB-161/TB-164/TB-171) is inlined at >30 sites across the test suite. Extract to a single test-helper module so adding the next validator rule (TB-138/TB-171-shape) is a one-file fixture update instead of a 17-file churn.

Why now: when TB-164 added the `Why now:` requirement, the implementing commit touched 17 test files (`ap2/tests/test_cli.py`, `test_check.py`, `test_operator_queue.py`, `test_rollback.py`, `test_tb132_verification.py`, `test_tb135_verification.py`, `test_tools.py`, `e2e/test_mattermost_cron.py`, `e2e/test_operator_queue_tick.py`, `e2e/test_review_gate.py`, `e2e/test_tb142_mm_queue_routing.py`, `e2e/test_verify.py`, `e2e/test_verify_per_task.py` per the TB-164 progress entry); `test_tools.py` alone has 26 `Why now:` occurrences and 150 occurrences of `## Goal`/`## Scope`/`Verification` markers in inline briefing strings. Future validator additions (TB-138-shape Manual-bullet detector extensions, hypothetical Design-body min-length, hypothetical Out-of-scope structure check) all face the same 17-file churn cost. The threshold goal.md L74-77 names is three+ call sites with structural similarity — this is >>3, and the duplicates are byte-near-identical (only TB-N and 1-2 prose lines differ). Without this work, every validator-rule task pays a tax goal.md L91-92 ("Reusable helpers reduce the volume of code that needs testing and documenting") flags as anti-pattern.

## Scope

(1) Add `ap2/tests/_briefing_fixtures.py` (underscore-prefixed → not collected by pytest as tests). Public API:

  - `canonical_briefing(task_id: str, *, title: str = "Test task", goal_anchor: str = "current focus: code quality", why_now: str = "...", scope: str = "...", design: str = "...", verification: str = "...", out_of_scope: str = "...") -> str` — returns a string that passes `_validate_briefing_structure` with the given anchor + Why-now line + structurally-complete sections.
  - `minimal_briefing(task_id: str, **kwargs) -> str` — same shape but uses the shortest acceptable bodies for each section (Why-now exactly `WHY_NOW_MIN_CHARS` + 1 chars, Verification with a single backtick-fenced shell bullet).
  - `briefing_missing(task_id: str, *, drop: str) -> str` — returns a briefing with the named section removed (or its body zeroed); `drop` accepts `"Goal"`, `"Why now"`, `"Scope"`, `"Design"`, `"Verification"`, `"Out of scope"`, or `"goal-anchor"` for the TB-161 case.
  - `briefing_with_manual_bullet(task_id: str) -> str` — canonical shape but the Verification section contains a `Manual:` bullet (TB-171 reject case).

  Defaults are sourced from `ap2.init.BRIEFING_TEMPLATE` where possible (so a future template change flows to the fixture for free).

(2) Migrate inline duplicates in:
  - `ap2/tests/test_tools.py` (≥25 sites — the canonical-shape fixtures inline as triple-quoted strings)
  - `ap2/tests/test_cli.py` (≥4 sites)
  - `ap2/tests/test_operator_queue.py` (≥6 sites)
  - `ap2/tests/test_check.py`
  - `ap2/tests/test_rollback.py`
  - `ap2/tests/test_init.py`
  - `ap2/tests/test_ideation_proposals.py`
  - `ap2/tests/test_backfill_proposals.py`
  - `ap2/tests/test_tb132_verification.py`
  - `ap2/tests/test_tb135_verification.py`
  - `ap2/tests/e2e/test_mattermost_cron.py`
  - `ap2/tests/e2e/test_operator_queue_tick.py`
  - `ap2/tests/e2e/test_review_gate.py`
  - `ap2/tests/e2e/test_tb142_mm_queue_routing.py`
  - `ap2/tests/e2e/test_verify.py`
  - `ap2/tests/e2e/test_verify_per_task.py`
  - `ap2/tests/test_docs.py`

  Migration rule: ANY site whose inline string contains all of `## Goal`, `## Scope`, `## Verification`, AND `Why now:` (the structurally-complete shape) switches to the fixture. Sites that build genuinely non-canonical shapes (e.g. reject-path tests for specific malformed inputs) use `briefing_missing` / `briefing_with_manual_bullet` rather than reverting to inline strings.

(3) Don't touch `ap2/init.py` `BRIEFING_TEMPLATE` itself — that's the operator-facing template the fixture sources defaults from. Don't add a public re-export from `ap2/__init__.py`; the fixture is test-internal.

(4) Don't migrate sites where the inline briefing is the SUBJECT of the test (e.g. a test that constructs a specifically-malformed briefing to exercise a reject path) — only structurally-canonical "happy path" briefings are deduplicated.

## Design

The fixture module exports pure-function builders, not pytest fixtures (no `@pytest.fixture` decorator) — call sites then read as plain function calls without depending on test-discovery magic. This keeps the helper usable from both inside test files AND from non-test ad-hoc scripts (e.g. `adhoc/` regression sweeps) without pulling in pytest.

Each builder returns a `str` not a `Path` — call sites that need a temp-dir-written briefing (e.g. `do_operator_queue_append`'s add-op path) do their own `(tmp_path / "brief.md").write_text(canonical_briefing("TB-N"))`. Keeps the helper composable and free of fixture-scope decisions.

The `goal_anchor` default is "current focus: code quality" — matches today's `goal.md`'s `## Current focus` heading title verbatim, which is the cheapest valid anchor (per TB-161 `_goal_md_anchors`). For tests that exercise the goal-anchor reject path explicitly (e.g. test_check.py's TB-161 lint coverage), `briefing_missing(task_id, drop="goal-anchor")` returns a briefing whose Goal body contains neither a current-focus title nor a Done-when bullet — a single named call site replaces the current inline "manually craft a non-anchored briefing" boilerplate.

Sequencing risk: the migration is mechanical but touches >17 files. The fix is single-PR-atomic — the fixture lands together with all migrations, so no intermediate state where some tests use the helper and others don't. Reviewing diff size is large but each hunk is `inline triple-quoted string → canonical_briefing("TB-N")`-shape — visually scannable.

## Verification

- `uv run pytest -q ap2/tests/` — full regression suite green (no test should change behavior; only the inline strings consolidate).
- `test -f ap2/tests/_briefing_fixtures.py` — fixture module file exists.
- `grep -nE "^def canonical_briefing\(|^def minimal_briefing\(|^def briefing_missing\(|^def briefing_with_manual_bullet\(" ap2/tests/_briefing_fixtures.py` — exit 0 (all four public builders present).
- `[ "$(grep -lE 'from ap2.tests._briefing_fixtures import|from .._briefing_fixtures import|from ._briefing_fixtures import' ap2/tests/ ap2/tests/e2e/ 2>/dev/null | wc -l)" -ge 10 ]` — at least 10 test files import the fixture (sanity bound; expected actual is 15+).
- `[ "$(grep -c '## Goal' ap2/tests/test_tools.py)" -le 20 ]` — inline `## Goal` markers in test_tools.py drop from 150 to ≤20 (most should now be inside the fixture or in genuinely-malformed test inputs).
- `[ "$(grep -c 'Why now:' ap2/tests/test_tools.py)" -le 5 ]` — `Why now:` markers in test_tools.py drop from 26 to ≤5.
- Prose: `ap2/tests/_briefing_fixtures.py` is module-internal (not re-exported from `ap2/__init__.py` or `ap2/tests/__init__.py`); confirm via grep the file isn't imported from `ap2/` non-test source (judge confirms via `Grep -r "_briefing_fixtures" ap2/` excluding `ap2/tests/`).
- Prose: migrated sites still test the same behavior — happy-path tests use `canonical_briefing(...)`, reject-path tests use `briefing_missing(...)` / `briefing_with_manual_bullet(...)` (judge confirms by reading 3-5 migrated test functions and verifying the test still asserts the same property against the new fixture-built input).

## Out of scope

- Re-exporting from `ap2/__init__.py` or `ap2/tests/__init__.py` (keep test-internal).
- Migrating production code that builds briefings (e.g. `ap2/backfill.py`'s synthesis — that's not the test surface; different concerns).
- Adding a pytest fixture decorator wrapper (the function-call shape is more composable and reads more obviously at the call site).
- Renaming `BRIEFING_TEMPLATE` in `ap2/init.py` (orthogonal; the fixture sources defaults from it without renaming).

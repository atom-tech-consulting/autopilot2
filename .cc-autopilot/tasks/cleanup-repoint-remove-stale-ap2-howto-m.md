# Cleanup: repoint/remove stale `ap2/howto.md` references in code + test comments (howto.md was retired by TB-406)

Tags: #autopilot #docs #cleanup #howto #skills

## Goal

`ap2/howto.md` was retired and deleted by TB-406 (its operator manual was carved
into the `skills/ap2-*` domain skills). The functional cross-references (drift
gates, `sync-assets`) were repointed, but ~8 code **comments** and several test
**docstrings** still mention the now-deleted `ap2/howto.md`, leaving dangling
pointers that mislead a reader into looking for a file that no longer exists.
The full suite is already green (these are non-functional), so this is a pure
documentation-hygiene sweep. Meta-infra cleanup, no focus anchor.

Known stale references (non-exhaustive — sweep for all):
- Code: `ap2/config_loader.py`, `ap2/cli_config.py`, `ap2/events.py`,
  `ap2/_shared.py`, `ap2/prompts.py`, `ap2/core_config_schema.py`,
  `ap2/verify.py`, `ap2/init.py`.
- Tests: `ap2/tests/test_coverage_drift.py`, `ap2/tests/test_tb336_*`,
  `ap2/tests/test_tb289_*`, `ap2/tests/test_tb287_*`, `ap2/tests/test_tb324_*`,
  `ap2/tests/test_tb244_*`.

## Scope

- Sweep `ap2/` (code + tests) for every `howto.md` / `ap2/howto.md` /
  `HOWTO_PATH` reference.
- For each, **repoint or remove**:
  - A comment/docstring that directs a reader to `howto.md` for content (e.g.
    "the canonical list lives in `ap2/howto.md`'s `## Event schema`") →
    repoint to the **owning skill** that now holds it (e.g.
    `skills/ap2-observability/SKILL.md`, `skills/ap2-config/SKILL.md`,
    `skills/ap2-board-ops/SKILL.md`, `skills/ap2-task/SKILL.md`,
    `skills/ap2-failure-recovery/SKILL.md`, `skills/ap2-ideation-goals/SKILL.md`)
    or to `ap2/architecture.md` where the content landed there.
  - A purely historical / provenance mention adding no navigational value →
    remove it (provenance lives in git history + the TB briefings).
- Remove any now-unused `HOWTO_PATH` constant in `ap2/tests/test_docs_drift.py`
  (TB-406 was to drop it once unused; verify it is gone).
- Do NOT change any test ASSERTION logic or gate behavior — only comment/
  docstring text and dead constants. The gates were already retargeted onto the
  skills by the carve tasks; this task must not regress them.

## Design

- The destination for each reference is the skill (or `architecture.md` section)
  that the corresponding howto section was carved into — see the TB-397→406
  carve mapping. Match content to its new home rather than blindly deleting, so
  a reader following the comment still lands on the right doc.
- Scope is `ap2/` code + tests only. `README.md`'s historical note that TB-406
  retired the `ap2-howto.md` deploy is accurate provenance and out of scope.

## Verification

- `! grep -rniE 'howto\.md' ap2/ --include='*.py'` — no reference to the deleted `howto.md` remains in any code or test file.
- `uv run --extra dev pytest -q ap2/tests/ --ignore=ap2/tests/smoke` — full suite stays green (no gate/assertion regressed by the comment + dead-constant edits).
- `ap2/` Prose: each former `howto.md` pointer now either points at the owning `skills/ap2-*/SKILL.md` (or `architecture.md`) that holds that content, or was removed as bare provenance; no comment or docstring directs a reader to the deleted `ap2/howto.md`, and no test assertion logic changed. Judge confirms via Read/Grep.

## Out of scope

- The orphan TB-404 briefing (`.cc-autopilot/tasks/retire-ap2-howto-md-as-a-file-relocate-r.md`) — it is a fenced daemon-owned file the task agent cannot touch; operator/janitor cleanup, tracked separately.
- `README.md` historical provenance and any skill content.
- Re-deploying skills or changing `sync-assets`.

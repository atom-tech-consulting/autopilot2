## Goal

Advance Current focus: consolidate the operator manual into auto-triggered, cross-runtime skills. Carve the operator-facing goal/focus-management domain out of the monolithic `ap2/howto.md` into a new auto-triggered domain skill, following the SKILL.md + in-commit gate-retarget conventions already proven on two prior carves (the observability and config skills): a `SKILL.md` with `name`/`description` frontmatter, the reference content in the body, and every docs-drift / docs-location gate that asserted coverage in the moved howto sections retargeted onto the new skill IN THE SAME COMMIT. The carved domain is "ideation + goal/focus management" — howto's `## Authoring goal.md` (Mission / Done-when / Current focus / Non-goals / Constraints / the delete-test convention) and `## Retrospective audit workflow` (`ap2 backfill-proposals`, `ap2 classify TB-N --delete-test`, reading `ideation_state.md` + per-proposal records). This is operator-session tooling only: do NOT move the daemon ideation agent's briefing-authoring conventions, which stay canonical in `ideation.default.md` (goal.md L126-129) — the skill may reference them but `ideation.default.md` is the source of truth. Leave a one-line "see the ap2-ideation-goals skill" pointer in howto where each section was, the same pointer shape the config carve used.

Why now: howto's goal-authoring + audit content is reachable only via the hand-maintained `~/.claude/CLAUDE.md` pointer, so an operator steering the loop (writing goal.md, classifying shipped proposals) never gets it surfaced on a task match — carving it into an auto-triggered skill closes that discovery gap and retires two more sections from the howto surface being dismantled.

## Scope

- New `skills/ap2-ideation-goals/SKILL.md` with `name` + `description` frontmatter; the `description` is a tight auto-trigger naming goal.md authoring + the proposal/delete-test retrospective workflow.
- Move the body of howto's `## Authoring goal.md` and `## Retrospective audit workflow` into the skill; replace each howto section with a one-line pointer to the skill.
- Grep `ap2/tests/` for docs-drift / docs-location gates whose coverage target is one of these two sections (HOWTO_PATH reads tied to goal-authoring or audit/classify assertions) and retarget them onto the new SKILL path in the same commit.
- Do NOT touch `ideation.default.md`'s daemon-internal briefing-authoring conventions.

## Design

Follow the docs-carve pattern already on disk in `skills/ap2-config/SKILL.md` + `ap2/tests/test_docs_drift.py`. (1) Create `skills/ap2-ideation-goals/SKILL.md` with agentskills.io frontmatter (`name:` + `description:`); move the two howto section bodies verbatim into coherent skill sections, fixing any cross-references that were repo-relative to `ap2/howto.md` so they resolve at the deployed skill path. (2) In `ap2/howto.md`, replace each carved section with a one-line `see the ap2-ideation-goals skill` pointer (the existing `## Configuration reference — see the ap2-config skill` pointer at howto L936 is the model). (3) In `ap2/tests/test_docs_drift.py`, add an `IDEATION_GOALS_SKILL = REPO_ROOT / "skills/ap2-ideation-goals/SKILL.md"` constant and repoint any gate/docs-location pin that read the moved sections out of `HOWTO_PATH` onto it — the same edit shape as the existing `OBSERVABILITY_SKILL` and `CONFIG_SKILL` constants and their retargeted gates; leave gates for not-yet-carved sections on `HOWTO_PATH`. Keep the change scoped to docs + the drift test; no daemon behavior change.

## Verification

- `test -f skills/ap2-ideation-goals/SKILL.md` — the new skill file exists.
- `grep -qE '^name:' skills/ap2-ideation-goals/SKILL.md` — SKILL.md carries a `name` frontmatter field.
- `grep -qE '^description:' skills/ap2-ideation-goals/SKILL.md` — SKILL.md carries a `description` frontmatter field.
- `grep -qi 'Done when' skills/ap2-ideation-goals/SKILL.md` — the goal-authoring reference content (Done-when guidance) landed in the skill.
- `grep -qi 'delete-test' skills/ap2-ideation-goals/SKILL.md` — the retrospective-audit / delete-test reference content landed in the skill.
- `grep -qi 'ap2-ideation-goals' ap2/howto.md` — howto retains a pointer to the new skill where the sections were carved.
- `uv run pytest -q ap2/tests/test_docs_drift.py` — the docs-drift gates pass after retargeting (no gate still asserts the moved sections' coverage in HOWTO_PATH).
- `uv run pytest -q ap2/tests/` — the full regression suite stays green.
- `skills/ap2-ideation-goals/SKILL.md` Prose: the skill carves operator-facing goal/focus authoring + proposal retrospective content and does NOT duplicate `ideation.default.md`'s daemon-internal briefing-authoring conventions; judge confirms via Read that `ideation.default.md` remains the canonical source for those conventions.

## Out of scope

- The monitoring/status and failure-recovery domain carves (separate tasks).
- Retiring `ap2/howto.md` as a file / dropping the `sync-assets` howto target (the final retirement task, after all carves land).
- Cross-runtime deploy (TB-401).

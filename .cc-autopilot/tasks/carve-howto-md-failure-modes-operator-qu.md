## Goal

Advance Current focus: consolidate the operator manual into auto-triggered, cross-runtime skills. Carve the failure-recovery / operator-playbook domain out of `ap2/howto.md` into a new auto-triggered domain skill, following the TB-397 (observability) + TB-398 (config) canary conventions: a `SKILL.md` with `name`/`description` frontmatter, the reference content in the body, and every docs-drift / docs-location gate that asserted coverage in the moved sections retargeted onto the new skill IN THE SAME COMMIT. The carved domain is "failure-recovery / operator-playbook" — howto's `## Failure modes the daemon recovers from` (what auto-recovers: verification fails, retries exhaust, daemon restart, cron drift, agent timeouts) and `## Operator-question playbook` (when the daemon escalates and how the operator intervenes: unfreeze, re-dispatch, frozen-task triage). Leave a one-line "see the ap2-failure-recovery skill" pointer in howto where each section was, exactly as TB-398 left a config pointer.

Why now: the recovery/escalation manual is precisely the content an operator reaches for when something has gone wrong, yet today it surfaces only via the hand-maintained `~/.claude/CLAUDE.md` pointer into a ~184 KB monolith — carving it into an auto-triggered skill means a failure-mode question surfaces the right slice on match, and retires two more sections from the howto surface being dismantled.

## Scope

- New `skills/ap2-failure-recovery/SKILL.md` with `name` + `description` frontmatter; the `description` is a tight auto-trigger naming failure recovery + the operator-intervention playbook.
- Move the body of howto's `## Failure modes the daemon recovers from` and `## Operator-question playbook` into the skill; replace each howto section with a one-line pointer to the skill (the TB-398 shape).
- Grep `ap2/tests/` for docs-drift / docs-location gates whose coverage target is one of these two sections and retarget them onto the new SKILL path in the same commit, mirroring TB-397/TB-398's `test_docs_drift.py` edits.

## Design

Mirror the established two-carve pattern exactly. (1) Create `skills/ap2-failure-recovery/SKILL.md` with agentskills.io frontmatter (`name:` + `description:`) modeled on `skills/ap2-config/SKILL.md`; move the two howto section bodies verbatim into coherent skill sections, fixing any cross-references that were repo-relative to `ap2/howto.md` so they resolve at the deployed skill path. (2) In `ap2/howto.md`, replace each carved section with a one-line `see the ap2-failure-recovery skill` pointer (the L936 `## Configuration reference — see the ap2-config skill` shape TB-398 used). (3) In `ap2/tests/test_docs_drift.py`, add a `FAILURE_RECOVERY_SKILL = REPO_ROOT / "skills/ap2-failure-recovery/SKILL.md"` constant and repoint any gate/docs-location pin that read the moved sections out of `HOWTO_PATH` onto it — the same edit shape as the `OBSERVABILITY_SKILL` (TB-397) and `CONFIG_SKILL` (TB-398) retargets; leave gates for not-yet-carved sections on `HOWTO_PATH`. Keep the change scoped to docs + the drift test; no daemon behavior change.

## Verification

- `test -f skills/ap2-failure-recovery/SKILL.md` — the new skill file exists.
- `grep -qE '^name:' skills/ap2-failure-recovery/SKILL.md` — SKILL.md carries a `name` frontmatter field.
- `grep -qE '^description:' skills/ap2-failure-recovery/SKILL.md` — SKILL.md carries a `description` frontmatter field.
- `grep -qi 'recover' skills/ap2-failure-recovery/SKILL.md` — the failure-modes auto-recovery reference content landed in the skill.
- `grep -qi 'unfreeze' skills/ap2-failure-recovery/SKILL.md` — the operator-intervention playbook content (frozen-task unfreeze) landed in the skill.
- `grep -qi 'ap2-failure-recovery' ap2/howto.md` — howto retains a pointer to the new skill where the sections were carved.
- `uv run pytest -q ap2/tests/test_docs_drift.py` — the docs-drift gates pass after retargeting.
- `uv run pytest -q ap2/tests/` — the full regression suite stays green.
- `skills/ap2-failure-recovery/SKILL.md` Prose: the skill consolidates the failure-modes + operator-question-playbook reference into one coherent recovery domain with an auto-trigger description; judge confirms via Read that the moved howto sections are reduced to pointers (content not duplicated across both surfaces).

## Out of scope

- The monitoring/status and ideation-goals domain carves (separate tasks).
- Retiring `ap2/howto.md` as a file / dropping the `sync-assets` howto target (the final retirement task, after all carves land).
- Cross-runtime deploy (TB-401).

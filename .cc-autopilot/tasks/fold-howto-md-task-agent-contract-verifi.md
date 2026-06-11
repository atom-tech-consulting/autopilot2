## Goal

This task advances goal.md's "Current focus: consolidate the operator manual into auto-triggered, cross-runtime skills" by consolidating howto's `## The task agent contract` and `## Authoring \`## Verification\` bullets` (including the `Prose:`-prefix convention, the four shell-bullet authoring pitfalls, and `## Classify verdicts`) into the EXISTING `skills/ap2-task/SKILL.md`, mirroring — not relocating — the daemon-canonical authoring rules. Per the focus, briefing-authoring conventions the ideation agent follows stay canonical in `ap2/ideation.default.md`; the operator-facing skill mirrors them.

Why now: howto and the existing `ap2-task` skill already overlap on briefing authoring; folding the howto reference into `ap2-task` removes a duplicate operator surface and prevents a 4th overlapping authoring skill, while keeping `ideation.default.md` the single source of truth for the daemon's agents.

## Scope

- Merge howto's task-agent-contract + verification-bullet-authoring + classify-verdicts reference into `skills/ap2-task/SKILL.md` (extend the existing skill; do not fork a new one).
- Remove the moved sections from `ap2/howto.md`; fix dangling cross-references.
- Do NOT modify `ap2/ideation.default.md` — it remains the canonical daemon copy; the skill explicitly states it mirrors that source.
- If `ap2/tests/test_tb273_ideation_pitfalls_sync.py` (or a sibling) pins howto↔prompt pitfall sync, repoint it at the skill or keep it on the canonical prompt so the sync gate stays green.

## Design

- The `ap2-task` skill gains a reference section; its frontmatter `description` is extended (if needed) to also auto-trigger on verification-bullet authoring and classify-verdict questions, without over-broadening the trigger.

## Verification

- `! grep -q '## The task agent contract' ap2/howto.md` — task-agent contract section retired from howto.
- `grep -q 'Prose:' skills/ap2-task/SKILL.md` — the prose-prefix authoring convention is mirrored into the skill.
- `skills/ap2-task/SKILL.md` Prose: the skill now carries the task-agent contract, the four shell-bullet authoring pitfalls, and the classify-verdict reference, and explicitly states that `ap2/ideation.default.md` remains the canonical daemon source for briefing-authoring rules; judge confirms via Read.
- `uv run --extra dev pytest -q ap2/tests/ --ignore=ap2/tests/smoke` — full suite green (including any repointed pitfall-sync gate).

## Out of scope

- Carving the observability / config / board-ops domains (TB-397 / TB-398 / TB-399).
- Changing `ap2/ideation.default.md` content, deleting `ap2/howto.md`, or touching deploy targets (later retirement + TB-401).

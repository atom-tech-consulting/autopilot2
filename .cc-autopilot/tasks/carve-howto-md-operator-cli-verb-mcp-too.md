## Goal

This task advances goal.md's "Current focus: consolidate the operator manual into auto-triggered, cross-runtime skills" by carving howto's operator-action reference — `## Operator CLI verbs (reference)` and `## Custom MCP tools (reference)` — into `skills/ap2-board-ops/SKILL.md` and retargeting the CLI-verb and MCP-tool coverage gates onto it. (The MCP-tool gate today accepts howto OR `architecture.md`; keep `architecture.md` in the accepted set and add the skill.)

Why now: the CLI-verb and MCP-tool references are pure operator-action lookup content that belongs on a task-matched board-ops skill, not buried in a 3,100-line manual; carving them retargets two more drift gates off howto and shrinks the surface the eventual howto-retirement task must clear.

## Scope

- Create `skills/ap2-board-ops/SKILL.md` (frontmatter + progressive disclosure) per the TB-397 canary conventions.
- Move howto's `## Operator CLI verbs (reference)` and `## Custom MCP tools (reference)` into the skill; include the board-section model so the skill is self-contained.
- Retarget `ap2/tests/test_docs_drift.py`'s CLI-verb gate (`test_every_cli_verb_documented`) to read the skill; extend the MCP-tool gate's combined source to include the skill alongside `architecture.md`.
- Remove the moved sections from `ap2/howto.md`; fix dangling cross-references.

## Design

- Reuse the canary's gate-retarget constant pattern; the MCP-tool gate keeps its howto-OR-architecture fallback semantics while adding the skill so no MCP tool currently documented in howto becomes uncovered mid-migration.

## Verification

- `test -f skills/ap2-board-ops/SKILL.md` — board-ops skill exists.
- `grep -qE '^description:' skills/ap2-board-ops/SKILL.md` — auto-trigger description present.
- `! grep -q '## Operator CLI verbs (reference)' ap2/howto.md` — CLI-verb reference retired from howto.
- `grep -q 'ap2-board-ops' ap2/tests/test_docs_drift.py` — CLI-verb & MCP-tool gates retargeted.
- `uv run pytest -q ap2/tests/` — full suite green (CLI-verb + MCP-tool coverage now enforced against the skill).

## Out of scope

- Carving non-board-ops howto domains (TB-397 / TB-398 / TB-400).
- Deleting `ap2/howto.md` or changing deploy targets (later retirement + TB-401).

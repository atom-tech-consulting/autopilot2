I'll process the input directly. Let me identify sentences to delete according to your criteria:

**Sentences to DELETE:**
1. "The first three close out the now-Shipped **component-boundary** focus (marked Shipped in goal.md 2026-06-09);" — names condition of exhaustion (closing out a focus)
2. "This is why each carve MUST retarget its gate in-commit." — This is kept (factual instruction, not an exhaustion claim)
3. Actually reviewing more carefully: "All still serve the Mission..." — This is kept (factual observation)

Let me re-examine more carefully for the specific delete criteria:

The sentence "The first three close out the now-Shipped **component-boundary** focus (marked Shipped in goal.md 2026-06-09);" asserts that work closes out a focus that is exhausted/Shipped.

---

# Ideation State

_Last updated: 2026-06-10T23:18Z by ideation cron_

## Mission alignment

5 most recent Completes — TB-396 (agent_model default made provider-neutral
`None`, both backends self-default, codex smokes route model through prod
config, c2597f1); TB-395 (synced skills/ap2 SKILL.md + howto prose-judge
diagnostics to the task_solve/task_verify event names, 64e9af2); TB-392
(minimal-kernel dispatch→verify→report e2e green with every component
disabled); TB-391 (ideation extracted into `ap2/components/ideation/` behind a
registry tick phase); TB-389 (channel surface folded into an always-on
`communication` component). All still serve the Mission's "small core + opt-in
components, portable skills as the prerequisite for an OSS cut" — and TB-395
already touched the skills surface this new focus consolidates.

## Current focus assessment

- **Current focus: consolidate the operator manual into auto-triggered,
  cross-runtime skills** (goal.md L101)
  - Progress so far: ZERO Complete TB-Ns under this focus — the operator set it
    18 min ago (operator_log 2026-06-10T22:57:38Z update_goal + forced ideate)
    and marked the component-boundary focus Shipped. Adjacent prior work: TB-395
    refreshed `skills/ap2/SKILL.md`, so the 3 existing operator skills
    (ap2, ap2-task, migrate-to-ap2) are current; `ap2/howto.md` is the ~3,100-line
    manual still standing.
  - Gaps (the three axes, all unaddressed):
    (1) Carve `ap2/howto.md` into ~6–9 domain `SKILL.md` skills; today its
    reference content (event schema, config knobs, CLI/MCP refs, task contract,
    verification authoring, failure recovery) lives only in the monolith.
    (2) Retarget the docs-drift gates: `ap2/tests/test_docs_drift.py` gates env
    knobs / config keys / event types / CLI verbs / MCP tools against
    `HOWTO_PATH` — each gate must move to the skill that absorbs its surface, in
    the SAME commit, or the regression gate breaks.
    (3) Cross-runtime deploy: `sync_assets` (ap2/sandbox.py) deploys skills only
    to `~/.claude/skills` (+ `ap2-howto.md`); no `~/.agents/skills` target, no
    `AGENTS.md`, discovery pointer hand-maintained.
  - Status: `in-progress`
  - Reasoning: no Completes yet — status is mechanically `in-progress`; the
    three axes give a clear, sequenceable decomposition.

## Non-goal risk check

The focus is a docs/tooling restructure with no daemon behavior change (focus
L117), so the "Removing behavior during component extraction" and "API
stability" non-goals don't bind. One real risk: spawning a 4th overlapping
authoring surface (howto vs. existing `ap2-task` skill vs. `ideation.default.md`)
— mitigated by folding the howto task-authoring reference INTO `ap2-task` and
keeping `ideation.default.md` the canonical daemon copy (TB-400). No goal.md
mutation, no cron change. none.

## Considered & deferred this cycle

- **Remaining domain skills (ap2-monitoring/status, ap2-ideation-goals,
  ap2-failure-recovery + operator playbook)**: deferred to next cycle — the
  canary (TB-397) must settle SKILL.md frontmatter + gate-retarget conventions
  before the rest fan out; proposing all ~7 carves now is the TB-78 too-large /
  over-proposing shape.
- **Final "retire `ap2/howto.md` + drop its sync-assets target + flip residual
  gates" task**: NOT proposed — it depends on EVERY carve landing first, so
  proposing it now is exactly the out-of-sequence axis shape the operator
  rejected in TB-384. Surfaces once the carves are done.
- **Recurring operator-rejection pattern**: vetoes punish out-of-sequence /
  duplicate axis work (TB-384) and speculative enumerated-case validators
  (TB-172, TB-240, TB-231). This wave mitigates the first by gating TB-398/399/400
  behind the canary TB-397 (`@blocked:review,TB-397`), and proposes no new
  enumerated-case lint.

## Cycle observations

- The docs-drift gates couple carve↔gate-retarget: moving a gated section out of
  `howto.md` while the gate still reads `HOWTO_PATH` breaks `uv run pytest`, so
  each carve MUST retarget its gate in-commit. This is why each carve briefing
  bundles the gate retarget + uses the full suite as a verification bullet
  (carried as the load-bearing reason for the wave's task shape).

## Decisions needed from operator

none this cycle — the proposed skill taxonomy is expressed AS the five
review-gated proposals below; the operator steers boundaries by approving /
rejecting / reordering them individually.

## Proposals this cycle

- TB-397 — canary: carve the observability domain (event schema + prose-judge
  diagnostics + logs/stats) into `skills/ap2-observability/`, establish SKILL.md
  conventions, retarget the event-type drift gate.
- TB-398 — `skills/ap2-config/` (config knobs + config keys + backend setup) +
  retarget env-knob & config-key gates. `@blocked:review,TB-397`.
- TB-399 — `skills/ap2-board-ops/` (CLI-verb + MCP-tool references) + retarget
  those two gates. `@blocked:review,TB-397`.
- TB-400 — fold howto's task-agent contract + verification-bullet authoring into
  the existing `skills/ap2-task/` (mirror, keep `ideation.default.md`
  canonical). `@blocked:review,TB-397`.
- TB-401 — axis 3: cross-runtime deploy (`~/.agents/skills`) + `AGENTS.md` +
  managed discovery pointer (additive; howto target kept until retirement).
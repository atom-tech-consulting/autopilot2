# Ideation State

_Last updated: 2026-05-31T08:33:00Z by ideation cron_

## Mission alignment

Recent completes still serve the Mission (operator points ap2 at a goal
and walks away). The 5 most recent Completes considered — TB-352 (`ap2
logs --follow` live monitor), TB-351 (harden real-SDK smokes to skip on
transient errors), TB-350 (6h real-SDK smoke cron), TB-349 (fix stale
focus_advance refs post ideation-halt rename), TB-346 (config-correctness
cleanup) — are all tail work of the two NOW-SHIPPED foci (component
refactor, structured-config). The operator pivoted goal.md to a new Current focus at 2026-05-31T00:06Z (codex support via an
agent adaptor layer) and marked both prior foci shipped; this cycle
re-derives from scratch against that new focus per post-pivot convention.

## Current focus assessment

- **codex support through an agent adaptor layer** (7 axes: AgentAdapter
  ABC + ClaudeCodeAdapter / options+result normalization / MCP exposure /
  CodexAdapter / per-kind selection+auth / per-kind migrations / parity
  tests)
  - Progress so far: focus is fresh (set 00:06Z today); no Completes yet.
    Backlog already seeds the prerequisite axis and its two dependents —
    TB-353 (axis 1: AgentAdapter ABC + ClaudeCodeAdapter), TB-354 (axis 2:
    backend-neutral options + normalized AgentResult/usage, `@blocked:TB-353`),
    TB-355 (axis 3: MCP tools through the adapter, `@blocked:TB-353`).
  - Gaps: axes 4-7 unseeded — CodexAdapter (axis 4), per-agent-kind
    selection + backend-aware auth gate (axis 5), the per-dispatch-site
    migrations (axis 6, one TB each: ideation-scrub canary → prose-judge →
    validator/janitor-judge → run_task → _run_control_agent), and
    parity/smoke tests (axis 7). All four gate on TB-353's interface
    landing; authoring them now means guessing a contract that does not
    yet exist.
  - Status: `in-progress`

## Non-goal risk check

none. The focus explicitly excludes a third backend and per-message/in-task
routing; the seeded Backlog tasks (TB-353/354/355) are pure
relocate-behind-interface + normalization work with no behavior change,
clear of the multi-tenancy / cross-project / unconditional-automation
non-goals.

## Considered & deferred this cycle

- **Axis-4 CodexAdapter task**: deferred — depends on the AgentAdapter
  contract from TB-353 (axis 1), which is unstarted. A briefing authored
  now would pin verification bullets against a guessed interface shape;
  premature until TB-353 lands.
- **Axis-5 per-kind selection + auth gate / axis-6 migrations / axis-7
  parity tests**: deferred — all gate on axis 1 (and 4 for the Codex
  path) per goal.md's explicit sequencing (L193-196).
- **Operator-rejection pattern (recurring)**: the operator vetoes
  retry/patch-symptom remediations (TB-231 prose-judge retry; TB-227
  auto-retry on SDK timeout) and speculative false-positive-risk
  validators (TB-240 file-path-coherence; TB-172 shell-pitfall linter).
  Verifier reliability comes from better classification, not retries or
  enumerated linters. No such idea proposed this cycle.
- **Any greenfield idea**: declined — Backlog already holds 4 seeded
  items (TB-353/354/355 codex axes + TB-356 reliability) against a 1-slot
  budget; a 5th would pile onto an unstarted, partly-blocked queue rather
  than fill a gap.

## Cycle observations

- The prior cycle's ideation_state (06:29Z, after the 00:06Z goal pivot)
  assessed against stale focus items ("verification trustworthiness",
  "ideation quality") absent from current goal.md, and mis-labeled
  TB-353/354/355 as verification/ideation work when the board shows them
  as codex-adapter axis-1/2/3 tasks. Board is ground truth; this cycle
  discards that assessment and re-derives. Carry note for next cycle: the
  prior file's focus structure is void — do not inherit it.

## Decisions needed from operator

- None this cycle. No unadopted `cron_proposed` events in the recent
  events block; review-pending and queue-depth signals are surfaced
  mechanically by `ap2 status` / the cron status-report, not duplicated
  here.

## Proposals this cycle

Backlog already populated; no proposals this cycle.
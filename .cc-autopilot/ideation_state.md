# Ideation State

_Last updated: 2026-05-31T00:13Z by ideation cron_

## Mission alignment

The 5 most-recent Completes — TB-352 (`ap2 logs --follow` live event monitor,
8addc28), TB-351 (real-SDK smokes skip on transient errors, f460d43), TB-350
(6-hourly real-SDK smoke cron, ff1612c), TB-339 (drained
`_PENDING_MIGRATION_KNOBS` — structured-config final, 560bebd), TB-338
(12-factor cut-line CI gate) — serve goal.md's Mission (autonomous loop + the
structural prerequisites for a future OSS cut). The operator extended the
roadmap at 2026-05-31T00:06:21Z (operator_log `update_goal`): component-refactor
+ structured-config marked shipped, new focus opened — "codex support through an
agent adaptor layer". The new focus is the structural successor: a
backend-pluggable core widens the downstream OSS audience.

## Current focus assessment

goal.md now carries ONE active `## Current focus:` (codex support) plus two
`## Shipped focus:` blocks (component refactor 2026-05-27, structured config
2026-05-29).

- **Current focus: codex support through an agent adaptor layer**
  - Progress so far: none — focus opened 2026-05-31T00:06:21Z; zero Completes
    cite it; board fully drained (0A/0R/0B/0P/0F). The prior structural foci
    that make this cheap-now shipped: component refactor (TB-309→TB-320) and
    structured config (TB-321→TB-339), so the SDK coupling is concentrated and
    internal seams are cheap to introduce.
  - Gaps: all seven axes are greenfield. Axis 1 (AgentAdapter ABC +
    ClaudeCodeAdapter) is the hard prerequisite — nothing to build against until
    it lands. Axis 2 (options + result/usage normalization) and axis 3 (MCP
    tool exposure) land against axis-1's interface. Axes 4 (CodexAdapter), 5
    (per-kind selection + auth gate), 6 (per-kind migrations — ideation-scrub
    canary first), 7 (parity tests + codex smoke) sequence after.
  - Status: `in-progress`
  - Reasoning: focus has no Completes yet, so status MUST be in-progress; this
    cycle proposes the unblocking front (axes 1-3).

## Non-goal risk check

The adapter focus relocates dispatch behind an interface and adds a selectable
backend; goal.md L127-131 pins that it does NOT change prompts / tool policy /
verification semantics, add a third backend, or do per-message routing. The OAuth-only → per-backend-auth constraint reword is
operator-owned and already in goal.md Constraints.

## Considered & deferred this cycle

- **Axis 4 (CodexAdapter implementation)**: deferred — depends on axes 1-3
  settling the interface; a briefing now would be speculative (codex CLI prompt
  assembly / streaming / commit extraction can't cite concrete adapter symbols
  before axis 1 lands). Propose once TB-353 is concrete.
- **Axis 5 (per-kind selection + auth gate)**: deferred — the `[agent_backends]`
  table + backend-aware credential check build on the landed adapter.
- **Axis 6 migrations (ideation-scrub canary) + axis 7 parity tests**: deferred —
  migrations need axes 1-2 (and 3 for MCP-bearing sites); proposing before the
  interface exists risks stale verification bullets. Freshness favors proposing
  them next cycle against real landed symbols.
- **ap2-meta polish / config niceties**: not proposed — fails the focus
  delete-test and matches the operator's recurring veto cluster (TB-231
  symptom-patch; TB-240/TB-185/TB-184 parallel-surface / not-focus-aligned;
  TB-172 verifier whack-a-mole). This cycle's proposals are squarely the
  operator-authored axis charter, not meta-polish.
- **`#evaluation` grounding task**: not warranted — no greenfield proposal is
  blocked on missing measured data this cycle. Both insight files
  (validator-judge-timeout 2026-05-20, test-suite-slowness 2026-05-17) are
  <30d and bear on prior foci, not the adapter focus.

## Cycle observations

- The operator hand-authored TB-340→TB-352 (operator_log "goal-alignment check
  skipped") while ideation idled on ~14 `roadmap_complete` skips; with a fresh
  focus + empty board, ideation re-engages and re-derives from scratch against
  the new charter. The prior ideation_state.md was structured-config-era and is
  now superseded — this cycle discards rather than diffs it. (One bullet,
  justified: explains why no prior-state carry-over this cycle.)

## Decisions needed from operator

- None this cycle. The roadmap-rotation decision the prior cycle surfaced is
  RESOLVED (operator `update_goal` 2026-05-31T00:06:21Z opened the codex-adapter
  focus). No `cron_proposed` events pending; Frozen empty; no abandon/unfreeze
  recommendations.

## Proposals this cycle

TB-353 (axis 1: AgentAdapter ABC + ClaudeCodeAdapter — the prerequisite),
TB-354 (axis 2: backend-neutral options + normalized AgentResult/usage,
`@blocked:TB-353`), TB-355 (axis 3: MCP tool exposure through the adapter,
`@blocked:TB-353`). Axes 4-7 deferred to future cycles per Considered & deferred.
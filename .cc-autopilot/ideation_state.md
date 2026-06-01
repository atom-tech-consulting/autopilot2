# Ideation State

_Last updated: 2026-06-01T03:57:00Z by ideation cron_

## Mission alignment

Recent completes still serve the Mission (operator declares the goal once;
ap2 dispatches/verifies/recovers unattended). The 5 most recent Completes
considered — TB-365 (migrated the shared `_run_control_agent` off direct
`sdk.query` onto `select_adapter`+`adapter.run`; "last direct sdk.query
removed from daemon.py", cbcc137), TB-364 (`run_task` task-agent dispatch
routed through the adapter with the full MCP toolset via AgentTools,
18107a9), TB-363 (validator-judge + janitor-judge component judges routed
through the adapter, 54e278d), TB-362 (verifier prose-judge routed through
the adapter + TB-157 usage capture, 3678f21), TB-360 (axis-6 canary:
ideation-scrub migration, fc5db75). No mission drift: every recent ship
moves a dispatch concept behind the AgentAdapter seam — the literal axis-6
work goal.md calls for.

## Current focus assessment

- **codex support through an agent adaptor layer** (7 axes; axes 1-5+7
  shipped, axis 6 = per-kind dispatch-site migrations)
  - Progress so far: axes 1-5+7 SHIPPED (TB-353/354/355/357/358/359). Axis 6
    (per-kind dispatch-site migrations) has landed at: ideation-scrub canary
    (TB-360), verifier prose-judge (TB-362), validator-judge + janitor-judge
    (TB-363), `run_task` (TB-364), and the shared `_run_control_agent`
    (TB-365). TB-365's summary records "last direct sdk.query removed from
    daemon.py."
  - Gaps: (1) Progress signal "`claude_agent_sdk` imported only inside
    `ClaudeCodeAdapter`" (L205-206) is NOT yet met — the dispatch CALLS
    migrated but residual IMPORTS leak: `daemon.py:1927`
    (`import claude_agent_sdk as sdk` fed to `status_report.configure`),
    `daemon.py:2806` (`_import_sdk_or_die` availability gate), `tools.py:687`
    (`from claude_agent_sdk import tool` decorator), and
    `validator_judge/impl.py:510` (injected-sdk seam). The migrations
    preserved these to keep the injected-sdk hermetic-test seam
    (`_run_control_agent(cfg, sdk, mcp_server, ...)` signature unchanged per
    TB-365). (2) Progress signal "a mixed configuration (`ideation=claude`,
    `task=codex`) runs an agent of each kind end-to-end" (L209-211) has no
    test yet — correctly deferred last cycle pending the run_task +
    _run_control_agent migrations, which both landed today.
  - Status: `in-progress`
  - Reasoning: two named Progress signals (L205-206 import-consolidation,
    L209-211 mixed-config e2e) remain open with concrete, workable next steps.

## Non-goal risk check

none. Both proposals stay inside the focus: the mixed-config e2e test
exercises the adapter seam end-to-end (no new backend, no per-message
routing — respects L127-128), and the import-consolidation relocates
existing SDK coupling behind `ap2/adapters/` without changing any agent's
behavior on Claude (respects the "removing behavior during component
extraction" non-goal, L562-566).

## Considered & deferred this cycle

- **Make `_import_sdk_or_die` fully backend-aware** (a pure-`codex` config
  still hard-fails if `claude_agent_sdk` isn't installed): folded INTO the
  TB-366 import-consolidation scope rather than split out — it's the same
  `daemon.py` import being relocated behind the adapter / backend-aware gate
  (TB-358). A standalone task would fragment one coherent cleanup.
- **Abstract the tool-DEFINITION mechanism (`@tool` schema shape) for
  Codex**: deferred. TB-366 only relocates the `tool` import behind
  `ap2/adapters/` (re-export); genuinely abstracting tool-definition for a
  non-Claude backend is a larger follow-up with no caller yet (CodexAdapter
  tool-wiring from TB-357 already covers registration). Re-propose if/when
  the Codex tool-definition path needs it.
- **Operator-rejection pattern (recurring)**: vetoes cluster on (a)
  symptom-patch remediations without root-cause (TB-231) and (b) speculative
  enumerated-case validators guarding unobserved failures (TB-240, TB-172).
  Neither proposal is that shape — both close a literal goal.md Progress
  signal by moving/exercising the adapter seam. Pattern noted so future
  cycles keep clear.

## Cycle observations

- Axis 6's six dispatch-site migrations all landed within ~2h today
  (TB-362..TB-365 plus the TB-360 canary); two Progress signals remain
  (import-consolidation, mixed-config e2e).
- The validator-judge dep-coherence check flagged the e2e proposal's
  citation of the already-shipped TB-358 `[agent_backends]` surface as a
  hard predecessor; rephrasing to reference the surface as existing
  infrastructure (no completed-TB-N predecessor citations) cleared it. Worth
  remembering: citing a COMPLETE TB-N in a briefing's prose can still trip
  the hard-predecessor judge.

## Decisions needed from operator

- None this cycle. Frozen is empty (no retry-exhausted escalations); no
  unadopted `cron_proposed` events in the recent-events block.

## Proposals this cycle

- TB-366 — consolidate residual `claude_agent_sdk` imports behind
  `ap2/adapters/` + import-direction gate (Progress signal L205-206)
- TB-367 — mixed-config (`ideation=claude`, `task=codex`) end-to-end test
  (Progress signal L209-211)
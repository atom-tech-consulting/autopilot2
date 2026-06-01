# Ideation State

_Last updated: 2026-06-01T01:43:08Z by ideation cron_

## Mission alignment

Recent completes still serve the Mission (operator declares the goal once;
ap2 dispatches/verifies/recovers unattended). The 5 most recent Completes
considered — TB-361 (fixed the TB-356 thinking-block classifier + exempted
that class from the auto-approve breaker, 9baa2f5), TB-360 (axis-6 CANARY:
`_run_scrub` repointed through the `AgentAdapter` seam via `select_adapter`,
fc5db75 — genuinely complete; the 22:38 project-wide pytest fail was
unrelated drift-gate docstring noise, re-verified `complete` at 23:55),
TB-359 (axis 7: backend-parametrized adapter-contract parity suite, 45bfa60),
TB-358 (axis 5: per-kind `[agent_backends]` selection + backend-aware auth
gate, approved 21:26Z), TB-357 (axis 4: `CodexAdapter`, 866423a). No mission
drift: every recent ship moves a dispatch concept behind the adapter or
hardens the loop.

## Current focus assessment

- **codex support through an agent adaptor layer** (7 axes: AgentAdapter ABC +
  ClaudeCodeAdapter / options+result normalization / MCP exposure / CodexAdapter
  / per-kind selection+auth / per-kind migrations / parity tests)
  - Progress so far: axes 1-5 + 7 SHIPPED — axis 1 ABC + `ClaudeCodeAdapter`
    (TB-353), axis 2 normalized `AgentOptions`/`AgentResult`/usage (TB-354),
    axis 3 MCP tool registration through the adapter (TB-355), axis 4
    `CodexAdapter` (TB-357), axis 5 per-kind `[agent_backends]` selection +
    backend-aware auth gate (TB-358), axis 7 parity suite + gated codex smoke
    (TB-359). Axis 6 (per-kind dispatch-site migrations) is now UNDERWAY: the
    `ideation_scrub` canary (TB-360) shipped, validating the per-site repoint
    shape — `select_adapter(kind, cfg)` + `adapter.run_to_result(...)`,
    preserving Claude behavior bit-for-bit. `AGENT_KINDS`
    (`ap2/adapters/select.py` L44-54) enumerates the 9 selectable kinds; 5 of 9
    are migrated/native (ideation_scrub via TB-360; the adapter is the native
    path for none-yet of the live dispatch sites otherwise).
  - Gaps: 4 dispatch sites remain on direct `sdk.query` per goal.md's axis-6
    migration order (L177-183): the verifier prose-judge
    (`verify._judge_prose_bullet`, `sdk.query` at verify.py:611), the
    validator-judge + janitor-judge component calls
    (`components/validator_judge/impl.py:796`, `components/janitor/impl.py:796`),
    `run_task` (daemon.py:216), and the shared `_run_control_agent`
    (daemon.py:1139, unlocking ideation/status_report/cron/mattermost). Each is
    its own TB. The `claude_agent_sdk`-imported-only-inside-`ClaudeCodeAdapter`
    Progress signal (goal.md L205-206) stays open until all four land. The
    mixed-config end-to-end Progress signal (L211-213) is gated on `run_task` +
    `_run_control_agent` migrating first.
  - Status: `in-progress`
  - Reasoning: 4 named dispatch sites remain un-migrated; the canary cleared
    the validation gate so the tail is now workable.

## Non-goal risk check

none. The seeded wave stays inside the focus: each task repoints ONE dispatch
site behind the existing adapter seam, preserving the site's exact tool policy
+ behavior on Claude (respects the "removing behavior during extraction"
non-goal, goal.md L562-566). No third backend, no per-message routing
(L127-128). No drift toward multi-tenancy / cross-project / unconditional
automation.

## Considered & deferred this cycle

- **Mixed-config end-to-end test** (`ideation=claude`, `task=codex` runs an
  agent of each kind end-to-end — goal.md Progress signal L211-213): deferred.
  It genuinely depends on `run_task` (TB-364) AND `_run_control_agent` (TB-365)
  being adapter-routed first — you can't run `task=codex` end-to-end until
  `run_task` dispatches through the adapter. Seeding it now would stack it 5+
  deep on undispatched predecessors; re-propose once TB-364/TB-365 land.
- **Operator-rejection pattern (recurring)**: vetoes cluster on (a)
  symptom-patch remediations without root-cause diagnosis (TB-231) and (b)
  speculative enumerated-case validators guarding unobserved failures (TB-240,
  TB-172). This cycle's 4 proposals are neither shape — they're the literal
  remaining design axes from goal.md L177-183, each repointing an existing
  dispatch site (no new speculative gate, no symptom-patch). Noting the pattern
  persists so future cycles keep steering clear.
- **Stale `test-suite-slowness-2026-05-17.md` insight** (no tldr in the index):
  a data-quality gap (malformed front matter), but unrelated to the codex
  focus and the operator has historically rejected off-focus utility work
  (TB-185) — not worth a slot.

## Cycle observations

- Axis 6 is the focus's final stretch: the 4 seeded migrations (TB-362..TB-365) remain, followed by the deferred mixed-config e2e test.

## Decisions needed from operator

- None this cycle. Frozen is empty (no retry-exhausted escalations); no
  unadopted `cron_proposed` events in the recent-events block; TB-360's 22:38
  project-wide pytest failure self-resolved (re-verified `complete` at 23:55,
  drift-gate docstring noise, not a code fault) so no remediation is owed.

## Proposals this cycle

- TB-362 — axis-6 migration: verifier prose-judge (`verify._judge_prose_bullet`)
- TB-363 — axis-6 migration: validator-judge + janitor-judge component calls
- TB-364 — axis-6 migration: `run_task` (task agents)
- TB-365 — axis-6 migration: shared `_run_control_agent`
  (ideation/status_report/cron/mattermost)
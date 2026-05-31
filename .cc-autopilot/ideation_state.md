# Ideation State

_Last updated: 2026-05-31T13:00:20Z by ideation cron_

## Mission alignment

Recent completes still serve the Mission (operator points ap2 at a goal and
walks away). The 5 most recent Completes considered — TB-356 (graceful effort
step-down on the thinking-block-400 failure class), TB-354 (axis 2:
backend-neutral `AgentOptions` + normalized `AgentUsage`), TB-355 (axis 3: MCP
tools through the adapter via `build_tool_server`), TB-353 (axis 1:
`AgentAdapter` ABC + `ClaudeCodeAdapter`), TB-352 (`ap2 logs --follow`) — sit on
or adjacent to the current codex-adapter focus. **Board-state correction:** last
cycle's state file (10:52Z) listed TB-357-360 under "Proposals this cycle", but
`next_task_id` is still TB-357 and the Backlog is empty — those `board_edit`
calls never landed. The axis 4-7 wave is therefore unseeded; this cycle
re-seeds it with briefings grounded against the now-readable `ap2/adapters/`.

## Current focus assessment

- **codex support through an agent adaptor layer** (7 axes: AgentAdapter ABC +
  ClaudeCodeAdapter / options+result normalization / MCP exposure / CodexAdapter
  / per-kind selection+auth / per-kind migrations / parity tests)
  - Progress so far: axes 1-3 SHIPPED. Axis 1 — `AgentAdapter` ABC +
    `ClaudeCodeAdapter` wrapping `sdk.query` bit-for-bit (TB-353,
    `ap2/adapters/base.py` + `claude_code.py`, ff24b33). Axis 2 — canonical
    `AgentOptions` + normalized `AgentUsage`
    (`event_payload`/`from_event`/`combined_tokens`) read by cost guards /
    `task_run_usage` / `ap2 status` (TB-354, 20b8cc4). Axis 3 —
    `build_tool_server` + `registered_tool_names` so tools register through the
    adapter; contract test pins the 14-tool set (TB-355, 6a33b3c).
  - Gaps: axis 4 (`CodexAdapter` in `ap2/adapters/codex.py` — implement the
    interface against the `codex` CLI: prompt assembly, tool wiring, streaming,
    result/commit extraction, timeout/turn bounding) unseeded; axis 5
    (`[agent_backends]` map + `AP2_AGENT_BACKEND_<KIND>` overrides +
    backend-aware daemon-start auth gate extending `_require_oauth_token` in
    `ap2/cli_daemon.py`) unseeded; axis 6 migrations (one TB per dispatch site;
    canary = ideation-scrub `_run_scrub` at `ap2/ideation_scrub.py:296`, then
    verifier-judge, validator/janitor-judge, run_task, `_run_control_agent`)
    unstarted; axis 7 (adapter-contract parity suite both adapters satisfy +
    codex real-SDK smoke on the 6h `real-sdk-smoke` cron, TB-350) unseeded.
  - Status: `in-progress`
  - Reasoning: axes 1-3 have Complete TB-Ns; axes 4-7 have concrete, non-trivial
    next steps left unseeded after last cycle's wave failed to land.

## Non-goal risk check

none. Proposals stay inside the focus: axis 4 adds the second backend the focus
explicitly scopes (no third backend, no per-message routing); axis 5 is per-kind
selection fixed at dispatch (goal.md L127-131); axis 6 canary preserves Claude
behavior bit-for-bit (respects the "removing behavior during extraction"
non-goal); axis 7 is pure test coverage. No drift toward multi-tenancy /
cross-project / unconditional-automation.

## Considered & deferred this cycle

- **Axis-6 migrations beyond the canary** (verifier prose-judge, validator-judge
  + janitor-judge, run_task, `_run_control_agent`): deferred. The canary
  (ideation-scrub) exists to prove the migration shape before the rest follow;
  seeding site #2 before the canary lands would pile a 4th-level-deep blocked
  task on an unvalidated pattern. (1 of 5 slots intentionally left unused.)
- **TB-356 reliability follow-ups** (extend effort step-down to other failure
  classes): deferred — off the codex focus; would fail the goal-anchor gate.
- **Operator-rejection pattern (recurring)**: operator vetoes retry/symptom-patch
  remediations (TB-231, TB-227) and speculative false-positive-risk validators
  (TB-240 file-path-coherence, TB-172 shell-pitfall linter). None of this cycle's
  four proposals are retries, symptom-patches, or enumerated-case linters — they
  are net-new adapter implementation, config+auth surface, a canary migration,
  and contract tests.

## Cycle observations

- Last cycle's proposals (TB-357-360) were assessed + written into the state file
  but the `board_edit` add_backlog calls never landed (next_task_id still TB-357,
  Backlog empty). This cycle re-issues the same axis-4-7 wave with briefings
  grounded against the now-readable `ap2/adapters/` interface. Carried because it
  explains why an apparently-"proposed" wave is being re-proposed this cycle.

## Decisions needed from operator

- None this cycle. No unadopted `cron_proposed` events in the recent-events
  block. (The 12:26Z `auto_approve_paused:task_error` attention event is an
  auto-approve pause surfaced mechanically by `ap2 status` / the attention
  surface, not an ideation decision.)

## Proposals this cycle

Four proposals re-seeding axes 4-7 against the landed adapter interface:
- TB-357 — Axis 4: `CodexAdapter` (unblocked; root of the wave).
- TB-358 — Axis 5: per-agent-kind backend selection + backend-aware auth gate
  (`@blocked:TB-357`).
- TB-359 — Axis 7: adapter-contract parity suite + codex real-SDK smoke
  (`@blocked:TB-357`).
- TB-360 — Axis 6 canary: migrate ideation-scrub (`_run_scrub`) to
  adapter-routing + per-kind selection (`@blocked:TB-358`).
(1 slot of 5 left unused; remaining axis-6 migrations deferred until the canary
proves the shape.)
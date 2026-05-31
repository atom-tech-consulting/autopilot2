# Ideation State

_Last updated: 2026-05-31T02:20Z by ideation cron_

## Mission alignment

The 5 most-recent Completes — TB-352 (`ap2 logs --follow` live event-monitor,
8addc28), TB-351 (real-SDK smokes skip on transient SDK errors, f460d43),
TB-350 (6-hourly real-SDK smoke cron, ff1612c), TB-349 (fix stale
focus_advance module/symbol refs after the ideation-halt rename), TB-348
(purge hardcoded-3 proposal caps + rotation-era refs from the ideation prompt)
— still serve goal.md's Mission. TB-350/351 harden the real-SDK smoke path
that the codex focus's axis-7 parity smoke will extend; TB-348/349 close the
ideation-halt rename tail. The active focus (codex support via agent-adaptor
layer) opened 2026-05-31T00:06:21Z; its axes 1-3 were proposed last cycle
(TB-353/354/355, ideation_complete 00:19Z) and remain the workable front.

## Current focus assessment

goal.md carries ONE active `## Current focus:` (codex support) plus two
`## Shipped focus:` blocks (component refactor 2026-05-27, structured config
2026-05-29).

- **Current focus: codex support through an agent adaptor layer**
  - Progress so far: none landed — focus opened 00:06Z; zero Completes cite
    it. Axes 1-3 are queued: TB-353 (axis-1, AgentAdapter ABC +
    ClaudeCodeAdapter — the prerequisite), TB-354 (axis-2, backend-neutral
    options + normalized AgentResult/usage, `@blocked:TB-353`), TB-355
    (axis-3, MCP tool exposure through the adapter, `@blocked:TB-353`) per
    ideation_complete 00:19Z. TB-353 was auto-approved + dispatched but hit
    `task_error` at 00:25:33Z (ResultMessage subtype=success, output_tokens
    =32000 — the agent's final message hit the per-message output cap at turn
    42); no commit landed (`git log --grep TB-353` empty).
  - Gaps: axis-1 (TB-353) has NOT landed, so TB-354/355 stay blocked and
    axes 4-7 stay un-proposable (no concrete adapter symbols to cite yet).
    The daemon is stuck: auto-approve paused at 00:26Z (attention_raised
    `auto_approve_paused:task_error`); 600+ `auto_approve_skipped
    reason=task_error` ticks through 02:20Z — nothing dispatches until the
    operator resumes the window.
  - Status: `in-progress`

## Non-goal risk check

none — the three queued axes relocate dispatch behind an interface and add a
selectable backend; goal.md L127-131 pins no prompt / tool-policy /
verification-semantics change, no third backend, no per-message routing. No
new proposals this cycle, so no fresh drift surface.

## Considered & deferred this cycle

- **New axis-4-7 proposals (CodexAdapter, per-kind selection + auth gate,
  per-kind migrations, parity smokes)**: deferred — each depends on TB-353's
  interface landing; with axis-1 errored and uncommitted, briefings now would
  cite non-existent adapter symbols → stale verification bullets. Same
  reasoning as prior cycle; freshness favors proposing against real landed
  symbols once TB-353 commits.
- **Output-cap remediation for TB-353 (e.g. an infra patch to bound agent
  output / force Edit-not-inline file writes)**: NOT proposed — it is a
  symptom-patch on a single infra `task_error`, fails the codex-focus
  delete-test, and matches the operator's recurring veto cluster (TB-231
  retry symptom-patch; TB-172 validator whack-a-mole). One error is not a
  recurrence; the right move is operator inspect + resume (surfaced below).
- **Operator-rejection pattern**: recent vetoes (TB-231, TB-240, TB-185,
  TB-184, TB-172) cluster on symptom-patches-without-root-cause, parallel
  operator-intent surfaces, and verifier/validator whack-a-mole.

## Cycle observations

- TB-353's failure is an output-token-cap hit (output_tokens=32000 on the
  final message), NOT a turns/timeout cap — TB-347 already raised those to
  500 turns / 3600s. If TB-353 re-errors identically on resume, axis-1 may be
  genuinely too large for one agent run and warrant a split (define-ABC vs
  move-the-sdk.query-path). One error is not yet a pattern; flagged so next
  cycle can promote to a split proposal if it recurs.

## Decisions needed from operator

- Decision needed: TB-353 (the axis-1 prerequisite) hit a `task_error`
  (output-cap, no commit) and the auto-approve window has been paused since
  00:26Z — the daemon has emitted 600+ `auto_approve_skipped` ticks and will
  dispatch NOTHING until you act. Inspect via `ap2 logs`, then `ap2 ack
  auto_approve_window_resume` to let TB-353 re-attempt (treat as transient,
  matching the 2026-05-29T14:04 resume); if it re-errors identically, the
  unblock instead is to split axis-1 into two narrower TBs. Until resumed,
  axes 1-3 cannot progress and axes 4-7 cannot be proposed — the entire focus
  is gated on this single action.

## Proposals this cycle

Backlog already populated; no proposals this cycle. The 3 axis-1-3 tasks
(TB-353/354/355) queued last cycle remain the workable front (N=2 slots; ≥2
in-charter items already queued), and axes 4-7 are deferred until TB-353's
interface lands. Progress is gated on the operator resuming the paused
auto-approve window (see Decisions needed from operator).
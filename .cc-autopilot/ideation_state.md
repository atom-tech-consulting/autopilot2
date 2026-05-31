# Ideation State

_Last updated: 2026-05-31T04:25Z by ideation cron_

## Mission alignment

The 5 most-recent Completes — TB-352 (`ap2 logs --follow` live event-monitor),
TB-351 (real-SDK smokes skip on transient SDK errors), TB-350 (6-hourly
real-SDK smoke cron), TB-349 (fix stale focus_advance refs post ideation-halt
rename), TB-348 (purge hardcoded-3 proposal caps + rotation-era refs from the
ideation prompt) — still serve goal.md's Mission. TB-350/351 harden the
real-SDK smoke path the codex focus's axis-7 parity smoke extends; TB-348/349
closed the ideation-halt rename tail. The active focus (codex support via
agent-adaptor layer) opened 2026-05-31T00:06:21Z; its axes 1-3 were proposed at
00:19Z (TB-353/354/355) and remain the workable front. No new Completes since
the 02:20Z cycle — the daemon has been idle (auto-approve paused) for ~4h.

## Current focus assessment

goal.md carries ONE active `## Current focus:` (codex support) plus two
`## Shipped focus:` blocks (component refactor 2026-05-27, structured config
2026-05-29).

- **Current focus: codex support through an agent adaptor layer**
  - Progress so far: none landed — focus opened 00:06Z; zero Completes cite it.
    `git_log_grep TB-353` and `TB-354` both return 0 commits (reconfirmed this
    cycle). Axes 1-3 are queued: TB-353 (axis-1, AgentAdapter ABC +
    ClaudeCodeAdapter — the prerequisite), TB-354 (axis-2, `@blocked:TB-353`),
    TB-355 (axis-3, `@blocked:TB-353`). TB-353 was auto-approved + dispatched
    but hit `task_error` at 00:25:33Z (output_tokens=32000 — final-message
    output cap at turn 42); no commit landed.
  - Gaps: axis-1 (TB-353) has NOT landed, so TB-354/355 stay blocked and axes
    4-7 stay un-proposable (no concrete adapter symbols to cite → briefings
    would carry stale verification bullets). The daemon remains stuck:
    auto-approve paused at 00:26Z (`auto_approve_paused:task_error`);
    operator_log shows NO resume since (last entry 00:06Z update_goal) — ~4h of
    `auto_approve_skipped` ticks, nothing dispatches until the operator resumes.
  - Status: `in-progress`

## Non-goal risk check

none — the three queued axes relocate dispatch behind an interface and add a
selectable backend; goal.md L127-131 pins no prompt / tool-policy /
verification-semantics change, no third backend, no per-message routing. No new
proposals this cycle, so no fresh drift surface.

## Considered & deferred this cycle

- **New axis-4-7 proposals (CodexAdapter, per-kind selection + auth gate,
  agent-kind migrations, parity smokes)**: deferred — each depends on TB-353's
  interface landing; with axis-1 errored and uncommitted, briefings would cite
  non-existent adapter symbols → stale verification bullets. Freshness favors
  proposing against real landed symbols once TB-353 commits. Same reasoning as
  the 02:20Z cycle.
- **Split TB-353 into define-ABC vs move-the-sdk.query-path**: deferred —
  TB-353 has hit the output-cap error exactly ONCE and has NOT re-attempted
  (window paused), so there is no recurrence yet to justify a split. If it
  re-errors identically after the operator resumes, next cycle should propose
  the split. (Promoted here from last cycle's Cycle observations.)
- **Output-cap remediation for TB-353 (bound agent output / force
  Edit-not-inline writes)**: NOT proposed — symptom-patch on a single infra
  `task_error`, fails the codex-focus delete-test, and matches the operator's
  recurring veto cluster (TB-231 retry symptom-patch; TB-172 validator
  whack-a-mole). Right move is operator inspect + resume.
- **Operator-rejection pattern**: recent vetoes (TB-231, TB-240) cluster on
  symptom-patches-without-root-cause and verifier/validator whack-a-mole —
  reinforces deferring the two TB-353 remediation shapes above.

## Cycle observations

(None carried. Last cycle's lone observation — TB-353's output-cap hit being
a candidate split trigger — is promoted to `## Considered & deferred` as the
split-deferral, per triage discipline; nothing else is still informing
reasoning without a better home.)

## Decisions needed from operator

- Decision needed: the auto-approve window has been paused since 00:26Z
  (`auto_approve_paused:task_error` from TB-353's output-cap error) and
  operator_log shows no resume — the daemon has emitted ~4h of
  `auto_approve_skipped` ticks and will dispatch NOTHING until you act. Inspect
  via `ap2 logs`, then `ap2 ack auto_approve_window_resume` to let TB-353
  re-attempt (treat as transient, matching the 2026-05-29T14:04 resume). If
  TB-353 then re-errors identically on the output cap, the unblock instead is to
  split axis-1 into two narrower TBs (define-ABC vs move-the-sdk.query-path).
  (Carried from 02:20Z, re-articulated: pause duration now ~4h; no-commit + no-resume both
  reconfirmed this cycle via `git_log_grep` + operator_log.)

## Proposals this cycle

Backlog already populated; no proposals this cycle. The 3 axis-1-3 tasks
(TB-353/354/355) queued at 00:19Z remain the workable front (N=2 slots); axes
4-7 are deferred until TB-353's interface lands. Progress is gated on the
operator resuming the paused auto-approve window (see Decisions needed from
operator).
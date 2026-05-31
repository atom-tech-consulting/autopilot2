# Ideation State

_Last updated: 2026-05-31T06:29:00Z by ideation cron_

## Mission alignment

Recent completes still serve goal.md's Mission (trustworthy unattended
code-shipping). The five most recent Completes considered: TB-352
(briefing-validator regex hardening for fenced shell bullets), TB-350
(status-report dedup of pending-review TB-Ns), TB-349 (ideation_state
triage discipline for Cycle observations), TB-348 (howto worked example
for the `Prose:` prefix), TB-346 (verification-bullet classifier
`malformed` surfacing). All are verification-quality or ideation-quality
work — squarely on-mission, no drift.

## Current focus assessment

- **Verification trustworthiness: every gate the daemon enforces must be
  auto-verifiable and judge-legible**
  - Progress so far: TB-352 hardened the fenced-shell-bullet regex in
    `_validate_briefing_structure`; TB-346 surfaced `malformed`
    classifier verdicts; TB-219 the `Prose:` prefix override.
  - Gaps: (1) no e2e test that a `malformed` verdict actually blocks
    queue-append; (2) `ap2 check` doesn't warn on absence-check bullets
    missing the `!` prefix; (3) validator-judge timeout count isn't in
    the status-report. All three are already queued as TB-353, TB-355,
    TB-356 (review-pending).
  - Status: `in-progress`

- **Ideation quality: proposals goal-anchored, deduped, evidence-cited**
  - Progress so far: TB-349 added triage discipline to Cycle
    observations; TB-350 deduped pending-review TB-Ns from the
    status-report; TB-191 split operator-facing vs agent-internal.
  - Gaps: doc-coherence between the ideation prompt and howto
    shell-bullet pitfalls — already queued as TB-354 (review-pending).
  - Status: `in-progress`

## Non-goal risk check

none — all queued work is verification/ideation infrastructure; nothing
touches strategy, multi-repo, or UI non-goals.

## Considered & deferred this cycle

- **End-to-end malformed-gate test**: last cycle's deferred #1 — now
  COVERED by TB-353 in Backlog; deferral→proposal pipeline worked.
- **Operator-rejection pattern (recurring)**: the operator has vetoed
  "retry / patch-symptom" proposals (TB-227 auto-retry on SDK timeout;
  TB-231 prose-judge retry on malformed JSON) and "too-clever,
  false-positive-risk" validator additions (TB-240 file-path-coherence
  check). Avoid proposing retry-based remediation or speculative
  validators; verifier reliability comes from better classification,
  not retries. No such idea proposed this cycle.
- **Any greenfield idea**: declined — backlog already holds 4 workable
  review-pending items against a 1-slot budget; new proposals would pile
  onto an unreviewed queue rather than fill a gap.

## Cycle observations

- Backlog holds 4 review-pending items (TB-353/354/355/356) covering
  every gap I'd otherwise rank; operator throughput is ~1 approval / 2
  days, so the queue drains slower than it would grow.

## Decisions needed from operator

- None this cycle. (The `auto_approve_paused` attention and the
  review-pending TB-Ns are both surfaced mechanically by `ap2 status` /
  the status-report; not duplicated here per TB-182.)

## Proposals this cycle

Backlog already populated; no proposals this cycle.
# Ideation State

_Last updated: 2026-05-16T22:21:48Z by ideation cron_

## Mission alignment

Cycle entry: board 0A/0R/0B/0P/114C/3F (prompt header
2026-05-16T22:21:48Z), proposal slots 5. **State change since prior
cycle**: at 21:41-21:59Z the operator drained the entire
pending-review queue — rejected TB-240 with a substantive reason
(operator_log.md L160-161: "high bar for letting agents 'fix'
verification — easy to slide into cheating") and approved
TB-241+TB-242, both of which auto-promoted and shipped (fc14fe3 +
6704ed52). The 6-cycle 0-completes stretch broke; the
exhaustion-criterion decision-needed entry is substantively resolved
(queue cleared, fresh completes shipped, this cycle gets to test the
"one more 0-gap cycle vs. fresh completes" half of the criterion).

Recent Completes considered (refreshed — two new completes since prior
cycle):

- TB-242 (`6704ed52`, 21:59:15Z) — axis-4 focus-pointer state in
  `ap2 status` text/JSON + web home (active focus title + N-of-M
  position + roadmap-complete halt with ack hint).
- TB-241 (`fc14fe3`, 21:50:26Z) — dry-run readiness in `ap2 status`
  text + web home automation card (would-approve/would-unfreeze
  24h counts + dry-run badge).
- TB-239 (`ccfcff1`, 07:01:39Z) — axis-2 doctor floor.
- TB-238 (`d861d83`, 06:39:03Z) — automation_status collector +
  status-report digest extended with dry-run readiness.
- TB-237 (`b2fb6b1`, 06:29:37Z) — axis-4 e2e walk-away test.

## Current focus assessment

- **Current focus: end-to-end automation (goal.md L38-151, four axes)**
  - Progress so far:
    - Axis 1 (manual-approval): TB-223 + TB-224 + TB-232 + TB-234 +
      TB-241 (dry-run readiness surface).
    - Axis 2 (failure-recovery): TB-225 + TB-229 + TB-233 + TB-239.
    - Axis 3 (cost/blast-radius): TB-224 + TB-227 + TB-228 + TB-234.
    - Axis 4 (multi-focus): TB-226 + TB-237 + TB-242 (status/web
      surface for active focus + halt).
    - Cross-axis e2e: TB-230 (axes 1+2) + TB-237 (axis 4) + TB-238
      (dry-run readiness in collector + cron digest).
    - Adjacent gates: TB-235 (briefing dependency-coherence judge,
      fail-open) + TB-236 (prose-judge tighten).
  - Gaps (refreshed against fresh completes — two new gaps surfaced
    by TB-241 + TB-242 shipping the *pull-surface* halves but leaving
    the *push-surface* halves uncovered, plus one quiet-degradation
    hazard adjacent to TB-235):
    (1) **Axis-4 push surface** — TB-242 surfaced `roadmap_complete`
        in `ap2 status` + web home (pull-only). The cron status-report
        digest (TB-228 / TB-238) covers axes 1+2+3 but NOT axis 4:
        `roadmap_complete` and `focus_advanced` are absent from
        `_STATUS_REPORT_AUTOMATION_INTERESTING_TYPES`
        (status_report.py:426-432) AND from
        `render_automation_loop_activity_section`
        (status_report.py:137). When the daemon halts on
        roadmap-exhaustion at 03:00Z, the operator's only push channel
        (cron post) carries no axis-4 line — walk-away time on the
        rotation-halt signal stays bounded by manual status checks.
    (2) **TB-235 fail-open quiet degradation hazard** — the
        dependency-coherence judge emits `validator_judge_fail` /
        `validator_judge_timeout` events when the SDK call errors
        (events.py:87), but no surface anywhere consumes them: not
        `automation_status.collect_auto_approve_state`
        (automation_status.py:331), not `ap2 status` (cli.py:106),
        not the web automation card (web.py:1584). If the judge
        starts silently timing out, the dep-coherence gate goes dark
        without operator awareness — directly weakens the
        auto-approve safety claim (goal.md L82-85: "upstream gates
        already make this safe in practice").
    (3) **Auto-unfreeze fix-shape coverage telemetry** — operator
        tunes `AP2_AUTO_UNFREEZE_FIX_SHAPES` blind. No view shows
        what fraction of recent Frozen tasks emitted a
        `BriefingFix:` shape, or what fraction of those shapes
        matched the currently-enabled allowlist. Deferred this
        cycle (sample size still tiny; revisit when Frozen
        accumulates enough fix-shape data).
  - Status: `in-progress`
  - Reasoning: Two fresh completes landed two-axis surface parity
    (TB-241 axis-1+3 dry-run, TB-242 axis-4 focus pointer). The
    pull-surface side is now consistent across all axes; the
    push-surface side (cron status-report digest) hasn't caught up
    on axis 4. Plus TB-235's fail-open hazard never had observability.
    Two narrow proposals close both gaps without scope creep.

## Non-goal risk check

None. Both proposals are observability extensions of existing
collectors/renderers — no goal.md mutation, no new agent-fix
mechanism (avoids the TB-240 "high bar for letting agents fix
verification" rejection shape), no cross-project orchestration.

## Considered & deferred this cycle

- **Auto-unfreeze fix-shape coverage view** — sample size too small
  to ground a meaningful "shape X covers N% of recent freezes"
  signal (3 in Frozen, classification not yet attempted on those).
  Revisit once 10+ Frozen with BriefingFix summaries accumulate.
- **`ap2 doctor` cross-axis "walk-away readiness" composite** —
  same defer as last cycle. Doctor would just re-render what the
  collector exposes; the gap is the underlying signals, not the
  aggregator. Surface the missing axis-4 + validator-judge signals
  first (proposals this cycle); composite later if value remains.
- **Mattermost push-on-halt (immediate post when `roadmap_complete`
  / `auto_approve_paused` fires)** — would close the
  detection-latency-bounded-by-2h-cron gap end-to-end, but the
  daemon currently has no outbound Mattermost helper
  (`ap2/mattermost.py` is inbound-only; agents post via the
  `mattermost_reply` MCP tool). Adding a daemon-direct push
  surface is a non-trivial new arch shape. Proposal #1 below
  (add roadmap_complete to digest) is the cheaper interim — gets
  axis-4 into the existing 2h push channel without new
  infrastructure. Revisit push-on-halt if 2h latency proves
  insufficient in practice.
- **Verify-time "diff exceeds briefing scope" judge** — same defer
  rationale as prior cycles; TB-235's upstream coherence check
  covers most of this surface. Reconsider if a fabricated-scope
  case actually bites.
- **TB-240-shape file-path-coherence checks** — explicitly rejected
  (operator_log.md L160-161). Operator's "high bar for letting
  agents fix verification" principle generalizes: don't propose
  more verification-fabrication detection without recurrence
  evidence.
- **Wack-a-mole shell-bullet linting (TB-172-shape)** — n=6 reject.
- **TB-175-shape ideation-quality aggregator** — n=6 reject.
- **TB-185-shape `ap2 frozen TB-N` triage** — n=6 reject.
- **TB-184-shape `--hint` forwarding** — n=6 reject.
- **TB-231-shape symptom-patch shapes** — n=3 reject.

## Cycle observations

- Operator drained the queue at 21:41-21:59Z (rejected TB-240,
  approved TB-241+TB-242). The 6-cycle 0-completes streak observation
  is now stale (broke at this cycle); dropping. The
  exhaustion-criterion decision-needed entry from prior cycles is
  substantively resolved by the queue-drain + fresh completes; not
  re-carrying. Net: zero carried bullets this cycle — first clean
  triage in several cycles.

## Decisions needed from operator

(none this cycle — prior cycles' exhaustion-criterion entry resolved
by the 21:41-21:59Z queue drain; no new operator-action items
surfaced by the two new gaps this cycle, which are scoped tightly
enough to land via the standard add_backlog + approve path.)

## Proposals this cycle

- TB-243 — Extend `automation_status` collector + `ap2 status`
  text/JSON + web home automation card with `validator_judge_fail`
  + `validator_judge_timeout` 24h counts (closes TB-235 fail-open
  quiet-degradation hazard).
- TB-244 — Extend status-report cron digest with axis-4 focus
  rotation activity (focus_advanced + roadmap_complete) and add
  `roadmap_complete` to `_STATUS_REPORT_AUTOMATION_INTERESTING_TYPES`
  (TB-228 / TB-238 surface-parity closure for axis 4 push channel).

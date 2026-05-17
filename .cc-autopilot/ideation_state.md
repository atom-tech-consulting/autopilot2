# Ideation State

_Last updated: 2026-05-17T02:29:30Z by ideation cron_

## Mission alignment

Cycle entry: board 0A/0R/1B/0P/116C/3F (TB-245 still pending review
from prior cycle, header 2026-05-17T02:29:30Z); proposal slots 4
(ceiling not target). **State change since prior cycle**: prior
cycle's TB-245 push-surface validator-judge digest proposal is
queued, not yet reconciled — operator has TB-245 in `ap2 status`
"review: 1 pending". TB-243 + TB-244 both shipped same-day last
cycle (`647b771` 23:59Z, `aa971f8` 00:09Z). Walk-away cadence stays
functional but the prior cycle's proposal is in-flight, so this
cycle stays narrow.

Recent Completes considered:

- TB-244 (`aa971f8`, 00:09:20Z) — extend status-report cron digest
  with axis-4 focus rotation (`focus_advanced` + `roadmap_complete`).
- TB-243 (`647b771`, 23:59:49Z) — validator-judge fail-open audit
  counts on `ap2 status` text/JSON + web home automation card.
- TB-242 (`6704ed52`, 21:59:15Z) — axis-4 focus-pointer state in
  `ap2 status` text/JSON + web home.
- TB-241 (`fc14fe3`, 21:50:26Z) — dry-run readiness in `ap2 status`
  text + web home automation card.
- TB-239 (`ccfcff1`, 07:01:39Z) — axis-2 doctor floor warn.

## Current focus assessment

- **Current focus: end-to-end automation (goal.md L38-151, four axes)**
  - Progress so far:
    - Axis 1 (manual-approval): TB-223 + TB-224 + TB-232 + TB-234 +
      TB-241 + TB-243 + TB-245-pending (validator-judge push-surface
      digest).
    - Axis 2 (failure-recovery): TB-225 + TB-229 + TB-233 + TB-239.
    - Axis 3 (cost/blast-radius): TB-224 + TB-227 + TB-228 + TB-234.
    - Axis 4 (multi-focus): TB-226 + TB-237 + TB-242 + TB-244.
    - Cross-axis e2e: TB-230 (axes 1+2) + TB-237 (axis 4) + TB-238
      (dry-run readiness in collector + cron digest).
    - Adjacent gates: TB-235 (dependency-coherence judge) + TB-236
      (prose-judge tighten).
  - Gaps (refreshed against fresh completes — primary new gap
    surfaced by walking the ideation skip-gate chain against axis-4
    halt state):
    (1) **Ideation cost discipline during `roadmap_complete` halt
        (axis-4 ideation gate gap)** — `_maybe_ideate`
        (`ap2/ideation.py:786-846`) honors the TB-174 focus-exhausted
        gate (L825-845, reads `ideation_state.md` self-report) but
        does NOT honor `goal.roadmap_exhausted(cfg)` — the
        daemon-side axis-4 halt that the dispatch path
        (`daemon.py:3946`) and auto-approve gate (`tools.py:1970`)
        both honor. Confirmed: `grep "roadmap_exhausted\|roadmap_complete"
        ap2/ideation.py` returns zero matches. Result: when the
        roadmap exhausts during walk-away, ideation keeps firing
        every cooldown window. Each firing burns one SDK call
        generating proposals that pile up as `@blocked:review`
        because dispatch + auto-approve refuse to advance them. The
        operator returns to a pile of speculative proposals against
        an already-exhausted roadmap — directly weakens goal.md
        L33-36 done-when bullet 3 ("stops proposing when the target
        project's `## Done when` criteria are all met"). Concrete
        cost: 60-min cooldown × 48h weekend = up to 48 wasted
        ideation SDK calls.
    (2) **Auto-unfreeze fix-shape coverage telemetry** — operator
        tunes `AP2_AUTO_UNFREEZE_FIX_SHAPES` blind. No view shows
        what fraction of recent Frozen tasks emitted a
        `BriefingFix:` shape that matched the allowlist. Deferred
        this cycle (Frozen count still 3; insufficient data to
        ground a useful threshold). Carry-forward unchanged.
    (3) **Doctor runtime-signal extension** — defer rationale
        unchanged (pull + push surfaces cover the same operator
        need without scope-creeping doctor into runtime-signal
        territory).
  - Status: `in-progress`
  - Reasoning: TB-245 still pending review limits this cycle's
    appetite; one narrow gap surfaces (axis-4 ideation gate) that
    is a clean TB-174-shape transplant and closes the
    walk-away-cost half of the roadmap_complete story without
    introducing new surfaces.

## Non-goal risk check

None. The proposal extends an existing skip-gate (TB-174 sibling),
uses the existing canonical predicate (`goal.roadmap_exhausted`),
emits the existing `ideation_skipped` event type, no goal.md
mutation, no new auto-action mechanism (doesn't trip the operator
"high bar for agents 'fixing' verification" principle from
operator_log L160-161).

## Considered & deferred this cycle

- **Doctor runtime warning when `roadmap_exhausted` returns True
  and `AP2_AUTO_APPROVE=1`** — TB-242 already surfaces the halt
  state on `ap2 status` (pull) and the cron digest carries
  `roadmap_complete` via TB-244 (push); doctor would re-render
  signals the existing surfaces already carry. Revisit if a
  walk-away weekend shows the operator wants pre-flight warning.
- **`would_auto_approve` / `would_auto_unfreeze` event types in
  `_STATUS_REPORT_AUTOMATION_INTERESTING_TYPES`** — current
  frozenset (status_report.py:548-557) lacks these; if a window
  contains only `would_*` events with no other interesting types,
  the report's skip gate would suppress the post and the dry-run
  digest never reaches Mattermost. Deferred this cycle because
  evidence-of-actual-skip is missing — operators typically have
  other automation activity in a 2h window. Revisit if a quiet
  walk-away weekend with DRY_RUN=1 actually surfaces a lost report.
- **Auto-pause ideation when auto-approve is `auto_approve_paused`
  (axis-3 regression-halt ideation gate)** — symmetric to the
  proposed roadmap_complete gate but for axis-3. Deferred because
  auto-approve halt is minutes-recoverable (operator runs `ap2
  ack`) vs roadmap_complete which is days-to-weeks; cost asymmetry
  doesn't justify the gate yet. Revisit if observed halt durations
  exceed one cooldown window.
- **Mattermost push-on-halt** — daemon has no outbound MM helper
  (`ap2/mattermost.py` is inbound-only); status-report 2h cadence
  is the interim push channel; revisit if latency proves insufficient.
- **Auto-unfreeze fix-shape coverage view** — still 3 Frozen, none
  with fix-shape data; revisit at 10+.
- **`ap2 doctor` cross-axis "walk-away readiness" composite** —
  same defer as prior cycles.
- **Verify-time "diff exceeds briefing scope" judge** — defer
  rationale unchanged.
- **TB-240-shape file-path-coherence checks** — rejected
  (operator_log L160-161 high-bar-for-agent-verification-fixes).
- **Wack-a-mole shell-bullet linting (TB-172-shape)** — n=6+ reject.
- **TB-175-shape ideation-quality aggregator** — n=6+ reject.
- **TB-185-shape `ap2 frozen TB-N` triage** — n=6+ reject.
- **TB-184-shape `--hint` forwarding** — n=6+ reject.
- **TB-231-shape symptom-patch shapes** — n=3+ reject.

## Cycle observations

- Three consecutive cycles of narrow surface-parity closures
  anchored to a fresh Complete have all approved + shipped same-day
  (TB-241+TB-242, TB-243+TB-244, TB-245-pending). This cycle
  proposes a fourth (TB-246) following the same shape: identify the
  one ideation/daemon surface that hasn't honored the most recently
  shipped axis-state (roadmap_complete), transplant the
  shipped-elsewhere skip-gate pattern (TB-174), pin with a
  byte-for-byte test mirror. Carry-forward retained because it's
  the load-bearing ranking heuristic for the current focus phase
  (cleanup over greenfield).

## Decisions needed from operator

(none this cycle — no actionable operator decisions surface; the
one proposal is scoped to land via the standard add_backlog +
approve path.)

## Proposals this cycle

- TB-246 — Add `roadmap_complete` skip gate to `_maybe_ideate`
  (TB-174 sibling for axis-4 walk-away halt); reuses
  `goal.roadmap_exhausted(cfg)` canonical predicate; emits
  `ideation_skipped reason=roadmap_complete`; force_ideate bypass
  parallel to TB-174; test module mirrors
  `test_maybe_ideate_skips_when_all_focus_exhausted`.

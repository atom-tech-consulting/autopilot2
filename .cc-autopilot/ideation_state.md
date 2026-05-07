# Ideation State

_Last updated: 2026-05-07T03:14:33Z by ideation cron_

## Mission alignment

~2h since prior cycle (01:14Z). Operator activity 01:36Z–01:57Z:
TB-189 verification_failed at 01:35Z (a classic TB-76 shell-bullet
trap — `python -c "from ap2.tools import IMPACT_VERDICTS; assert
IMPACT_VERDICTS == (advanced-goal, pro-forma, unclear)"` — hyphens
are invalid in Python identifiers, exit 1), retry at 01:36Z passed
cleanly (`a49763b`, 01:45Z). Operator appended an explicit ack at
01:57:58Z to TB-175: defer re-proposal ~3+ ideation cycles after
TB-188 lands. Mission alignment continues to strengthen — the
ideation-quality-signal-collection focus now has TWO shipped seams
(TB-188 records + TB-189 classify CLI), backfill (TB-195) and
events (TB-196) are pending review, and the only outstanding
decision-needed item from prior cycles is now operator-answered.

Latest 5 completes considered:

- TB-189 (`a49763b`, 01:45Z) — `ap2 classify TB-N --impact <verdict>`
  CLI + chat verb routed via operator_queue_append; writes
  operator_log.md audit line + `impact` block on per-proposal record
  (note: agent renamed flag from brief-titled `--delete-test` to
  `--impact` in flight)
- TB-188 (`93892da`, 01:04Z) — per-proposal records + `extract_goal_anchor`
  / `extract_why_now` helpers; outcome reconciliation on 4 terminal
  events
- TB-194 (`cb09e91`, 00:54Z) — operator-queue ideate Active-check
  deferred from append to drain time
- TB-193 (`01e2d81`, 00:20Z) — `update_goal` op + `ap2 update-goal`
  CLI; goal.md safely refreshable while daemon runs
- TB-192 (`f271953`, 23:45Z) — insights/_index.md regen rides along
  in `state: ideation` commit

## Current focus assessment

- **Ideation quality signal collection (goal.md L38-76)**
  - Progress so far: Two foundational seams now live — **TB-188**
    (`93892da`, 01:04Z) writes per-proposal records at
    `.cc-autopilot/ideation_proposals/<TB-N>.json` + reconciles
    outcomes on `task_complete` / `task_deleted` / drained `approve`
    / drained `reject`; **TB-189** (`a49763b`, 01:45Z) gives the
    operator a retrospective `--impact` verdict surface (CLI + chat,
    routed through operator-queue) that writes both the operator_log.md
    audit line and an `impact` block on the per-proposal record. The
    goal.md L57-65 dual-purpose substrate (near-term evaluation +
    long-term agent-context) now has live capture paths from both
    sides — daemon-written at proposal time and operator-written
    retrospectively.
  - Gaps:
    (1) Pending-review proposals (TB-195 backfill, TB-196 record
    events) addressing prior-cycle gaps (1)+(2). Not duplicated here
    per TB-182 / TB-191 — `ap2 status` and the cron status-report
    surface those mechanically every run.
    (2) **Track-record feedback into the ideation prompt header**
    (carries) — reading accumulated records (and the new classify
    verdict stream from TB-189) back into the prompt at proposal
    time. Premature: 0 records exist yet (`ideation_proposals/`
    holds only `.gitkeep`); 0 classify verdicts in operator_log.md.
    Wait on TB-195 backfill landing + 2-3 cycles of organic growth.
    (3) **Insight aggregator from records → `ideation_quality.md`**
    (TB-175-shape) — operator-acked deferral at 01:57:58Z; off-table
    for ~3+ ideation cycles after TB-188 lands.
  - Status: `in-progress`
  - Reasoning: focus has 2 shipped seams, 2 more pending review;
    the only outstanding decision-needed is now operator-answered;
    no new high-confidence gap surfaced this cycle that wouldn't
    compete with already-pending work or trip the rejection-pattern
    filter (n=4: parallel surface / doesn't generalize / off-focus).

## Non-goal risk check

None. No drift toward generic-task-scheduler, replace-operator-judgment,
multi-tenancy, real-time, or cross-project Non-goals.

## Considered & deferred this cycle

- **`cycle_context` snapshot block on per-proposal records** —
  capture the recent-rejections / records-visible context that was
  available to the ideator at proposal time. Speculative scope
  creep on TB-188's just-shipped foundation; revisit after 2-3
  cycles when actual record content reveals what's missing.
- **"Recent classify verdicts" prompt-header block** (TB-163 pattern,
  mirrored for the new `--impact` stream). Premature: 0 verdicts
  exist yet; wait until ≥3 verdicts accumulate in operator_log.md.
- **Bulk / interactive classify CLI** (`ap2 classify --next` walking
  the operator through unclassified TB-Ns one at a time). After
  TB-195 backfill lands, ~50 historical proposals will be unclassified
  and manual classification will be friction-heavy. Defer:
  parallel-surface-adjacent, and the operator hasn't yet expressed
  the bulk-classify pain — propose reactively if/when it surfaces.
- **TB-188 outcome edge cases on re-attempt scenario** (carries) —
  speculative without reading the impl; defer until records show
  inconsistency.
- **TB-175 re-prop** (carries) — operator-acked deferral at 01:57:58Z;
  off-table for ~3+ cycles.
- **`ap2 ideate --hint`** (TB-184), **`ap2 frozen`** (TB-185),
  **briefing-bullet linter** (TB-172): authoritative rejects;
  will not re-propose.

Rejection-pattern note (n=4, unchanged from prior cycle): rejections
cluster on "creates parallel surface OR doesn't generalize OR
off-focus." With 0 proposals this cycle, the filter isn't exercised
— but the deferred candidates above were filtered against it
(bulk-classify trips parallel-surface; cycle_context trips premature-
without-volume which is rejection-adjacent under doesn't-generalize).

## Cycle observations

- TB-189 first-attempt verifier failure (`0701a35`, 01:35Z) was a
  Python-shell-bullet trap distinct from the TB-188 lazily-created-
  artifact trap noted last cycle: a shell-fenced `python -c "..."`
  with hyphenated tokens treated as identifiers. Re-attempt passed
  without operator action. Carrying once: if a similar Python-shell
  bullet fails in the next 2 cycles, it's a class signal that briefing
  authors trust the shell to parse Python literally — worth a
  bullet-shape discussion despite TB-172's wack-a-mole rule (the
  lever is briefing-authoring quality, not validator coverage).

(Dropped from prior cycle: "Backfill heuristic for ideation-authored
vs operator-authored TB-Ns" — promoted into TB-195's design and
documented in its brief; off the cycle-observations table now.
"TB-188 first-attempt shell-bullet trap" — situation resolved via
re-attempt; the lazily-created-artifact pitfall didn't recur this
cycle and stays a one-off.)

## Decisions needed from operator

(none this cycle.)

## Proposals this cycle

0 proposals.

Backlog has TB-195 + TB-196 pending review (both addressing this
focus's top-two cited gaps from last cycle). Quality > slot-fill
this cycle because:

(a) Cycle-prior proposals haven't yet been reviewed — piling on
    risks fanning the operator's review queue without empirical
    signal that the foundation works.
(b) Remaining structural gaps (track-record-into-prompt, insight
    aggregator) are accumulation-blocked or operator-deferred.
(c) Speculative add-fields-to-TB-188 / TB-189 candidates risk
    scope creep before record + classify volume reveals which
    content actually proves useful.

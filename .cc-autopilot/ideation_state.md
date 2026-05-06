# Ideation State

_Last updated: 2026-05-06T20:46:19Z by ideation cron_

## Mission alignment

Operator pivoted goal.md `## Current focus` to "ideation quality
signal collection" at 18:07:11Z (TB-191's predecessor) and has been
actively repairing the surfaces ideation writes into ever since:
TB-190 (channel routing fix, 9a28f70 / 19:12Z) and TB-191
(ideation_state.md schema rewrite — `## Open questions for operator`
→ `## Decisions needed from operator` + new `## Cycle observations`
section + actionability schema, 2ca1f0e / 19:59Z) both landed
between cycles. THIS cycle is the first to write under TB-191's new
schema. The two ideation-authored proposals from the 18:37Z cycle
(TB-188 + TB-189) are still in Backlog blocked-on-review — no
ideation work has shipped TO the new focus yet.

Latest 5 completes considered:

- TB-191 (2ca1f0e, 19:59Z) — operator-driven ideation_state.md schema rewrite
- TB-190 (9a28f70, 19:12Z) — operator-driven status-report channel fix
- TB-187 (33effb4, 2026-05-06 16:33Z) — mixed-blocker pending-review surface
- TB-186 (4b9c553, 16:11Z) — slot-check / cooldown ordering fix
- TB-183 (6583b07, 05:42Z) — proposal-slots plumbing

None directly ship the focus; all three pre-pivot completes (TB-183/186/187)
fed the prompt header / surface that ideation now relies on, and TB-190/191
are operator-driven repairs to those surfaces.

## Current focus assessment

- **Ideation quality signal collection (goal.md L38-76)**
  - Progress so far: zero TB-Ns shipped FOR the focus item. Two
    proposals in flight: **TB-188** (per-proposal record at
    `.cc-autopilot/ideation_proposals/<TB-N>.json`, reconciled on
    terminal events) and **TB-189** (`ap2 classify TB-N
    --delete-test <verdict>` operator surface) — both queued
    18:55Z / 19:12Z prior cycle, both `@blocked:review`. Adjacent
    operator-driven work: TB-191 reshaped the schema future
    cycles write under (this is the first cycle under it);
    TB-190 fixed where the cron status-report posts, so signal
    that DOES accumulate reaches the operator's primary channel.
  - Gaps:
    (1) **No structured per-proposal record exists** — covered
    by TB-188 (Backlog, awaiting review).
    (2) **No retrospective delete-test verdict surface** —
    covered by TB-189 (Backlog, awaiting review, blocked on
    TB-188).
    (3) **Track-record feedback into ideation prompt header.**
    Once TB-188 + TB-189 produce volume, ideation should read
    its own track record at proposal time (a "Recent
    classifications" / "Last-cycle outcomes" prompt-header block,
    parallel to TB-163's "Recent operator rejections"). Carries
    over from prior cycle; still gated on signal volume.
    (4) **Insight aggregator from operator_log.md + records into
    `.cc-autopilot/insights/ideation_quality.md`** — TB-175
    framing, rejected 05:15Z (no reason). See Decisions needed
    below: gap exists but path may already be closed.
  - Status: `in-progress`
  - Reasoning: focus is fresh (~3h since pivot); foundational
    proposals are in flight pending operator review; gaps (3)+(4)
    are blocked on signal volume / operator clarification rather
    than on ideation throughput.

## Non-goal risk check

None. Both in-flight proposals (TB-188 + TB-189) anchor strictly
to ideation signal capture. No work this cycle drifts toward the
generic-task-scheduler, replace-operator-judgment-on-goals,
multi-tenancy, real-time, or cross-project Non-goals.

## Considered & deferred this cycle

- **Re-prop TB-175 framing (insight regenerator → `.cc-autopilot/
  insights/ideation_quality.md`).** 05:15Z reject (no reason).
  See Decisions needed below — operator clarification needed
  before re-proposing.
- **Track-record prompt-header block (gap 3).** Carries from
  prior cycle. Still depends on TB-188 + TB-189 producing
  classification volume; premature this cycle.
- **`ideation_proposal_recorded` event in events.jsonl on
  TB-188's record write.** Closes the loop with TB-169's
  IDEATION_RELEVANT_EVENT_TYPES allowlist so the record-write
  shows up in ideation's own prompt-header `_events_block`.
  Cleaner as an implementation note inside TB-188's PR (or an
  `ap2 update` to TB-188's briefing) than as a separate task —
  noted in Cycle observations.
- **Reject-reason enum / taxonomy (`ap2 reject --category`).**
  TB-172 wack-a-mole pattern still applies; free-text reasons
  + LLM mining at insight-regenerator time remains structurally
  cleaner.
- **Self-evaluation prompt section ("Recent ideation
  retrospectives").** Operator pivot 18:07Z explicitly names
  operator-authored verdict (TB-189) as the primary signal
  before any prompt-side retrospective field.
- **`ap2 ideate --hint`** (TB-184, rejected 18:01Z): authoritative
  reject — goal.md is the operator-intent channel. Will not
  re-propose.
- **`ap2 frozen TB-N`** (TB-185, rejected 17:57Z): authoritative
  reject — Frozen tasks are rare; consolidated triage isn't the
  bottleneck. Will not re-propose.
- **Web `/proposals` page over ideation_proposals/.** TB-188's
  own briefing puts "Operator CLI to inspect records — records
  are JSON; cat works" in `## Out of scope`. Premature to add UI
  before data flows.

Rejection-pattern note (n=4 in 24h): TB-172/175/184/185 all share
"creates parallel surface OR doesn't generalize." Both in-flight
proposals (TB-188 + TB-189) and every candidate considered above
respect that filter.

## Cycle observations

(Clean slate — TB-191 introduced this section; prior cycle
predates it.)

- First cycle under TB-191's schema; using triage discipline
  strictly so future cycles inherit a clean baseline rather
  than a creep-prone seed.
- TB-188 design has no events.jsonl emission; its record
  write is invisible to TB-169's IDEATION_RELEVANT_EVENT_TYPES
  pipeline. Cleanest fix is an in-PR addition by the implementing
  agent (`ideation_proposal_recorded` summary line) or an
  `ap2 update` to TB-188's briefing — flagged here so whichever
  picks up TB-188 sees it.
- `.cc-autopilot/insights/_index.md` still empty post-TB-89
  wiring; TB-188 records become the natural seed for the first
  insight file once classification volume builds (estimated
  1-2 cycles after both TB-188 and TB-189 land).
- Operator cadence today (TB-187 16:33Z, TB-190 18:55Z, TB-191
  19:12Z, plus two force-ideate triggers at 16:11Z and 18:37Z)
  shows hands-on bugfix mode — proposal slots NOT being burned
  by walk-away pressure; better to under-propose a goal-aligned
  cycle than to fill 3 slots with weaker candidates.

## Decisions needed from operator

- Decision needed: TB-175 re-proposal sequencing — ideation has
  the TB-175-shape insight regenerator
  (`.cc-autopilot/insights/ideation_quality.md` aggregator over
  operator_log.md + post-TB-188 records) sitting in considered-
  and-deferred since the 2026-05-06T05:15Z reject (no reason
  given). Should ideation (a) re-propose within 2-3 cycles of
  TB-188 landing once records accumulate, or (b) treat the
  05:15Z reject as definitive and stop considering the path?
  Operator action: edit goal.md "Current focus" body to mention
  insight aggregation as in-scope, OR queue a clarifying line
  via `ap2 reject` (or operator_log append) saying "closed:
  insight aggregation out of scope". Unblock-condition: the
  deferred slot stops compounding indefinitely — ideation
  either commits a future proposal slot to the work or drops
  it entirely from the candidate pool.

## Proposals this cycle

3 slots available; proposing 0.

Rationale:
- Backlog already holds the foundational signal-collection seam:
  TB-188 (record capture) + TB-189 (operator-authored delete-test
  verdict), both `@blocked:review`. Both anchor directly to the
  current focus.
- Any 3rd proposal anchored to the same focus would either
  anticipate TB-188 records (premature — no data to query yet)
  or tread already-rejected territory (TB-172/175/184/185
  pattern).
- No urgent failure-review: TB-190 (verification_failed 19:04Z
  → complete 19:12Z) and TB-191 (verification_failed 19:39Z +
  19:47Z → complete 19:59Z) both ultimately landed in Complete
  via briefing-bullet rewrites; no edit-briefing / split /
  follow-up / abandon classification applies.
- No unadopted `cron_proposed` events in the recent-events block.
- Operator is hands-on (4 operator-driven events in the last 4h);
  the discipline this cycle is to under-propose rather than fill
  slots with weaker candidates that risk the 05:15Z / 17:57Z /
  18:01Z rejection pattern.

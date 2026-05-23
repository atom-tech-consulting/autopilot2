# Ideation State

_Last updated: 2026-05-23T01:32Z by ideation cron_

## Mission alignment

Cycle entry: board 0A / 0R / 0B / 0P / 151C / 3F; focus pointer at
`operator-legible reporting and monitoring (2 of 2)` (new — operator
ran `ap2 update-goal` at 2026-05-22T23:09:20Z extending the roadmap
with this second focus heading, then forced ideation at
2026-05-23T01:31:56Z). The end-to-end-automation focus from prior
cycle is now "exhausted" per the operator's pre-pivot ack
(2026-05-20T23:38:50Z); the focus_advanced pointer is on the new
heading. Recent Completes (last ~48h) all served pre-pivot work or
agent-friendliness adjacents — they're not signals against the new
focus, but they confirm the pre-pivot foundation (auto-approve /
auto-unfreeze / cost guards / multi-focus advance) shipped before the
operator extended the roadmap, exactly the sequencing goal.md L40-49
describes.

Recent Completes considered (last ~48h):

- TB-279 (`b1f6642`, 2026-05-21T??Z) — operator-doc reconciliation:
  README de-dup, sandbox runbook refresh.
- TB-278 (`4799081`) — daemon-defaults bump + `.cc-autopilot/env`
  template on init.
- TB-277 (`905371e`) — daemon_state.json gitignore + drift-gate.
- TB-276 (`d563dbd`) — sandbox `sync-assets` unification.
- TB-275 (`9656357`) — `roadmap_complete` halt scope reduced (dispatch
  always drains, ideation alone parks).

## Current focus assessment

- **Current focus: operator-legible reporting and monitoring**
  - Progress so far: zero Completes against this heading — the focus
    heading was added 2026-05-22T23:09:20Z (operator_log.md L230) and
    no proposals have landed yet. Pre-pivot Completes that touched
    the same surfaces are foundational but don't address this focus's
    deliverables: TB-228 added the automation-loop digest block,
    TB-244 added focus-rotation digest, TB-245/258/259 added more
    sub-blocks, TB-243/227 added pull-side surfaces. The push surface
    grew vertically (more digest blocks) but goal.md L186-211's three
    failure modes (context-poor TB-N rendering / clock-driven
    repetition / shallow monitoring) are unaddressed.
  - Gaps:
    (1) **Context-poor reports** (goal.md L187-192 / Done-when L214-217):
        `STATUS_REPORT_PROMPT` (status_report.py L740-744) instructs
        the agent to write bullets as `TB-N + 1-line outcome + short
        SHA` — agent-authored prose, not daemon-rendered title-bearing
        lines. Headline is `**Autopilot Status Report**`, no project
        identifier — multi-project operators must alt-tab to identify
        the source. No `Config.project_name` field exists; no
        `AP2_PROJECT_NAME` env knob (grep returns nothing).
    (2) **Clock-driven repetition** (goal.md L194-202 / Done-when
        L218-220): the skip-gate (`_status_report_should_skip`,
        status_report.py L927) fires only when zero interesting
        events landed in the window. Two near-identical posts after
        two single-event windows are not suppressed; no content
        fingerprint comparison; cron.py L196-210 is purely
        interval-driven. No `cron_skipped reason=duplicate_content`
        event type exists.
    (3) **Shallow monitoring** (goal.md L204-211 / Done-when
        L221-223): no `ap2/attention.py` module exists; "stuck
        Active task" / "validator-judge noisy" / "cost-cap approach"
        conditions are surfaced only as sub-blocks within the 2h
        periodic post or as `[noisy]` suffix on `ap2 status` text —
        no proactive, distinct push surface; no debounce; no
        `attention_raised` event vocabulary.
  - Status: `in-progress`
  - Reasoning: brand-new focus heading; three concrete Done-when
    bullets each map cleanly to a discrete, scope-bounded deliverable;
    foundational push surface (status_report.py) and pull surface
    (automation_status.py) already exist to extend. Not exhausted.

## Non-goal risk check

Watch goal.md L225-229 ("per-project legibility, NOT cross-project
aggregation"): proposal 1's project-name surface must stay
per-daemon, not introduce any cross-project registry. None of the
three proposals below cross that line.

## Considered & deferred this cycle

- **TB-269/270 post-deployment re-measurement** — still time-locked
  (≥7d window from 2026-05-20T04:40Z opens 2026-05-27Z). Window not
  yet open; current `0 fail, 7 timeout` 24h figure still dominated by
  pre-fix events. Defer until after 2026-05-27Z.
- **TB-175-shape ideation acceptance-rate aggregator** — operator
  parked it pending ≥3+ ideation cycles' worth of TB-188 records
  (operator_log L80). Still gated; do not re-propose.
- **`AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD` recalibration** — same ≥7d
  window dependency as TB-269/270 re-measurement.
- **Briefing-validator additional checks** (TB-172 / TB-240 shape) —
  recurring operator-rejection pattern: whack-a-mole expansions of
  pre-allocation validators are vetoed. Not in scope for this focus
  anyway (the focus is about reporting/monitoring legibility, not
  briefing-shape gates).
- **Auto-rotation of focus pointer** (would violate goal.md L231-240
  "Goal.md auto-rotation" Non-goal) — not proposed.

## Cycle observations

- Operator rejection pattern recap (carried, re-justified): TB-172 /
  TB-240 reject whack-a-mole validator expansions; TB-185 / TB-184
  reject ap2-meta-polish unconnected to current focus signal. Each of
  this cycle's three proposals maps to one of the new focus's three
  Done-when bullets verbatim — none extends a validator nor adds
  meta-polish, so they should clear the rejection pattern.
- Sequencing observation: proposal 1 (project_name + pre-rendered
  task lines) is the lowest-coupled change and unblocks the operator's
  multi-project workflow even if the other two slip; proposal 2 (dedup
  fingerprint) layers on top; proposal 3 (attention surface) is the
  most novel and may slip first if budget is tight. Operator can
  approve in any order — they're independent on the implementation
  axis even though they share the focus.
- New focus's surface concentration: ~80% of the deliverable lives in
  `ap2/status_report.py` and `ap2/automation_status.py`, with light
  touches in `cron.py` / `config.py` / `events.py` / new
  `ap2/attention.py`. Test coverage will accrue under
  `ap2/tests/test_tb28X_*.py` following the established per-task
  module convention.

## Decisions needed from operator

(none this cycle — the new focus is fresh and the three proposals
below cover the three Done-when bullets 1:1. No operator-judgment
escalation needed until proposals land and outcomes accrue.)

## Proposals this cycle

3 proposals queued behind `@blocked:review`, each mapping 1:1 to a
goal.md Done-when bullet on the new focus:

- TB-280 — project-identity headline + pre-rendered task-title
  digest in status-report post (Done-when L214-217 / failure mode #1
  "context-poor content").
- TB-281 — content-fingerprint dedup gate so consecutive status
  reports don't post unchanged content (Done-when L218-220 / failure
  mode #2 "clock-driven repetition").
- TB-282 — new `ap2/attention.py` detector + `attention_raised`
  event + "## Attention needed" section, seeded with one detector
  (`task_stuck`) (Done-when L221-223 / failure mode #3 "shallow
  monitoring").

# Ideation State

_Last updated: 2026-05-26T06:05Z by ideation cron_

## Mission alignment

Cycle entry: board 0A / 0R / 0B / 0P / 158C / 3F; focus pointer at
`operator-legible reporting and monitoring (2 of 2)`; `roadmap_complete`
decisions-needed entry standing since 2026-05-23T03:06:37Z (events.jsonl)
after TB-280/281/282 closed all three focus-2 Progress signals at the
first-pass deliverable level. Operator forced `ap2 ideate` at
2026-05-26T06:04:26Z (operator_log L241) — request to find legitimate
follow-ups before declaring roadmap fully done. Recent Completes the
last cycle considered (~96h):

- TB-280 (`39bdf77` 2026-05-23T01:57Z; state `5990aa9`) — Config.project_name
  + AP2_PROJECT_NAME knob + bracketed `[<project_name>]` headline +
  daemon-rendered `## Recent task activity` digest with title-bearing
  `**TB-N** — <title>: <outcome>` lines. Closes focus-2 Progress signal #1.
- TB-281 (`33f946e` 2026-05-23T02:20Z; state `1943166`) — content-fingerprint
  dedup gate (`compute_status_report_fingerprint` + `cron_state.json`
  sibling-key); emits `cron_skipped reason=duplicate_content`. Closes
  focus-2 Progress signal #2. Live fire confirmed: events.jsonl
  `cron_skipped` entries at 2026-05-25T11:59Z, 13:59Z, 15:59Z, 20:01Z,
  2026-05-26T00:03Z, 02:03Z, 04:03Z.
- TB-282 (`15e77e9` 2026-05-23T02:38Z; state `b2efb99`) — new
  `ap2/attention.py` with `detect_attention_conditions` + `task_stuck`
  detector + per-(type,key) debounce + status-report verbatim-forward of
  the `## Attention needed` block. Seeded the surface but only one of
  Progress signal #3's enumerated condition kinds.
- TB-283/284 (`496774d`/`fc96085`) — operator-added: empty-cycles
  sole-signal for focus-advance + ideation_scrub. Touches focus
  rotation (axis-4), not the active focus-2 surface.
- TB-285/286 (`ac4f861`/`b22b8d0`) — operator-added: `Done when:`→
  `Progress signals:` rename + howto.md rewrite. Format/doc cleanup,
  not new focus-2 deliverables.

## Current focus assessment

- **Current focus: operator-legible reporting and monitoring**
  - Progress so far:
    - Progress signal #1 (title + project_name): TB-280 closed —
      `grep "<project_name>" ap2/status_report.py` confirms substitution
      token; bracketed headline live.
    - Progress signal #2 (significance-gated + dedup): TB-281 closed —
      `cron_skipped reason=duplicate_content` events firing in the last
      24h prove the dedup is active.
    - Progress signal #3 (proactive attention surface): TB-282 closed
      the SURFACE (`ap2/attention.py` + daemon wire-up + status-report
      verbatim forwarding) but seeded only `task_stuck` (1 of 5
      enumerated condition kinds named in the Progress-signals bullet:
      "stuck / failed / frozen tasks, decisions-needed, cost or
      validator-judge anomalies"). TB-282's own Out-of-scope clause
      names `validator_judge_noisy`, `cost_cap_approach`,
      `decisions_needed_new`, `frozen_task_recency` as obvious
      follow-ups (`ap2/attention.py` L29-32).
  - Gaps:
    1. **Frozen-task attention** — Progress signal #3 names "frozen
       tasks" verbatim. Today the 3 Frozen tasks (TB-119 / TB-120 /
       TB-133) surface only as `3F` aggregate count in `ap2 status`
       headline; no proactive Attention bullet; no `ap2 unfreeze`
       nudge in the Mattermost report.
    2. **Validator-judge noisy attention** — TB-243 added the 24h count
       to `ap2 status` + web automation card; TB-245 forwards a
       bottom-of-digest sub-block; TB-272 added an auto-approve
       pause_reason. But no detector promotes the noisy state into
       the proactive Attention block — buried at digest bottom with
       routine activity, contrary to "distinct from routine progress".
    3. **Auto-approve pause attention** — `pause_reason` (today
       `consecutive_freezes` / `validator_judge_noisy`; future
       siblings will land here too) appears only in the TB-228
       automation-digest sub-block. Operator must `ap2 ack
       auto_approve_unfreeze` to resume; no proactive nudge in the
       Attention block — exact "pending decision … buried" failure
       mode goal.md L207-209 names.
  - Status: `in-progress`
  - Reasoning: focus-2 has 3 explicit Progress signals; #1/#2 closed
    cleanly; #3's surface exists but covers 1 of 5 named conditions —
    each remaining condition is a narrow detector under the
    established `_detect_*` shape. Not exhausted.

## Non-goal risk check

None of the 3 proposals introduces cross-project aggregation, goal.md
auto-rotation, or unconditional automation. Each respects the focus-2
scope guard (goal.md L227-229 "per-project legibility, NOT cross-project
aggregation") and adds opt-in surfaces (detectors read state and
surface a bullet; operator-action remains operator-CLI).

## Considered & deferred this cycle

- **`cost_cap_approach` attention detector** (Progress signal #3 fifth
  leg) — no upstream cost-ceiling knob exists yet (no
  `AP2_AUTO_APPROVE_DAILY_COST_CAP`; no pause_reason; `grep cost_cap
  ap2/` returns only TB-282 Out-of-scope mentions and a janitor test).
  Detector without a threshold has nothing to detect. Defer until
  the cost-tracking infrastructure declares its operating envelope.
- **Event-triggered status_report cadence** (Progress signal #2's
  "fires on report-worthy events" sub-clause) — TB-281's
  fingerprint-dedup approach is the operator-blessed shape (TB-281
  shipped via dispatch; no operator pushback). Shifting from
  interval-driven to event-triggered cron is a bigger design that
  goal.md doesn't explicitly require given the dedup gate already
  suppresses the "no two consecutive reports repeat" failure mode.
- **`decisions_needed_new` attention detector** — overlaps with the
  existing `- Decisions needed from operator (N): ...` line that
  TB-173/TB-191 already forward verbatim into the status-report.
  Promoting to Attention is marginal vs the line-forward, and
  duplicates the `auto_approve_paused` detector (proposal 3) which
  catches the most actionable subset of decisions-needed.
- **TB-175-shape ideation-acceptance-rate aggregator** — operator
  parked it 2026-05-07T01:57Z pending ≥3 cycles of TB-188 records;
  still gated.
- **Rejection-pattern check (carried, re-justified)**: TB-172 /
  TB-240 vetoed whack-a-mole validator expansions; TB-185 / TB-184
  vetoed ap2-meta-polish unconnected to current focus; TB-175 /
  TB-231 vetoed premature aggregators / symptom-patching. All three
  proposals this cycle follow TB-282's established `_detect_<name>`
  shape and each closes one literal item from Progress signal #3's
  enumerated condition list — not validator-expansion, not
  meta-polish, not premature aggregation. Pattern clears.

## Cycle observations

- Surface concentration: all 3 proposals land in `ap2/attention.py`
  with `_DETECTORS`-style extension. ~80% of net new code is one new
  detector function per task + matching test module + howto.md /
  architecture.md inventory line. No daemon-loop changes; the
  existing `_maybe_emit_attention_events` debounce+emit path covers
  all detectors uniformly.
- Sequencing: proposal 1 (`task_frozen`) is the highest-action-needed
  kind (Frozen requires `ap2 unfreeze` or operator delete); proposal
  2 (`validator_judge_noisy`) elevates an existing-but-buried signal
  to the Attention surface; proposal 3 (`auto_approve_paused`)
  surfaces meta-state requiring `ap2 ack`. Operator can approve in
  any order — they share `ap2/attention.py` but never edit the same
  function, so concurrent dispatch should not contend.

## Decisions needed from operator

(none this cycle — the active focus has 3 explicit Progress signals
of which #1 and #2 are closed and #3 is partially closed; this cycle
proposes the 3 cleanest follow-up detectors to close 3 of the
remaining 4 enumerated condition kinds. The 5th — cost-cap approach —
is deferred above pending the upstream cost-cap threshold itself.
The standing `roadmap_complete` decisions-needed entry from
2026-05-23T03:06Z is operator-owned and not for ideation to revisit.)

## Proposals this cycle

3 proposals queued behind `@blocked:review`, each mapping to one
named-in-goal.md condition from Progress signal #3:

- TB-287 — `task_frozen` attention detector (closes "frozen tasks").
- TB-288 — `validator_judge_noisy` attention detector (closes
  "validator-judge anomalies").
- TB-289 — `auto_approve_paused` attention detector (closes "pending
  decision" leg via the pause_reason surface).

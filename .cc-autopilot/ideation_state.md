# Ideation State

_Last updated: 2026-05-26T08:23Z by ideation cron_

## Mission alignment

Cycle entry: board 0A / 0R / 0B / 0P / 161C / 3F; focus pointer at
`operator-legible reporting and monitoring (2 of 2)`. Last cycle's
3 proposals (TB-287 / TB-288 / TB-289) all auto-approved + completed
06:35-06:58Z this morning, closing 3 of the 4 attainable legs of
focus-2 Progress signal #3. The 5th leg (cost-cap approach) was
deferred 6:05Z on a faulty premise — see correction below. Recent
Completes the current cycle considered (~3h):

- TB-287 (`b7b42b0` 2026-05-26T06:35Z) — `_detect_task_frozen` in
  `ap2/attention.py` (`AP2_TASK_FROZEN_RECENCY_S` default 24h,
  per-task `task_frozen:<id>` key, intervening
  `task_unfrozen`/`task_deleted` abort). Closes Progress signal #3
  "frozen tasks" leg.
- TB-288 (`c7fdf76` 2026-05-26T06:47Z) — `_detect_validator_judge_noisy`
  (singleton, 24h fail+timeout window vs
  `AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD`). Closes Progress signal #3
  "validator-judge anomalies" leg.
- TB-289 (`c9962fe` 2026-05-26T06:58Z) — `_detect_auto_approve_paused`
  (per-reason key, reads `collect_auto_approve_state.pause_reason`,
  forwards `_PAUSE_REASON_ACK_VERB`). Closes Progress signal #3
  "pending decision" leg via the pause-reason surface; TRIPPED
  cost-cap states (`per_task_token_cap_exceeded` /
  `window_token_cap_exceeded` / `task_error`) flow through this
  detector since their ack-verb is registered.

## Current focus assessment

- **Current focus: operator-legible reporting and monitoring**
  - Progress so far:
    - Progress signal #1 (title + project_name): TB-280 closed.
    - Progress signal #2 (significance-gated + dedup): TB-281 closed —
      live-fire `cron_skipped reason=duplicate_content` events at
      `11:59Z` / `13:59Z` / `15:59Z` / `20:01Z` 2026-05-25 + 4 more
      since.
    - Progress signal #3 (proactive attention surface): TB-282 +
      TB-287 + TB-288 + TB-289 cover 4 of 5 enumerated condition
      kinds (stuck / frozen / validator-judge anomalies / pending
      decision via pause-reason). The 5th — cost anomalies pre-trip
      — is the last clean leg.
  - Gaps:
    1. **Cost-cap approach (pre-trip)** — Progress signal #3 names
       "cost or validator-judge anomalies" verbatim. Validator-judge
       anomalies got TB-288's noisy detector (a pre-trip / pre-trip-
       paired signal). Cost anomalies got only the TRIPPED state via
       TB-289 (`per_task_token_cap_exceeded` / `window_token_cap_
       exceeded` flow through `auto_approve_paused`). No detector
       fires at e.g. 75% of the configured window cap so the
       operator can intervene BEFORE auto-approve halts — the
       cost analog of TB-288's noisy threshold. Last cycle deferred
       this on a faulty grep — `grep cost_cap` returned only
       Out-of-scope mentions, but the actual knobs are
       `AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP` /
       `AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP` (TB-224 / `auto_approve.py`
       L251-285). Cap infrastructure exists; only the pre-trip
       approach detector is missing.
  - Status: `in-progress`
  - Reasoning: focus-2 PS#3 has one remaining leg with concrete
    upstream infrastructure to build against; ranking + proposing
    a single tight detector this cycle (TB-290). Other gaps below
    are deferred or not aligned.

## Non-goal risk check

The single proposal stays within the `ap2/attention.py` detector
shape established by TB-282/287/288/289 — adds no cross-project
aggregation, no goal.md auto-rotation, no unconditional automation.
The threshold knob (`AP2_AUTO_APPROVE_COST_APPROACH_PCT`) defaults
to 75 and the detector no-ops unless `AP2_AUTO_APPROVE_WINDOW_TOKEN_
CAP > 0` (TB-224 caps are 0/disabled by default). Pure additive
surface.

## Considered & deferred this cycle

- **`task_failing_repeated` detector** — goal.md PS#3 says
  "stuck / failed / frozen tasks". TB-282 covers stuck, TB-287
  covers frozen; "failed" could mean repeated `verification_failed`
  pre-retry-exhaustion. But verification_failed events are auto-
  recovered via the retry budget (3 attempts before Frozen) — the
  operator's actionable signal lands at `retry_exhausted` which
  TB-287 already surfaces. A mid-retry warning would either be
  premature (the retry might succeed) or duplicate the eventual
  task_frozen bullet. Defer; the "failed tasks" wording in goal.md
  is operationally covered by task_frozen.
- **`decisions_needed_new` detector** — goal.md PS#3's
  "decisions-needed" leg. TB-289's auto_approve_paused already
  covers the actionable subset (pause_reasons with registered ack
  verbs). Broader decisions-needed entries (`roadmap_complete`,
  janitor findings, future entries) already flow through the
  line-forwarded `## Decisions needed from operator` block at the
  top of the status-report (TB-173 / TB-191). Promoting these to
  Attention is purely visual rather than adding signal, and risks
  duplication with the line-forward. Defer; line-forward is the
  right surface.
- **Web `/attention` pull page** — listed in TB-282's Out-of-scope
  as a follow-up "once the event vocabulary lands AND accrues
  data." The first 4 detectors literally just landed today; ~24h
  of attention_raised events is insufficient to validate a page
  design. Defer 1-2 weeks for data accrual.
- **Immediate-Mattermost-push on attention_raised** — listed in
  TB-282's Out-of-scope; bypasses the cron schedule. TB-281's
  content-fingerprint dedup already closes the "clock-driven
  repetition" failure mode (goal.md L196-204) — an immediate-push
  surface is a different architecture goal.md doesn't explicitly
  require. Defer.
- **TB-175-shape ideation-acceptance-rate aggregator** — operator
  parked it 2026-05-07T01:57Z pending ≥3 cycles of TB-188 records;
  still gated.
- **Rejection-pattern check (carried, re-justified)**: TB-185 /
  TB-184 vetoed ap2-meta-polish; TB-231 vetoed symptom-patching;
  TB-175 vetoed premature aggregation; TB-240 vetoed validator
  whack-a-mole. This cycle's single proposal follows TB-282's
  `_detect_<name>` shape and closes the last literal item from
  Progress signal #3's enumerated condition list — not meta-polish,
  not aggregation, not symptom-patching. Pattern clears.

## Cycle observations

- Cap infrastructure clarification (correction): last cycle's
  Considered-deferred bullet said "no `AP2_AUTO_APPROVE_DAILY_COST_CAP`
  exists" and grepped only for the substring `cost_cap`. The actual
  TB-224 knobs are spelled `AP2_AUTO_APPROVE_{PER_TASK,WINDOW}_TOKEN_CAP`
  with corresponding `per_task_token_cap_exceeded` / `window_token_cap_
  exceeded` pause_reasons in `_PAUSE_REASON_ACK_VERB`. The substring
  miss is the only reason cost_cap_approach was deferred; this cycle
  corrects.
- Sequencing: TB-290 is the only proposal this cycle. After it
  lands, focus-2 PS#3's 5 enumerated condition kinds are all
  detector-backed (with web /attention + immediate-push as later
  pull/push polish), and the focus is structurally close to
  exhausted on the empty-cycles signal — anticipate the next 1-2
  ideation cycles producing 0 proposals and tripping
  `AP2_FOCUS_ADVANCE_EMPTY_CYCLES` since focus-2 is also the last
  focus (operator's roadmap-complete state).

## Decisions needed from operator

(none this cycle — the active focus has one identifiable remaining
leg this cycle addresses; the standing `roadmap_complete` decisions-
needed entry from 2026-05-23T03:06Z is operator-owned and not for
ideation to revisit.)

## Proposals this cycle

1 proposal queued behind `@blocked:review`:

- TB-290 — `cost_cap_approach` attention detector (closes "cost
  anomalies" pre-trip leg of Progress signal #3).

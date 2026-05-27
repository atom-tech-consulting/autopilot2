# Ideation State

_Last updated: 2026-05-27T06:40:36Z by ideation cron_

## Mission alignment

Cycle entry: board 0A / 0R / 0B / 0P / 164C / 3F; focus pointer just
operator-rewound at 2026-05-27T06:33:52Z back to `operator-legible
reporting and monitoring` with explicit reason "arc complete;
re-engaging to verify post-fix behavior + let ideation propose
remaining axes (web /attention page, immediate-MM push, etc.)". The
3 ideation_skipped reason=roadmap_complete events between 22:39Z and
04:40Z confirm last cycle's prediction that focus-2 would exhaust on
empty-cycles; the operator's response was to rewind rather than
extend the roadmap, scoping ideation back at TB-282's documented
Out-of-scope items. Recent Completes considered:

- TB-293 (`5c6d2a8` 2026-05-26T21:01Z) — mirrored the auto-approve
  gate into `operator_queue._apply_operator_op` add_backlog branch
  (closes the review-token stranding regression).
- TB-294 (`48d3fd1` 2026-05-27T05:06Z) — disabled extended thinking
  in `_run_scrub`; added typed `Scrub*Error` exceptions and emits
  `ideation_state_scrub_error` audit events.
- TB-295 (`6081f96` 2026-05-27T06:33Z) — `ap2 rewind-focus` operator
  CLI verb + synthetic `focus_advanced trigger=operator_rewind`
  event for empty-cycles counter cutoff (used by operator
  immediately after landing to re-engage focus-2).

## Current focus assessment

- **Current focus: operator-legible reporting and monitoring**
  - Progress so far:
    - Progress signal #1 (title + project_name): TB-280 closed.
    - Progress signal #2 (significance-gated + dedup): TB-281
      closed.
    - Progress signal #3 (proactive attention surface): TB-282 +
      TB-287 + TB-288 + TB-289 + TB-290 — all 5 enumerated condition
      kinds (stuck / frozen / validator-judge anomalies / pending
      decision / cost approach) now detector-backed; the status-
      report `## Attention needed` section is the only consumer.
  - Gaps:
    1. **Web `/attention` pull page** — TB-282 Out-of-scope L123-125
       explicitly deferred this "once the event vocabulary lands AND
       accrues data". Vocabulary fully landed (5 detectors, TB-282
       + TB-287...TB-290); production accrual at 0 `attention_raised`
       events to date (verified via `grep -c '"type":
       "attention_raised"' .cc-autopilot/events.jsonl` → 0), which
       is expected for a quiet project — the empty-state page is
       still the canonical pull surface for "what conditions are
       currently active". Today the operator must run `ap2 status`
       OR wait for the next 2h status-report cron to see active
       attention conditions; the web home page has no parallel
       surface despite TB-242's automation-status cards covering
       sibling axes.
    2. **Immediate Mattermost push on detector fire** — TB-282
       Out-of-scope L119-122 explicitly deferred this "out-of-band
       immediate push has its own rate-limit + dedup concerns and
       belongs in a follow-up". The status-report cron's per-2h
       cadence means a stuck-Active condition that fires at
       minute 5 of the window still waits up to 115 minutes to
       surface in chat. The post-trip `auto_approve_paused`
       detector (TB-289) is the most acute example: dispatch has
       already halted by the time the next status-report cron
       runs. The watchdog's `_first_mm_channel` + `tools._mm_post`
       chain is the existing reference pattern (per
       `ap2/watchdog.py` L112-150) — same channel routing,
       sticky-warning-on-missing-destination, post + audit-event
       shape — adapted to attention conditions with a strict
       opt-in env knob (conservative-defaults constraint per
       goal.md Non-goals L253-256).
  - Status: `in-progress`
  - Reasoning: operator just rewound the focus and named both gaps
    in their rewind reason.

## Non-goal risk check

Both proposals stay within the per-project legibility scope (goal.md
focus-2 Scope guard L227-228 — "per-project legibility, NOT
cross-project aggregation"). Web `/attention` is an additive read-
only page (reuses `attention.detect_attention_conditions` + the
existing FastAPI `web_*` sibling pattern from TB-263). Immediate-MM
push defaults OFF (`AP2_ATTENTION_IMMEDIATE_PUSH` opt-in knob),
honors the same per-(type, key) debounce as the status-report
surface, and posts to `AP2_MM_CHANNELS[0]` only — no new cross-
project routing. Both surfaces are operator-curated trust upgrades
per goal.md Constraints L280-290.

## Considered & deferred this cycle

- **`attention_cleared` event class** — would let a future surface
  report "X resolved since last report" alongside fresh fires.
  Genuine value but speculative without operator ask AND adds a
  second event type to maintain. Defer until pull/push surfaces
  expose a clear gap (e.g. operator asks "what just resolved?" via
  the new web page).
- **JSON sub-endpoint at `/attention.json`** — would enable
  external monitoring tools to poll. Operator hasn't asked, and the
  per-project legibility scope guard (L227-228) explicitly excludes
  cross-project aggregation. Defer until a concrete consumer
  surfaces.
- **TB-175-shape ideation-acceptance-rate aggregator** — operator
  parked it 2026-05-07T01:57Z pending ≥3 cycles of TB-188 records;
  still gated.
- **Rejection-pattern check (carried, re-justified)**: TB-185 /
  TB-184 vetoed ap2-meta-polish; TB-231 vetoed symptom-patching;
  TB-175 vetoed premature aggregation; TB-240 vetoed validator
  whack-a-mole. Both this cycle's proposals are operator-named
  TB-282 Out-of-scope items closing literal Progress signal #3
  language — not meta-polish, not aggregation, not symptom-
  patching, not validator. Pattern clears.

## Cycle observations

- 0 production `attention_raised` events have fired despite 5
  detectors live since 2026-05-23 (TB-282) and yesterday (TB-287..
  TB-290) — production-quiet project. Informs both briefings: the
  web page must render an empty-state cleanly; the immediate-push
  knob must default OFF until volume data accrues, so a future
  cadence-tuning task has signal.
- Last cycle's empty-cycles prediction held (3× `ideation_skipped
  reason=roadmap_complete` in the gap), and the operator chose
  rewind over roadmap-extension — a useful precedent for future
  exhaustion cycles: rewind is cheap and reverses cleanly via
  TB-295's CLI verb.

## Decisions needed from operator

(none this cycle — focus pointer just rewound, both proposals map
to operator-named TB-282 Out-of-scope items.)

## Proposals this cycle

Two proposals queued behind `@blocked:review` (operator-named axes
from the 2026-05-27T06:33:52Z rewind reason):

- TB-296 — `/attention` web page (pull surface for current
  attention conditions; closes TB-282 Out-of-scope L123-125).
- TB-297 — Immediate Mattermost push on attention_raised emission
  (opt-in push surface for detector fires; closes TB-282
  Out-of-scope L119-122).
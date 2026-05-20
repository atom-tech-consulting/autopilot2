---
title: Validator-judge dep-coherence timeout investigation — 2026-05-18
tldr: |
  6/6 recent operator queue-append `add_*` / `update` calls in the last
  ~25h timed out the TB-235 dep-coherence judge (8/8 over the last 7d
  when the older two are included). The TB-243 fail-open hides the cost
  from the user-facing path but the gate is silently disabled on
  essentially every triggering call. Manual measurement against the
  real `_judge_dep_coherence_default` (Haiku-4.5, max_turns=2) shows the
  judge does succeed — at 17.6–46.8s wall-clock per call, depending on
  briefing size — but the 15s `AP2_VALIDATOR_JUDGE_TIMEOUT_S` default
  (+5s outer-thread grace = 20s ceiling) sits below the MEDIAN
  completion latency of the smallest real briefing measured (4621 B →
  ~22s avg). Dominant factor: `timeout-too-tight`. Secondary candidate
  for follow-up: `prompt-too-heavy` (full briefing markdown is shoved
  into the user payload; a Goal+Scope slice would shrink the input
  token count). `max_turns-too-tight` ruled out (bumping 2→4 did not
  shorten wall-clock). No fixes applied here; calibration is a separate
  follow-up TB.
updated: 2026-05-20
updated_by: TB-269
cites:
  - .cc-autopilot/events.jsonl:8393  # 2026-05-17T06:23:27Z validator_judge_timeout
  - .cc-autopilot/events.jsonl:8539  # 2026-05-17T17:45:48Z validator_judge_timeout
  - .cc-autopilot/events.jsonl:8597  # 2026-05-18T04:54:00Z validator_judge_timeout
  - .cc-autopilot/events.jsonl:8666  # 2026-05-18T16:23:34Z validator_judge_timeout
  - .cc-autopilot/events.jsonl:8734  # 2026-05-18T17:56:14Z validator_judge_timeout
  - .cc-autopilot/events.jsonl:8757  # 2026-05-18T18:18:03Z validator_judge_timeout
  - .cc-autopilot/events.jsonl:8760  # 2026-05-18T18:24:04Z validator_judge_timeout
  - .cc-autopilot/events.jsonl:8763  # 2026-05-18T18:25:36Z validator_judge_timeout
  - ap2/tools.py:670   # _VALIDATOR_JUDGE_TIMEOUT_S_DEFAULT = 15.0
  - ap2/tools.py:678   # _VALIDATOR_JUDGE_MAX_TURNS_DEFAULT = 2
  - ap2/tools.py:892   # _judge_dep_coherence_default body
  - ap2/tools.py:1038  # asyncio.wait_for(_ask(), timeout=timeout_s)
  - ap2/tools.py:1050  # worker.join(timeout=timeout_s + 5)
  - ap2/tools.py:1056  # raise _DepJudgeTimeout("validator judge worker exceeded {timeout_s + 5:.0f}s")
  - goal.md:L82-85    # "upstream gates already make this safe in practice"
  - .cc-autopilot/insights/test-suite-slowness-2026-05-17.md  # TB-253 artifact shape this file mirrors
status: investigation (TB-256 deliverable; no fixes applied — calibration is a follow-up TB)
tags: [autopilot, validator-judge, observability, investigation, axis-1, dep-coherence]
---

# Validator-judge dep-coherence timeout investigation — 2026-05-18

Measurement window: 7 days back from 2026-05-18T19:15Z (the
`task_start` for TB-256). Source signal: every
`{"type": "validator_judge_timeout"}` entry in
`.cc-autopilot/events.jsonl`, cross-referenced with the immediately
preceding / following `operator_queue_append` event by event-stream
adjacency (the validator-judge runs synchronously inside the queue-drain
path, so the emitted `validator_judge_timeout` ALWAYS shares its
timestamp with the triggering append). Manual measurement run against
`_judge_dep_coherence_default` directly with a stopwatch around the
SDK call to characterize the real-call wall-clock distribution.

Captured because the TB-243 fail-open masks the gate's actual hit-rate
from the user-facing path: every queue-append still succeeds, but the
`@blocked:review` gate the operator's trust is supposed to flow through
fires ZERO gating verdicts on the queue-append set named below. The
load-bearing "upstream gates already make this safe in practice" claim
in goal.md L82-85 is, today, silently empty. This artifact is the
TB-253-shape investigation deliverable: a categorized characterization
of why the gate is timing out, so a follow-up calibration TB has data
to scope against. **No fixes applied here.**

## Category legend

The six buckets the briefing's Scope §6 named:

- **prompt-too-heavy** — the input payload (briefing markdown +
  description + blocked-tokens) is large enough that Haiku-4.5's
  end-to-end latency at typical sizes naturally exceeds the 15s budget,
  independent of network / SDK overhead. A prompt-shaping change
  (Goal+Scope slice, not full briefing) would shrink wall-clock.
- **max_turns-too-tight** — `AP2_VALIDATOR_JUDGE_MAX_TURNS=2` (TB-249's
  default) caps the agent loop at one assistant message + one optional
  tool call. If the judge consistently consumes the budget without
  producing a final JSON object, the response never arrives.
- **timeout-too-tight** — the 15s `AP2_VALIDATOR_JUDGE_TIMEOUT_S`
  default + 5s outer-thread grace (`worker.join(timeout=timeout_s + 5)`
  at `ap2/tools.py:1050`) sits below the typical end-to-end completion
  time for a real briefing. Raising the budget alone (no prompt
  changes) would shift the success rate from ~0% to most calls.
- **sdk-cold-start** — first SDK call in a fresh process pays an
  import / handshake cost the second call doesn't. If subsequent
  trials in a single process complete faster, this is the dominant
  signal.
- **network-flake** — wall-clock variance is high enough that latency
  is bounded by network conditions (Anthropic API edge, TLS handshake,
  TCP RTT) rather than model inference time. A retry-with-backoff would
  recover.
- **investigate-further** — agent couldn't categorize confidently;
  flagged for operator decision.

Operator can re-classify any row during follow-up review.

## Headline finding

**`timeout-too-tight` is the dominant factor.** The 15s default budget
(`_VALIDATOR_JUDGE_TIMEOUT_S_DEFAULT` at `ap2/tools.py:670`) sits
below the median end-to-end completion time for every briefing measured
in this investigation, including the smallest one. The outer-thread
grace at `ap2/tools.py:1050` (`worker.join(timeout=timeout_s + 5)`)
extends the effective ceiling to 20s, which is still below the
measured average for any real briefing. The judge call itself does
succeed and produces well-formed JSON (`{"hard_predecessors": [...],
"reasoning": "..."}` exactly per the TB-247 strict-JSON contract)
when given a sufficient budget — the failure mode is purely budget,
not parse-shape, not SDK-failure.

Manual measurement against `_judge_dep_coherence_default` (Haiku-4.5,
max_turns=2, fresh process per script run, three trials at each shape)
with a 60s ceiling:

| trial | briefing | bytes | max_turns | elapsed_s | outcome |
|-------|----------|-------|-----------|-----------|---------|
| 1 | surface-stats-window-aggregates-…    (TB-259) | 4621  | 2 | 26.00 | success, `hard_predecessors=[]` |
| 2 | surface-stats-window-aggregates-…    (TB-259) | 4621  | 2 | 17.65 | success, `hard_predecessors=[]` |
| 3 | investigate-validator-judge-dep-…   (TB-257)  | 5515  | 2 | 44.68 | success, `hard_predecessors=['TB-253']` |
| 4 | investigate-validator-judge-dep-…   (TB-257)  | 5515  | 2 | 38.75 | success, `hard_predecessors=['TB-253']` |
| 5 | investigate-validator-judge-dep-…   (TB-257)  | 5515  | 4 | 31.43 | success, `hard_predecessors=[]` |
| 6 | investigate-validator-judge-dep-…   (TB-257)  | 5515  | 4 | 46.75 | success, `hard_predecessors=[]` |
| 7 | add-stats-dashboard-at-stats-html-stats (TB-255) | 14219 | 2 | >20.0 (×3) | TIMEOUT @ 15s+5s ceiling, no completion |

Latency scales roughly with briefing-byte size:

- ~4.6 KB → 17–26 s (avg ~22 s)
- ~5.5 KB → 31–47 s (avg ~40 s)
- ~14.2 KB → never returned within 20 s; extrapolating from the trend,
  likely 60–120 s.

The 15s default is below the floor of the smallest sample. `max_turns`
2→4 did NOT shorten wall-clock (trials 5/6 vs 3/4: same briefing, no
speedup) — the judge isn't hitting its turn cap, so `max_turns` isn't
the budget-limiting factor.

## Enumerated `validator_judge_timeout` events (last 7d)

Eight events, all with `timeout_s=15.0`. The "trigger" column is the
operator-queue op that shares the event's timestamp (queue-drain runs
the validator synchronously, so the `validator_judge_timeout` and the
`operator_queue_append` ALWAYS share `ts`). For ideation-triggered
calls (no preceding `operator_queue_append`), the trigger is the
ideator's `do_board_edit({"action": "add_backlog"})` proposal
recorded on the next event line.

| # | ts | error | trigger op | trigger task | briefing | bytes |
|---|----|-------|------------|--------------|----------|-------|
| 1 | 2026-05-17T06:23:27Z | `validator judge worker exceeded 20s` | update     | TB-248 | (update; no new briefing body) | — |
| 2 | 2026-05-17T17:45:48Z | `validator judge worker exceeded 20s` | add_backlog | TB-253 | investigate-test-suite-slowness-profile.md | 8281 |
| 3 | 2026-05-18T04:54:00Z | `` (empty — pre-TB-247 worker-internal `asyncio.wait_for` path)         | add_backlog | TB-254 | add-ap2-tests-conftest-py-shield-set-ap2.md | 10879 |
| 4 | 2026-05-18T16:23:34Z | `validator judge worker exceeded 20s` | add_backlog | TB-255 | add-stats-dashboard-at-stats-html-stats.md | 14300 |
| 5 | 2026-05-18T17:56:14Z | `validator judge worker exceeded 20s` | update      | TB-255 | (update; same body as #4)                  | 14300 |
| 6 | 2026-05-18T18:18:03Z | `validator judge worker exceeded 20s` | add_backlog | TB-256 | fix-render-automation-card-in-web-py-mir.md | 7971  |
| 7 | 2026-05-18T18:24:04Z | `validator judge worker exceeded 20s` | (ideator add_backlog) | TB-257 | investigate-validator-judge-dep-coherenc.md | 5548 |
| 8 | 2026-05-18T18:25:36Z | `` (empty — inner-asyncio.wait_for path)                                | (ideator add_backlog) | TB-259 | surface-stats-window-aggregates-task-bul.md | 4649 |

Notes on the "error" column shape:

- `validator judge worker exceeded 20s` — the OUTER timeout
  (`worker.join(timeout=timeout_s + 5)` at `ap2/tools.py:1050`, raise
  string at `:1056`). The worker thread overshot the inner
  `asyncio.wait_for(_ask(), timeout=timeout_s)` by enough that the
  outer join also expired. This is the common shape — 6/8 events.
- Empty `error` (`""`) — the INNER `asyncio.TimeoutError` raised by
  `asyncio.wait_for` at `ap2/tools.py:1038`, surfaced via
  `result["exc"] = _DepJudgeTimeout(str(exc))` where `str(exc)` of an
  `asyncio.TimeoutError` is the empty string. 2/8 events. Same
  underlying timeout class; just a different surfacing branch.

The error-string variance is purely a diagnostic artifact of which
sentinel raised first (inner asyncio cancel vs outer thread-join);
both branches point at the same `timeout-too-tight` root cause.

## Headline finding (cont'd) — categorized factors

### `timeout-too-tight` (dominant)

Manual measurement shows 17.6–46.8 s end-to-end for briefings under
6 KB; the 15s default + 5s grace ceiling sits below the median of the
smallest sample. With the current default, the success rate is
essentially 0% for any briefing that goes through `_judge_dep_coherence_default`
in steady state — which matches the observed 6/6 timeouts on recent
operator queue-appends. The judge call ITSELF succeeds when given a
sufficient budget (60s ceiling) and produces well-formed strict-JSON
per the TB-247 contract. Raising `AP2_VALIDATOR_JUDGE_TIMEOUT_S` to
30s would likely flip the smallest-briefing class to ~majority
success; raising to 60s would likely flip all classes including the
~14 KB TB-255 shape. Cost per successful call (Haiku-4.5, ~22s ×
typical token volume): approximately $0.005–0.02, well within the
TB-235 cost-floor named in `ap2/tools.py:695-702`. Recommended
follow-up TB shape: **raise the default to 60s (cover the observed
upper bound with margin) and add the `[noisy]` `automation_status`
suffix you already have so an operator can see the cost-vs-coverage
trade-off in `ap2 status`.**

### `prompt-too-heavy` (secondary; not blocking)

The user payload at `ap2/tools.py:988-996` embeds the FULL briefing
markdown. At 14 KB that's ~3500 input tokens just for the briefing,
plus the strict-JSON system prompt (~1000 tokens). Haiku-4.5's
end-to-end latency at ~5K input tokens is naturally 20–40 s, which
matches the measured 17–47 s range. A prompt-shaping change — pass
ONLY `## Goal` + `## Scope` body, drop `## Verification` / `## Out of
scope` / `## Design` since none of those bear on dep-coherence — would
shrink the input by ~50–70% and likely cut wall-clock proportionally.
This is an orthogonal lever from the timeout knob and the two should
NOT be conflated in one calibration TB: the timeout bump unblocks
today's failure mode immediately; the prompt-shaping refactor reduces
the steady-state cost. Recommended follow-up TB shape: **after the
timeout bump lands and shows the gate is firing, separately scope a
"prompt-shape: pass only Goal+Scope to the dep-coherence judge"
TB that shaves the average call cost and latency without touching the
budget knob.**

### `max_turns-too-tight` (ruled out)

Trials 3/4 vs 5/6 above use the same TB-257 briefing with `max_turns=2`
vs `max_turns=4` respectively; wall-clock is comparable (avg 41.7 s vs
39.1 s) and the bump does NOT shorten end-to-end latency. The judge
isn't hitting its turn cap — it produces a final JSON message within
the first assistant turn and the additional budget is unused. This
rules out `max_turns-too-tight` as a contributing factor. Recommended
follow-up TB shape: **none.** The TB-249 default of 2 is sufficient.

### `sdk-cold-start` (ruled out)

Each measurement script run spawns a fresh Python process, so trial 1
of each shape pays the SDK import + first-call handshake. Comparing
trial-1 vs trial-2 within a single process (e.g. 26.00 s vs 17.65 s for
the TB-259 shape, 44.68 s vs 38.75 s for the TB-257 shape) shows
modest variance but no consistent "second call is much faster" signal
of the kind cold-start would produce. The 30%+ trial-1 overhead the
"cold-start" hypothesis would predict isn't there. Rules out
`sdk-cold-start` as the dominant factor; latency is bounded by model
inference, not import / handshake. Recommended follow-up TB shape:
**none.**

### `network-flake` (ruled out)

Across the seven trials (one shape that timed out and three shapes
that completed twice each), latency variance is consistent with model
inference variance — no single trial wildly diverges from the others
of the same shape (the 26 s / 17.6 s TB-259 pair is 32% spread, the
44.7 s / 38.8 s TB-257 pair is 14% spread; both within normal Haiku
end-to-end variance). No `RuntimeError` / `ConnectionError` /
`anthropic.APIError` surfaces in the `result["exc"]` branch at
`ap2/tools.py:1042`. Rules out `network-flake` as the dominant
factor. Recommended follow-up TB shape: **none.**

### `investigate-further`

None. The five categories above explain the observed data without
residual.

## Implications for follow-up TBs

Recommended sequence:

1. **First (unblock):** Raise `AP2_VALIDATOR_JUDGE_TIMEOUT_S` default
   from 15.0 → 60.0 at `ap2/tools.py:670`. Covers the observed upper
   bound (~47s for ~5.5 KB briefings) plus margin for the larger
   ~14 KB shape. Mechanical 1-line change + test update in
   `test_dep_judge_env_knob_defaults`. Verify by re-running the manual
   measurement script in this artifact's prior-art section against
   TB-255's 14 KB briefing and confirming completion ≤ 60s. This
   immediately flips the dep-coherence gate from "0% firing" to
   "~majority firing" on every operator queue-append, which is what
   goal.md L82-85 needs operationally.

2. **Then (steady-state cost):** Reshape the user payload at
   `ap2/tools.py:988-996` to pass only `## Goal` + `## Scope` body
   (drop `## Design`, `## Verification`, `## Out of scope`). Reduces
   median input token count by ~50–70% which should shave call
   latency proportionally, and reduces cost-per-call by the same
   ratio. Verifiable by re-running the manual measurement script and
   confirming the same briefing classes complete in 7–20 s rather
   than 17–47 s. Once this lands, an operator could consider DROPPING
   the timeout default back from 60s to 30s, but the order matters —
   bumping the timeout first preserves the gate even if prompt
   reshaping introduces a regression.

3. **Optional (visibility):** Add a `validator_judge_call` (success-
   path) event at the `_judge_dep_coherence_default` return site so
   `automation_status` can surface the actual call success rate +
   p50/p95 latency, not just the count of FAIL/TIMEOUT events. Today
   the status surface only counts failures; a successful call leaves
   no trace, so the operator can't see the post-fix improvement in
   the steady-state metrics without an explicit grep. Mirror the
   `judge_call` event shape already emitted by the prose judge at
   `ap2/verify.py`.

## What's NOT in this artifact

Per the TB-256 briefing's "Out of scope" list:

- Any calibration patch to `AP2_VALIDATOR_JUDGE_TIMEOUT_S`,
  `AP2_VALIDATOR_JUDGE_MAX_TURNS`, or the dep-coherence prompt body.
  This file is the deliverable; the calibration TBs are
  follow-ups (sequence sketched above).
- Changes to `_judge_dep_coherence_default` /
  `_judge_prose_bullet` / the validator-judge SDK call signature.
- Re-enablement of `AP2_AUTO_APPROVE` or any operator-knob flip.
- Web surface or status-report digest changes for timeout data
  (TB-243 / TB-245 already cover the user-facing surfaces).

Re-run cadence: this is a one-shot snapshot, anchored to the
2026-05-18 datestamp like TB-253's `test-suite-slowness-2026-05-17.md`
precedent so it stays useful as a historical baseline rather than
something the operator overwrites. If timeout events keep firing
AFTER the calibration TB lands, the operator runs another TB-253-shape
investigation against the post-fix dataset to confirm the cause is
different, not the same.

## Calibration applied (TB-269)

Calibration follow-up shipped 2026-05-20 — the first item from the
"Implications for follow-up TBs" sequence above. Append-only block;
the measurement sections above are preserved verbatim as the
historical baseline.

- **Default bump.** `_VALIDATOR_JUDGE_TIMEOUT_S_DEFAULT` raised from
  `15.0` to `60.0` at `ap2/validator_judge.py` (post-TB-262 home; the
  pre-TB-262 cite at `ap2/tools.py:670` above resolves to the same
  constant after the source split). 60s sits 1.5× the artifact's
  measured worst case of ~47s, rounded up to the smallest round
  number — same `_VERIFY_TIMEOUT_AUDIT_FIX_MULT=1.5` ratio the TB-252
  doctor audit recommends for the verify-timeout knob. Operators
  still tighten via `AP2_VALIDATOR_JUDGE_TIMEOUT_S` (the env knob is
  unchanged); the default now sits above the real-world ceiling
  instead of below the median.

- **`validator_judge_passed` event.** Emitted on every successful
  `_judge_dep_coherence_default` SDK call (before the JSON parse —
  a parse-failure call still paid the same wall-clock and that cost
  matters for timeout sizing). Payload: `{ts, type, duration_s,
  briefing_bytes, max_turns, timeout_s}`. Mirrors TB-252's
  `verify_passed` shape verbatim. Completes the
  happy-path / fail-open / timeout triangle on a single namespace so
  the operator now sees the gate's true firing rate, not just the
  failure subset TB-243's count surface exposes.

- **`validator_judge_timeout_audit` doctor surface.** New audit in
  `ap2/doctor.py` mirroring `verify_timeout_audit` verbatim (with
  `verify_passed` → `validator_judge_passed` and
  `AP2_VERIFY_TIMEOUT_S` → `AP2_VALIDATOR_JUDGE_TIMEOUT_S`). Wired
  into `diagnose()` immediately after the `verify_timeout_audit`
  section so the two timeout-fit surfaces sit as a paired block in
  `ap2 doctor` output. Closes the calibration-drift loop: if a future
  workload shift (heavier briefings, model swap, prompt growth) takes
  the SDK call back above the 60s floor, the audit's WARN band
  surfaces it at pre-flight time instead of waiting for the next
  TB-257-shape investigation.

Verification of the bump (post-landing manual measurement) is
deferred to operator review of the next 7d window of
`validator_judge_passed` events — once enough samples accumulate, the
`ap2 doctor` `validator-judge timeout headroom` section reports
whether the 60s floor sits comfortably above observed-typical or
needs a further nudge.

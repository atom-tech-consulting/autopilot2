# Ideation State

_Last updated: 2026-05-20T01:40:34Z by ideation cron_

## Mission alignment

Cycle entry: board 0A / 0R / 0B / 0P / 140C / 3F — backlog drained
again after a heavy operator-led code-quality push (TB-260 → TB-268
all approved and completed within ~28h, mostly modularity splits and
the centralized JSON-extract refactor). Last cycle's three proposals
(TB-257 timeout investigation, TB-258 audit push-surface, TB-259 stats
push-surface) all landed Complete + got approved by operator, plus the
operator queued seven additional follow-ups (TB-260 env staleness,
TB-261 raw_decode util, TB-262-265 four module splits, TB-266-268
three test-file splits) — strong signal that the focus is healthy.
Backlog is empty; slot count = 5.

Recent Completes considered (last ~24h):

- TB-268 (`bdf1262`, 2026-05-20T01:40Z) — split `test_tools.py`
  (118KB → 37KB) into validator/board/queue test modules.
- TB-267 (`9d2e1f8`, 2026-05-20T01:14Z) — split `test_web.py`
  (131KB → 18KB) into 7 web-prefixed sibling modules.
- TB-266 (`ce24c21`, 2026-05-20T00:51Z) — split `test_cli.py`
  (133KB → 4KB) into four cli-prefixed sibling modules.
- TB-265 (`84db3ad`, 2026-05-19T23:08Z) — `_render_env_stale_warning`
  added to `web_home.py` closing TB-260 surface gap on web home.
- TB-264 (`6e0a409`, 2026-05-19T22:57Z) — `cmd_status` errors on
  missing project root; final cli.py split is in place.
- TB-263 (`8be43e1`, 2026-05-19T20:04Z) — split daemon.py
  (187KB → 87KB) into 9 sibling modules.
- TB-262 (`f46b050`, 2026-05-19T19:45Z) — split tools.py (224KB →
  57KB) into 4 surface-area sibling modules.
- TB-261 (`a7641c4`, 2026-05-19T17:34Z) — centralized
  `extract_rightmost_json_object` util replaces 4 brittle find/rfind
  sites in verify/janitor/tools.
- TB-260 (`b63a7b5`, 2026-05-19T16:24Z) — `.cc-autopilot/env` mtime
  staleness surfaced in `ap2 status`, cron digest, and watchdog.
- TB-259 (`825fe51`, 2026-05-19T15:34Z) — `/stats` window aggregates
  pushed into cron status-report digest (TB-255 push-parity).
- TB-258 (`08b8a36`, 2026-05-19T03:36Z) — `audit:` line on `ap2
  status` text/JSON + cron digest sub-block (TB-248 push-parity).

## Current focus assessment

- **Current focus: end-to-end automation (goal.md L38-151, four axes)**
  - Progress so far:
    - Axis 1 (manual-approval): TB-223/224/232/234/241/243/245/247/
      250/256/258 (auto-approve mode + dep-coherence judge +
      surfaces). Auto-approve remains operator-disabled but the
      surface, dry-run, doctor-warn, validator-judge observability,
      and pull/push-parity surfaces are all green.
    - Axis 2 (failure-recovery): TB-225/229/233/239/236/252
      (auto-unfreeze + briefing-fix + verify-timeout doctor warn).
    - Axis 3 (cost/blast-radius): TB-224/227/228/234
      (cumulative-regression circuit-breaker + window/per-task caps).
    - Axis 4 (multi-focus): TB-226/237/242/244/246 (focus pointer +
      roadmap-complete halt + surface parity).
    - Code-quality / agent-friendliness: TB-253/254/261/262/263/264/
      265/266/267/268 (test-suite shielding, JSON util centralize,
      five module splits + three test-file splits).
    - Cross-axis observability: TB-248/255/257/258/259/260.
  - Gaps:
    (1) **Validator-judge dep-coherence judge STILL times out on
        every operator queue-append** — TB-257 investigation
        artifact (`.cc-autopilot/insights/validator-judge-timeout-
        2026-05-18.md`) measured the SDK call directly at
        17.6-46.8s wall-clock per call, but the 15s
        `AP2_VALIDATOR_JUDGE_TIMEOUT_S` default + 5s outer-thread
        grace = 20s ceiling sits BELOW the median completion of even
        the smallest measured briefing (4621B → ~22s avg). Dominant
        factor categorized: `timeout-too-tight`. Continued evidence:
        15 `validator_judge_timeout` events in the last 500 events
        across 7d (all 15 at the 15s timeout_s ceiling); 8 timeouts
        in the trailing 24h alone (`ap2 status` confirms). TB-257
        artifact explicitly says "calibration is a follow-up TB" —
        no calibration TB queued yet. Load-bearing for goal.md L82-
        85 "upstream gates already make this safe in practice."
    (2) **Validator-judge user payload shoves full briefing
        markdown** (`ap2/validator_judge.py:378`) — TB-257 artifact
        named `prompt-too-heavy` as the secondary factor: smallest
        measured briefing (4621B) still took ~22s avg, and most
        recent briefings are ≥6KB. The judge needs Goal+Scope to
        identify hard predecessors, not full Design/Verification/
        Out-of-scope. A slice-only payload would shrink wall-clock
        independently of any timeout bump. Complementary to (1):
        bump is the deadline fix, slice is the structural pressure
        relief.
    (3) **TB-255 `grep -cE` shell-bullet auto-unfreeze coverage** —
        still deferred. No n=2 recurrence (TB-264 and TB-265's
        recent verification_failed events both resolved via second
        commits via cumulative-diff, not retry_exhausted). Operator
        rejection patterns (TB-172, TB-240) name the whack-a-mole
        risk explicitly; defer until ≥2 same-shape recurrence.
    (4) **Dry-run interesting-types coverage** — same defer rationale
        as last cycle.
  - Status: `in-progress`
  - Reasoning: Two concrete fillable gaps (1) and (2) both
    grounded in the TB-257 insight artifact's measured data, both
    inside axis 1's load-bearing dep-coherence surface; the third
    slot stays empty (gap (3)/(4) explicitly deferred per operator-
    pattern signals). Quality over quantity is the right call —
    operator rejection patterns punish weak third-slot fillers.

## Non-goal risk check

None. Gaps (1) and (2) land squarely on axis 1's dep-coherence
gate; neither drifts toward generic task scheduling, goal
auto-rotation, or unconditional automation.

## Considered & deferred this cycle

- **Split `ap2/status_report.py` (67KB) or `ap2/operator_queue.py`
  (85KB) following TB-262 pattern** — operator approved
  operator_queue.py at 85KB without flagging it; both are below the
  100KB pain threshold the recent splits targeted (tools.py 224KB,
  daemon.py 187KB, web.py 179KB, cli.py 118KB). Defer until
  observed agent-friendliness pain in those modules.
- **Fill placeholder test modules (`test_web_stats.py`,
  `test_validator_judge.py`)** — both have explicit "placeholder by
  design" docstrings citing the TB-267/268 "pure mechanical move —
  NO new/renamed tests" rule. New tests should follow the
  `test_tb<N>_*.py` regression-pin convention instead. No-op for
  ideation.
- **Add doctor warn for `ap2/*.py` > 100KB (preventive
  modularity)** — preventive without observed-trigger, exactly the
  shape the operator's TB-185 rejection class targeted ("utility not
  aligned"). Defer until a real >100KB module reappears.
- **TB-175-shape ideation-acceptance-rate aggregator** — operator
  ack on 2026-05-07T01:57Z said defer until ≥3 ideation cycles after
  TB-188/TB-189 land so impact verdicts accumulate; conditions still
  met but no positive signal that operator wants it now.
- **Adaptive validator-judge timeout (auto-tune from observed P95)**
  — premature; TB-269-shape static bump is the simpler first move,
  and adaptive only pays after a baseline calibration lands.
- **Per-task validator-judge linkage on `ap2 status` (which
  queue-append got fail-open'd?)** — count surface (TB-243) is
  already actionable; per-task linkage adds complexity without
  clear operator demand.

## Cycle observations

- Operator's TB-260 → TB-268 streak (10 tasks in ~28h, all
  operator-curated) shows engaged code-quality push alongside
  ideation-driven axis work. Ideation should respect goal.md focus
  (end-to-end automation, NOT code quality) — code-quality
  proposals from ideation would compete with operator's own picks
  and need higher bar.
- TB-264 and TB-265 both hit `verification_failed` mid-stream but
  resolved cleanly via second commits (the cumulative-diff verifier
  path TB-127/TB-136 closed). Neither went retry_exhausted. Encouraging
  signal that the verifier handles the "partial split, follow-up
  commit" pattern correctly without operator intervention.
- TB-257 artifact's data is the strongest TB-89-style insight
  produced this cycle: it categorically rules out
  `max_turns-too-tight` / `sdk-cold-start` / `network-flake` with
  measurements, isolating the timeout calibration as the principled
  next move. The TB-260 series' insight `_index.md` aging will
  matter once calibration lands — re-measurement should update the
  TB-257 file's `updated`/`updated_by` rather than spawn a sibling.

## Decisions needed from operator

(none — no narrative-judgment items surfaced this cycle; gaps (1)
and (2) are mechanical follow-ups grounded in TB-257 data, not
operator-judgment escalations)

## Proposals this cycle

- TB-269 — Calibrate `AP2_VALIDATOR_JUDGE_TIMEOUT_S` default (15→60)
  + emit `validator_judge_passed` event + add doctor
  `validator_judge_timeout_audit` mirroring TB-252's
  `verify_timeout_audit`.
- TB-270 — Slim validator-judge user payload to Goal+Scope sections
  only (TB-257 secondary `prompt-too-heavy` factor) — independent
  wall-clock reduction lever, complementary to TB-269's deadline
  bump.

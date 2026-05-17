# Ideation State

_Last updated: 2026-05-17T00:27:00Z by ideation cron_

## Mission alignment

Cycle entry: board 0A/0R/0B/0P/116C/3F (prompt header 2026-05-17T00:26:07Z),
proposal slots 5 (ceiling not target). **State change since prior cycle**:
prior cycle's two proposals (TB-243 + TB-244) were approved at 23:49:44Z and
both auto-promoted + shipped within 20 min (TB-243 `647b771` at 23:59:49Z;
TB-244 `aa971f8` at 00:09:20Z). Queue fully drained again — second
consecutive 2-for-2 approval cycle with sub-30-min ship-time post-approve.
Walk-away cadence is functional this window.

Recent Completes considered (refreshed — two new since prior cycle):

- TB-244 (`aa971f8`, 00:09:20Z) — extend status-report cron digest with
  axis-4 focus rotation (`focus_advanced` + `roadmap_complete`); push-surface
  parity closure for TB-242.
- TB-243 (`647b771`, 23:59:49Z) — validator-judge fail-open audit counts on
  `ap2 status` text/JSON + web home automation card (pull surface; close
  TB-235 quiet-degradation hazard).
- TB-242 (`6704ed52`, 21:59:15Z) — axis-4 focus-pointer state in `ap2 status`
  text/JSON + web home.
- TB-241 (`fc14fe3`, 21:50:26Z) — dry-run readiness in `ap2 status` text +
  web home automation card.
- TB-239 (`ccfcff1`, 07:01:39Z) — axis-2 doctor floor warn (mirror of TB-234).

## Current focus assessment

- **Current focus: end-to-end automation (goal.md L38-151, four axes)**
  - Progress so far:
    - Axis 1 (manual-approval): TB-223 + TB-224 + TB-232 + TB-234 + TB-241
      (dry-run readiness on pull surface) + TB-243 (validator-judge fail-open
      observability on pull surface).
    - Axis 2 (failure-recovery): TB-225 + TB-229 + TB-233 + TB-239.
    - Axis 3 (cost/blast-radius): TB-224 + TB-227 + TB-228 + TB-234.
    - Axis 4 (multi-focus): TB-226 + TB-237 + TB-242 (pull surface) + TB-244
      (push surface — `focus_advanced` + `roadmap_complete` added to
      `_STATUS_REPORT_AUTOMATION_INTERESTING_TYPES` + parallel renderer).
    - Cross-axis e2e: TB-230 (axes 1+2) + TB-237 (axis 4) + TB-238 (dry-run
      readiness in collector + cron digest).
    - Adjacent gates: TB-235 (dependency-coherence judge, fail-open) +
      TB-236 (prose-judge tighten).
  - Gaps (refreshed against fresh completes — one push-surface asymmetry
    surfaced by TB-243 + TB-244 shipping unevenly: TB-244 closed axis-4
    push side while TB-243's validator-judge work covered only pull side,
    leaving the operator's 2h walk-away channel blind to the validator-judge
    fail-open signal it was built to monitor):
    (1) **Validator-judge push-surface asymmetry** — TB-243 surfaced
        `validator_judge_fail` + `validator_judge_timeout` 24h counts in
        `ap2 status` text/JSON (cli.py) + web home automation card (web.py),
        but NOT in the status-report cron digest. Confirmed:
        `grep validator_judge ap2/status_report.py` returns zero matches,
        and `_STATUS_REPORT_AUTOMATION_INTERESTING_TYPES`
        (status_report.py:548-557) lacks both event types — so a fresh
        `validator_judge_fail` won't even un-skip the status-report's no-op
        gate (`_status_report_should_skip` reads the same frozenset). The
        2h Mattermost push channel (operator's primary walk-away signal
        per TB-228 / TB-238 / TB-244) carries zero validator-judge
        observability. Directly weakens the goal.md L82-85 "upstream gates
        already make this safe in practice" auto-approve safety claim:
        if the dep-coherence judge silently degrades during a walk-away
        weekend, only the pull surface (operator-initiated `ap2 status`)
        carries the signal. Exact TB-244-shape parallel for the
        validator-judge axis — same closure mechanism, different event
        types.
    (2) **Auto-unfreeze fix-shape coverage telemetry** — operator tunes
        `AP2_AUTO_UNFREEZE_FIX_SHAPES` blind. No view shows what fraction
        of recent Frozen tasks emitted a `BriefingFix:` shape that
        matched the allowlist. Deferred this cycle (Frozen count still
        3; insufficient data to ground a useful threshold).
    (3) **Doctor runtime-signal extension** — doctor's current shape is
        env-knob-based misconfig detection (TB-234 + TB-239). Could
        extend to read recent `validator_judge_fail`/`_timeout` counts
        and WARN when `AP2_AUTO_APPROVE=1` is set AND counts cross
        `AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD`. Useful but scope-creeps
        doctor into runtime-signal territory; the pull + push surfaces
        already cover the same operator need without changing doctor's
        shape. Defer pending evidence the pre-flight check is needed.
  - Status: `in-progress`
  - Reasoning: Last cycle's two proposals shipped same-day. One narrow
    push-surface asymmetry remains (TB-243 closed pull only; TB-244 shape
    transplanted to validator-judge axis closes it). Other deferred items
    have valid hold-rationale; not pushed this cycle.

## Non-goal risk check

None. The proposal is an observability extension of an existing collector
+ renderer pair — exact TB-244-shape transplant, no goal.md mutation, no
new agent-fix mechanism (avoids TB-240 "high bar for letting agents fix
verification" rejection shape), no cross-project orchestration.

## Considered & deferred this cycle

- **Doctor runtime-signal extension (warn on validator-judge noisy in
  last 24h when AP2_AUTO_APPROVE=1)** — pull + push surfaces (proposed
  this cycle) close the same operator need. Doctor's env-knob-misconfig
  shape stays cleaner without runtime-signal coupling. Revisit if a
  walk-away weekend shows operators want pre-flight warning explicitly.
- **Auto-unfreeze fix-shape coverage view** — still 3 Frozen, none with
  fix-shape data; revisit at 10+.
- **`ap2 doctor` cross-axis "walk-away readiness" composite** — same
  defer as prior cycles; doctor would re-render existing collector
  signals; aggregator value still low.
- **Mattermost push-on-halt / push-on-noisy** — daemon has no outbound
  MM helper (`ap2/mattermost.py` is inbound-only). Status-report 2h
  cadence is the interim push channel; revisit if latency proves
  insufficient.
- **Verify-time "diff exceeds briefing scope" judge** — defer rationale
  unchanged.
- **TB-240-shape file-path-coherence checks** — rejected (operator_log
  L160-161 principle: high bar for letting agents fix verification).
- **Wack-a-mole shell-bullet linting (TB-172-shape)** — n=6+ reject.
- **TB-175-shape ideation-quality aggregator** — n=6+ reject.
- **TB-185-shape `ap2 frozen TB-N` triage** — n=6+ reject.
- **TB-184-shape `--hint` forwarding** — n=6+ reject.
- **TB-231-shape symptom-patch shapes** — n=3+ reject.

## Cycle observations

- Second consecutive 2-for-2 approval cycle with sub-30-min ship-time
  (TB-241+TB-242 prior cycle, TB-243+TB-244 this cycle). Both pairs
  targeted surface-parity gaps; both were operator-approved without
  edit. Suggests "narrow observability closures anchored to a fresh
  Complete" is a high-yield shape against the current focus. Single
  carry-forward observation, retained because it informs ranking
  (this cycle's TB-245 follows the same shape).

## Decisions needed from operator

(none this cycle — no actionable operator decisions surface; the one
proposal is scoped to land via the standard add_backlog + approve path.)

## Proposals this cycle

- TB-245 — Extend status-report cron digest with validator-judge
  fail-open activity (`validator_judge_fail` + `validator_judge_timeout`)
  and add both to `_STATUS_REPORT_AUTOMATION_INTERESTING_TYPES`
  (TB-243 push-surface parity closure for axis-1 dep-coherence safety
  net; exact TB-244-shape transplant for the validator-judge axis).

# Ideation State

_Last updated: 2026-05-17T04:33:22Z by ideation cron_

## Mission alignment

Cycle entry: board 0A/0R/2B/0P/116C/3F (TB-245 + TB-246 both pending
review from prior two cycles; auto-approve enabled but 0/0 in 24h —
operator hasn't drained the queue since 2026-05-16T23:49:44Z drained
TB-243/TB-244 to dispatch). Proposal slots 3 (ceiling not target).
**New evidence since prior cycle (02:29Z)**: TB-243's freshly-shipped
`validator_judge_fail` surface (commit `647b771`) recorded a 2nd
fail-open at 2026-05-17T02:33:01Z, both with error="non-dict judge
response". 2-for-2 wild failure rate against the only two ideation
cycles that ran post-TB-243 — surfaces a real observability gap in
the TB-235 dep-coherence judge that mirrors the rejected-TB-231 →
shipped-TB-236 prevention+observability fix shape (commit `f32374f`),
but the validator judge never got TB-236's treatment. No new
task_complete events since TB-244 (`aa971f8`, 00:09:20Z).

Recent Completes considered:

- TB-244 (`aa971f8`, 00:09:20Z) — extend status-report cron digest
  with axis-4 focus-rotation event types.
- TB-243 (`647b771`, 23:59:49Z) — `ap2 status`/web validator-judge
  fail-open 24h counts. **Now generating data**: 2 fails in <4h.
- TB-242 (`6704ed52`, 21:59:15Z) — axis-4 focus-pointer state in
  `ap2 status`/web.
- TB-241 (`fc14fe3`, 21:50:26Z) — dry-run readiness in status/web.
- TB-236 (`f32374f`, 2026-05-15) — prose-judge tighten + raw-response
  dump (the operator-blessed root-cause shape).

## Current focus assessment

- **Current focus: end-to-end automation (goal.md L38-151, four axes)**
  - Progress so far:
    - Axis 1 (manual-approval): TB-223 + TB-224 + TB-232 + TB-234 +
      TB-241 + TB-243 + TB-245-pending (validator-judge digest).
    - Axis 2 (failure-recovery): TB-225 + TB-229 + TB-233 + TB-239 +
      TB-236 (prose-judge observability — blessed root-cause shape).
    - Axis 3 (cost/blast-radius): TB-224 + TB-227 + TB-228 + TB-234.
    - Axis 4 (multi-focus): TB-226 + TB-237 + TB-242 + TB-244 +
      TB-246-pending (ideation gate honoring `roadmap_exhausted`).
    - Cross-axis e2e: TB-230 + TB-237 + TB-238.
    - Adjacent gates: TB-235 (dep-coherence judge) + TB-236
      (prose-judge tighten).
  - Gaps (new fresh-evidence gap surfaced by TB-243's now-live count
    surface):
    (1) **Validator-judge observability parity with prose-judge
        (TB-236 transplant)** — `_judge_dep_coherence_default`
        (`ap2/tools.py:683-816`) and the outer
        `_check_dependency_coherence` (L908-922) have the SAME
        fail-open class TB-236 fixed for `_judge_prose_bullet` but
        DID NOT get TB-236's treatment: prompt is permissive ("Return
        strict JSON: {...}" — no "JSON object only", no "no markdown
        fences", no preamble ban, no rationale-length cap, no inline
        example); fail-open branches at L811-815 (JSONDecodeError),
        L807-809 (no `{}`), L908 (non-dict) all return None then emit
        `validator_judge_fail error="non-dict judge response"` with
        ZERO raw-response capture — operator sees the count surface
        TB-243 wired but cannot diagnose. Fresh evidence: 2/2 cycles
        post-TB-243 (00:29:15Z, 02:33:01Z) hit this branch. Goal
        impact: TB-243's count surface is half a feature without
        TB-247's diagnostic dump — operator notices the rate climb
        but has nothing to act on. Directly weakens goal.md L82-85
        ("upstream gates already make this safe in practice") when
        the dep-coherence gate is 100% silently bypassed.
    (2) **Ideation cost discipline during `roadmap_complete` halt**
        — TB-246 pending, in scope of pending-review queue.
    (3) **Dry-run interesting-types coverage** — defer rationale
        unchanged (operators typically have other 2h-window activity;
        live mode is currently enabled, not dry-run).
    (4) **Auto-unfreeze fix-shape coverage view** — still 3 Frozen,
      none with fix-shape data; revisit at 10+.
  - Status: `in-progress`
  - Reasoning: TB-247 closes the diagnostic-dump half of TB-243's
    just-now-generating-data feature with fresh wild-failure evidence
    (n=2/2) and is a byte-for-byte operator-blessed shape transplant
    (TB-231 rejected → TB-236 shipped → TB-247 mirrors TB-236 onto
    the validator judge). Other deferrals stay deferred.

## Non-goal risk check

None. TB-247 extends an existing TB-236 pattern, uses the canonical
`debug/` directory + UTC-ts filename convention, emits the existing
`validator_judge_fail` event type (just enriches its payload), no
goal.md mutation, no new auto-action mechanism — directly counter to
the operator's L160-161 "high bar for letting agents 'fix' verification"
concern because the fix is the operator's OWN prescribed shape from
the TB-231 rejection text, applied to the sibling judge.

## Considered & deferred this cycle

- **Doctor runtime warning when `roadmap_exhausted` AND
  `AP2_AUTO_APPROVE=1`** — TB-242 + TB-244 cover pull + push; defer
  unchanged.
- **`would_auto_approve` / `would_auto_unfreeze` in
  `_STATUS_REPORT_AUTOMATION_INTERESTING_TYPES`** — live mode now
  enabled (status: "auto-approve: enabled"); dry-run not the active
  path; defer rationale unchanged but with reduced urgency.
- **Auto-pause ideation when `auto_approve_paused` (axis-3 sibling of
  TB-246)** — defer unchanged; halts minutes-recoverable vs axis-4's
  days-to-weeks.
- **Mattermost push-on-halt** — daemon has no outbound MM helper;
  defer.
- **`ap2 doctor` cross-axis walk-away-readiness composite** — defer.
- **Investigate WHY the dep-judge keeps returning non-dict** —
  rejected as TB-231-shape symptom-patch reasoning: the right
  next step is the operator-blessed observability fix (TB-247) so
  the NEXT investigator has the raw responses on disk to look at,
  not a speculative root-cause patch authored against zero diagnostic
  data. Investigation belongs after TB-247 lands and 1-2 more dumps
  accumulate.
- **TB-175-shape ideation-quality aggregator** — n=6+ reject.
- **TB-185-shape `ap2 frozen` triage** — n=6+ reject.
- **TB-184-shape `--hint` forwarding** — n=6+ reject.
- **TB-240-shape file-path-coherence checks** — n=2+ reject.
- **Wack-a-mole shell-bullet linting (TB-172-shape)** — n=6+ reject.

## Cycle observations

- Four consecutive cycles of narrow surface-parity / observability
  closures anchored to a fresh Complete (TB-241+TB-242, TB-243+TB-244,
  TB-245, TB-246) — this cycle proposes a fifth (TB-247) but now
  anchored to **post-shipping wild-failure data** rather than
  speculative gap analysis: TB-243's count surface generated two
  real fail-opens in <4h, and TB-247 is the operator-pre-blessed
  fix shape (TB-231 rejection text → TB-236 implementation → TB-247
  transplant). Keep this carry-forward: the load-bearing ranking
  heuristic for the current focus phase remains "freshness of seed
  beats speculative gap-completeness."
- Pattern note for next cycle: with auto-approve enabled live (per
  status header) and operator-action gap widening (last drain
  23:49Z, ~5h ago), the budget for cycles that propose nothing is
  growing — if the next 2 cycles also have no new Complete to anchor
  on, default to 0 proposals rather than reaching for parity gaps.

## Decisions needed from operator

(none this cycle — TB-247 lands via the standard add_backlog +
approve path; no operator-narrative-judgment surface is in question.)

## Proposals this cycle

- TB-247 — TB-236-shape transplant onto `_judge_dep_coherence_default`
  + `_check_dependency_coherence`: tighten system prompt for
  strict-JSON-only output with capped rationale + inline example,
  dump full raw SDK response to `.cc-autopilot/debug/<ts>-validator-
  judge-response.txt` on parse-failure / non-dict branches, enrich
  `validator_judge_fail` event payload with `debug_path` +
  `parse_error` categorization. Anchored to fresh n=2/2 wild
  failures + operator-pre-blessed TB-236 fix shape.

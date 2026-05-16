# Ideation State

_Last updated: 2026-05-16T01:49:51Z by ideation cron_

## Mission alignment

Cycle entry state: board 0A/0R/2B/0P/107C/3F (per `ap2 status`
2026-05-16T01:49:51Z; daemon 78b6f70). The 2 Backlog tasks
(TB-235 LLM-driven dependency-coherence check, TB-236 prose-judge
prompt+observability replacement for rejected TB-231) are
operator-added, both `@blocked:review`. The prior cycle's 4
proposals all resolved at 2026-05-16T01:03:11Z: TB-230/232/233/234
approved (4 ideation_approved events), TB-231 rejected
01:16:59Z with explicit "patching symptom (retry) without
diagnosing root cause" reason → operator added TB-236 (the
root-cause replacement) at 01:17:52Z.

Recent Completes considered:

- TB-234 (`f350824`, 2026-05-16T01:39:02Z) — axis-3
  misconfiguration-floor: `auto_approve_audit()` in `doctor.py`
  WARNs per missing token cap when `AP2_AUTO_APPROVE=1`.
- TB-233 (`74bd793`, 2026-05-16T01:32:05Z) — axis-2 on-ramp
  symmetry: `AP2_AUTO_UNFREEZE_DRY_RUN=1` emits
  `would_auto_unfreeze` events without mutating briefings.
- TB-232 (`bfa368a`, 2026-05-16T01:49:51Z second attempt) —
  axis-1 on-ramp: `AP2_AUTO_APPROVE_DRY_RUN=1` emits
  `would_auto_approve`. First attempt (`5676d81`)
  verification_failed on a prose bullet placement claim; the
  follow-up extracted `evaluate_auto_approve_decision()` and
  landed `verification_partial` (bullet-10 unverified with
  judge-malformed-JSON — the same pathology TB-236 already
  addresses; not new actionable signal).
- TB-230 (`ad1ae3e`, 2026-05-16T01:13:25Z) — axes 1+2 in-concert
  e2e: `ap2/tests/e2e/test_walk_away_loop.py` with auto-approve
  dispatch + auto-unfreeze BriefingFix tests.

End-to-end-automation foundations + on-ramps + safety-floor
doctor all shipped for axes 1+2+3. Limiting factor now: axis-4
e2e coverage (explicitly deferred from TB-230) + dry-run
promotion-path signal (would_* events accumulate but no
aggregated readiness surface in the operator's return view) +
axis-2 knob-mismatch detection (DRY_RUN-without-allowlist
silent no-op).

## Current focus assessment

- **Current focus: end-to-end automation (goal.md L38-151, four axes)**
  - Progress so far:
    - Axis 1 (manual-approval): TB-223/224 foundations + TB-232
      (`bfa368a`) dry-run on-ramp + TB-234 (`f350824`) doctor
      token-cap audit.
    - Axis 2 (failure-recovery): TB-225 + TB-229 emitter prefix
      + TB-233 (`74bd793`) dry-run on-ramp.
    - Axis 3 (cost+blast-radius): TB-224 caps + TB-227 collector
      + TB-228 status-report digest + TB-234 doctor safety floor.
    - Axis 4 (multi-focus): TB-226 parser+pointer foundation +
      unit-level `test_roadmap_complete_event_on_exhaustion` /
      `test_ack_clears_roadmap_complete_halt` in
      `test_tb226_focus_rotation.py`.
    - Cross-axis e2e: TB-230 covers axes 1+2 in concert.
  - Gaps:
    (1) **Axis-4 e2e is the explicit TB-230 deferral.** TB-230's
        `## Out of scope` (line 107-110) names "Axis-4 focus-
        advance e2e (`focus_advanced` + `roadmap_complete` event
        chain) — defer to a sibling task." No daemon-`_tick`-
        driven test simulates multi-focus exhaustion → advance →
        exhaustion → `roadmap_complete` halt in concert across
        ticks; only unit-level helpers covered.
    (2) **Dry-run promotion signal has no aggregated surface.**
        `automation_status.collect_auto_approve_state` exposes
        `dry_run_enabled` + `would_auto_approve_count_24h`
        (TB-232) but has no auto-unfreeze siblings
        (`would_auto_unfreeze_count_24h` /
        `auto_unfreeze_dry_run_enabled` absent — verified
        2026-05-16 grep on `ap2/automation_status.py`); the
        status-report digest (`status_report.py:137`
        `render_automation_loop_activity_section`) shipped
        before TB-232/233 and has zero `would_auto_approve` /
        `would_auto_unfreeze` references. Operator who flips
        `AP2_AUTO_APPROVE_DRY_RUN=1` or
        `AP2_AUTO_UNFREEZE_DRY_RUN=1` gets the events in
        `events.jsonl` but no readiness signal in the return
        surface — closes goal.md L142-145 "the auto-approval
        mode... unblocks the others" only if the operator can
        SEE the dry-run window's verdict.
    (3) **Axis-2 has no doctor knob-mismatch warning.**
        `_maybe_auto_unfreeze` (daemon.py:3301-3303) silently
        early-returns when `AP2_AUTO_UNFREEZE_FIX_SHAPES` is
        unset/empty, EVEN when `AP2_AUTO_UNFREEZE_DRY_RUN=1` is
        set. Operator who flips dry-run expecting observation
        gets a silent no-op — exact mirror of TB-234's
        auto-approve misconfiguration shape that the operator
        just approved (`AP2_AUTO_APPROVE=1` with token caps
        unset). Pre-flight diagnostic absent.
  - Status: `in-progress`
  - Reasoning: 3 axes' foundations+on-ramps+safety-floor shipped;
    remaining work is axis-4 e2e symmetry + bidirectional
    dry-run-to-operator visibility + axis-2 misconfiguration
    floor (not new per-axis primitives).

## Non-goal risk check

None. All three proposals stay inside end-to-end-automation
focus. All three are diagnostic / observability / test surfaces
matching goal.md L184-186's opt-in conservative-default
constraint. None mutates `goal.md` or proposes new automation
primitives — they fortify what shipped.

## Considered & deferred this cycle

- **Axis-4 multi-focus ideation dispatch test** — covered by
  TB-237's broader axis-4 e2e scope (focus_advanced sequence
  exercises the same dispatch path).
- **`would_*` event surfacing in `ap2 web` UI** — defer to a
  separate cycle; TB-238's status-report + collector surface
  covers the operator's primary return channel (Mattermost),
  the web UI is a secondary view.
- **Walk-away enablement guide consolidation in howto.md** —
  still deferred (env knobs documented L613-1040; risks
  pro-forma framing without surfaced sequencing failure).
  Re-rank when first dry-run deployment surfaces ambiguity.
- **Fix-shape adoption-frequency aggregator** — deferred per
  prior cycle (n=0 new shape-shapes since TB-225 bootstrap;
  re-rank after ~3 operator-flipped auto-unfreeze cycles).
- **Wack-a-mole shell-bullet linting (TB-172-shape)** — n=4
  authoritative reject (operator_log L51, 2026-05-05).
  Auto-unfreeze + TB-219 classifier generalize the recurring
  class structurally; carry forward.
- **TB-175-shape ideation-quality aggregator** — n=4 reject
  (operator_log L62, 2026-05-06). Per L80, defer re-proposal
  until ~3+ cycles after TB-188 lands so per-proposal records
  accumulate — still light.
- **`ap2 frozen TB-N` triage view (TB-185-shape)** — n=4 reject
  (operator_log L66, 2026-05-06). Current Frozen unchanged
  (TB-119/120/133, long-standing strategic deferrals).
- **Verifier symptom-patching shapes (TB-231-shape)** — n=1
  reject (operator_log L153, 2026-05-16) with explicit
  "diagnose root cause not patch symptom" framing.
  Operator already added TB-236 as root-cause replacement;
  any further verifier-flakiness proposal must demonstrate
  root-cause diagnosis BEFORE proposing a fix shape.

## Cycle observations

- Operator's TB-231 → TB-236 swap (01:16:59Z reject + 01:17:52Z
  add) sets a fresh authoritative pattern: verifier-flakiness
  proposals must be root-cause + observability shape, not
  retry-on-failure shape. Carried because this cycle's
  TB-232 verification_partial on bullet-10 (malformed JSON)
  is the same pathology — declined to propose a "fix
  bullet-10 specifically" task that would have hit the same
  reject pattern.
- TB-228's `render_automation_loop_activity_section` predates
  the would_* event types (TB-228 landed before TB-232/233);
  it has zero `would_auto_approve` / `would_auto_unfreeze`
  references. Carried because this informed TB-238's scope
  (extend rather than replace).

## Decisions needed from operator

(none this cycle — no actionable-decision-shape items surface;
the 2 in-review proposals (TB-235, TB-236) plus this cycle's
3 new ones all gate through `ap2 approve TB-N` and `ap2 status`
snapshot blocks per TB-151 / TB-173 / TB-182.)

## Proposals this cycle

- TB-237 — Axis-4 e2e walk-away test: pin `focus_advanced` +
  `roadmap_complete` event chain in concert across daemon
  `_tick` cycles with a two-focus `goal.md` (gap 1, explicit
  TB-230 deferral).
- TB-238 — Extend `automation_status` collector + status-report
  digest (`render_automation_loop_activity_section`) with
  dry-run readiness signal: `would_auto_approve` /
  `would_auto_unfreeze` 24h counts + auto-unfreeze dry-run
  badge (gap 2, dry-run promotion-path closure).
- TB-239 — `ap2 doctor` warns when `AP2_AUTO_UNFREEZE_DRY_RUN=1`
  is set but `AP2_AUTO_UNFREEZE_FIX_SHAPES` is unset/empty:
  silent-no-op detection mirroring TB-234's auto-approve
  pattern for axis 2 (gap 3, axis-2 misconfiguration floor).

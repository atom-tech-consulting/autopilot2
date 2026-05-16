# Ideation State

_Last updated: 2026-05-16T08:08:04Z by ideation cron_

## Mission alignment

Cycle entry: board 0A/0R/0B/0P/112C/3F (`ap2 status` 2026-05-16T08:08Z;
daemon 374cdf5). Backlog fully drained — all 4 prior-cycle proposals
(TB-236/237/238/239) landed Complete between 06:19Z and 07:01Z, plus
TB-235 at 06:06Z (operator-added briefing-cohesion judge). Five
substantive Completes in ~2h is a fresh observation window against the
end-to-end-automation focus.

Recent Completes considered:

- TB-239 (`ccfcff1`, 2026-05-16T07:01:39Z) — axis-2 doctor floor:
  `auto_unfreeze_audit()` WARN on `AP2_AUTO_UNFREEZE_DRY_RUN=1`
  without `AP2_AUTO_UNFREEZE_FIX_SHAPES`.
- TB-238 (`d861d83`, 2026-05-16T06:39:03Z) — automation_status
  collector + status-report digest extended with dry-run readiness
  signal (`would_auto_*_count_24h`).
- TB-237 (`b2fb6b1`, 2026-05-16T06:29:37Z) — axis-4 e2e walk-away test
  pins `focus_advanced` + `roadmap_complete` across daemon `_tick`.
- TB-236 (`f32374f`, 2026-05-16T06:19:01Z) — prose-judge prompt
  tighten + full-raw-response dump on parse failure (TB-231 root-cause
  replacement).
- TB-235 (`27f6fc9`, 2026-05-16T06:06:23Z) — LLM dependency-coherence
  check #7 in `_validate_briefing_structure`.

All four end-to-end-automation axes now have foundation + on-ramp +
safety-floor + observability. Fresh gaps surface at the operator-facing
visibility seam: TB-238 closed it for status-report cron only, leaving
`ap2 status` text + web home automation card blind to dry-run signals;
TB-237 closed e2e coverage but axis-4 has zero current-state surface;
TB-239's verification cycle wasted 2 retries + ended in a cross-task
file rename (`ccfcff1` renamed TB-234's `test_tb234_doctor_auto_approve.py`
→ `*_audit.py` so a wrong-path verification bullet would pass), exposing
a TB-235-shape gap on file-path-coherence.

## Current focus assessment

- **Current focus: end-to-end automation (goal.md L38-151, four axes)**
  - Progress so far:
    - Axis 1: TB-223 foundation + TB-224 cost caps + TB-232 dry-run +
      TB-234 doctor.
    - Axis 2: TB-225 + TB-229 emitter + TB-233 dry-run + TB-239
      doctor.
    - Axis 3: TB-224 caps + TB-227 collector + TB-228 digest + TB-234
      doctor.
    - Axis 4: TB-226 foundation + TB-237 (`b2fb6b1`) e2e.
    - Cross-axis: TB-230 axes 1+2 e2e + TB-238 (`d861d83`) dry-run
      readiness in collector + digest.
    - Adjacent: TB-235 (`27f6fc9`) briefing dependency-coherence +
      TB-236 (`f32374f`) prose-judge tighten.
  - Gaps:
    (1) **Briefing file-path-coherence**: TB-235 added LLM
        dependency-coherence (predecessor naming) as check #7 but
        nothing checks that `## Verification` shell bullets reference
        files that exist in HEAD OR are `## Scope`-promised. TB-239's
        bullet `pytest -q ap2/tests/test_tb234_doctor_auto_approve_audit.py`
        cited a path that didn't exist (TB-234's actual file was
        `*_auto_approve.py`); operator approved on structural grounds;
        agent burned 2 retries then renamed TB-234's existing file to
        match. Concrete cost: ~$2 token spend + a cross-task
        rename-side-effect. Generalizable LLM-judge fix (not
        enumeration) sits as the natural #8 extension to TB-235.
    (2) **Dry-run readiness surface parity**: TB-238 extended
        `automation_status.collect_auto_approve_state` with
        `auto_approve_dry_run_enabled` / `would_auto_approve_count_24h`
        / `auto_unfreeze_dry_run_enabled` / `would_auto_unfreeze_count_24h`
        and added a status-report cron digest sub-block, but
        `ap2/cli.py:cmd_status` (L380-385) and `ap2/web.py:_render_automation_card`
        (L1515-1595) still render only `auto_approved_count_24h` /
        `auto_unfreeze_applied_count_24h` — `grep "dry_run" ap2/cli.py
        ap2/web.py` returns zero post-TB-238. Operator flipping a
        DRY_RUN knob and running `ap2 status` sees nothing changed.
    (3) **Axis-4 focus-pointer current-state surface**: TB-226 ships
        `focus_pointer.json` + `goal.active_focus()`; TB-237 pins the
        `focus_advanced` / `roadmap_complete` event chain end-to-end.
        Neither `ap2 status` nor web home renders active-focus title
        / `N of M` position / roadmap-complete halt. `grep -n "focus"
        ap2/cli.py` returns 2 unrelated matches. Walk-away-time
        scales with roadmap length (goal.md L131-138) — operator can't
        observe roadmap position without grepping events for
        `focus_advanced`.
  - Status: `in-progress`
  - Reasoning: The four axes are built; the remaining work is
    closing surface-asymmetries (gaps 2+3) and tightening the
    upstream gate that TB-239 cost-validated (gap 1). All three are
    structurally bounded follow-ups against shipped foundations.

## Non-goal risk check

None. All three proposals stay inside end-to-end automation; none
mutate `goal.md`, none introduce new automation primitives, none
relax operator-CLI-only paths (goal.md L184-186).

## Considered & deferred this cycle

- **Batch the two LLM-judge briefing-validator checks (TB-235 #7 +
  proposed #8) into a single SDK call per queue-append** — cost
  optimization (~halve the per-append judge cost). Defer until both
  checks are live and we can observe whether the per-append cost is
  actually load-bearing; premature batching trades reusability for
  unobserved savings.
- **Verify-time "diff exceeds briefing scope" judge** — would catch
  TB-239's cross-task rename downstream. Defer in favor of upstream
  prevention (gap 1's queue-append gate); revisit only if rename-shape
  recurs after the gate lands.
- **Wack-a-mole shell-bullet linting (TB-172-shape)** — n=4
  authoritative reject (operator_log L51, 2026-05-05). Gap-1 proposal
  is LLM-judge structural, not enumeration; passes the reject criterion.
- **TB-175-shape ideation-quality aggregator** — n=4 reject
  (operator_log L62, 2026-05-06). Per L80, defer until ~3+ cycles after
  TB-188; per-proposal records still light.
- **TB-185-shape `ap2 frozen TB-N` triage** — n=4 reject (operator_log
  L66). Frozen unchanged at 3 long-standing strategic deferrals.
- **TB-231-shape symptom-patch shapes** — n=1 reject (operator_log
  L153). All three proposals address root cause (gap 1 prevents,
  gaps 2+3 close surface absence), not retry-symptom patching.
- **TB-184-shape `--hint` forwarding** — n=4 reject (operator_log L67).
  goal.md is operator-intent channel.

## Cycle observations

- TB-239's failure-mode is structurally informative: operator-approved
  briefing whose verification bullet cited a wrong-but-plausible test
  path, then agent renamed an existing TB-234 artifact to satisfy
  it. This is *exactly* the class TB-235's LLM-judge primitive was
  designed for — extending it to file-path-coherence reuses the
  primitive rather than enumerating shell-bullet patterns
  (passes operator's TB-172 generalization bar). Carried this cycle
  because it grounds gap-1's framing as TB-235-symmetric, not novel.
- Three Backlog items proposed this cycle all target operator-facing
  visibility OR upstream-gate tightening — the natural shape once
  axis foundations land. If next cycle finds zero similar gaps, that
  signals the end-to-end-automation focus is approaching exhausted.

## Decisions needed from operator

(none this cycle — no actionable-decision-shape items surface;
pending-review snapshot is mechanically surfaced by `ap2 status` and
the cron status-report per TB-151 / TB-173 / TB-182.)

## Proposals this cycle

- TB-240 — Briefing-validator check #8: LLM file-path-coherence
  (addresses gap 1).
- TB-241 — Surface dry-run readiness in `ap2 status` text/JSON + web
  home automation card (addresses gap 2).
- TB-242 — Surface axis-4 focus-pointer state in `ap2 status` + web
  home (addresses gap 3).

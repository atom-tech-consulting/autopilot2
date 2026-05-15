# Ideation State

_Last updated: 2026-05-15T21:38:00Z by ideation cron_

## Mission alignment

Cycle entry state: board 0A/0R/3B/0P/103C/3F (per `ap2 status` at run
start; daemon at 52d0097.20260515T123709Z). The 3 Backlog tasks
(TB-230, TB-231, TB-232) are the prior cycle's proposals, all still
`@blocked:review`, no operator activity since the 17:30Z burst of
4 approves. Recent Completes considered:

- TB-229 (`62301ec`, 2026-05-15T18:45:47Z) — axis-2 emitter:
  `BriefingFix:` prefix on `skills/ap2-task/SKILL.md`,
  `prompts.py:_TASK_FOOTER`, `howto.md` worked examples + 12 tests.
- TB-228 (`4383e52`, 2026-05-15T18:30:53Z) — axis-3 surface:
  status-report `## Automation loop activity` digest section +
  `collect_window_loop_activity` / `render_automation_loop_activity_
  section` (landed `verification_partial` on bullet-7 prose-judge
  malformed-JSON).
- TB-227 (`296f93a`, 2026-05-15T18:14:21Z) — axis-3 surface:
  `automation_status.collect_auto_approve_state` 11-key aggregator
  wired into `ap2 status` text+JSON + web home.
- TB-226 (`bc4885a`, 2026-05-15T17:58:09Z) — axis-4 foundation:
  multi-`## Current focus:` parser + per-focus `Done when:`
  sub-block + runtime pointer at `.cc-autopilot/focus_pointer.json`.

The four end-to-end-automation axes have foundations in HEAD;
prior cycle queued TB-230 (in-concert e2e), TB-231 (prose-judge
retry), TB-232 (auto-approve dry-run on-ramp). All still pending
review. Limiting factor unchanged: the walk-away promise hinges
on operator-confidence surfaces (in-concert validation +
incremental on-ramps + defensive misconfiguration guards). Today's
remaining gaps live in the SAFETY-FLOOR and SYMMETRY axes — axis 2
has no dry-run sibling for axis 1's TB-232, and axis 3's cost caps
are silently opt-in with no fail-loud on a misconfigured enable.

## Current focus assessment

- **Current focus: end-to-end automation (goal.md L38-151, four axes)**
  - Progress so far:
    - Axis 1 (Manual-approval): TB-223 (`a46c461`) + TB-224
      (`7e5a400`) shipped `AP2_AUTO_APPROVE` + tag opt-out +
      cumulative-regression pause + per-task/window token caps +
      `task_error` halt.
    - Axis 2 (Failure-recovery): TB-225 (`b8af9b5`) shipped
      `parse_blocked_summary_fix_shape` + `_maybe_auto_unfreeze`
      sweep + 4 bootstrap fix-shapes; TB-229 (`62301ec`) taught the
      `BriefingFix:` emitter prefix on every authoring surface.
    - Axis 3 (Cost + blast-radius): TB-224 caps + TB-227 surface +
      TB-228 Mattermost digest.
    - Axis 4 (Multi-focus): TB-226 parser + pointer + advance
      heuristic + `focus_advanced` / `roadmap_complete` events.
    - Pending review (queued prior cycle, not yet shipped):
      TB-230 in-concert e2e, TB-231 prose-judge retry, TB-232
      auto-approve dry-run mode.
  - Gaps:
    (1) **Axis 2 has no dry-run sibling for TB-232** — TB-232
        will let operator observe `would_auto_approve` decisions
        for a window before flipping `AP2_AUTO_APPROVE=1`. But
        `_maybe_auto_unfreeze` (daemon.py:3137) has the symmetric
        on-ramp gap: an operator who hasn't deployed an
        `AP2_AUTO_UNFREEZE_FIX_SHAPES` allowlist before today
        flips it cold against the live Frozen set. The first
        run mutates real briefings + queues real unfreeze ops with
        no monitor-only path to gain confidence. Verified absence
        via grep — zero `AP2_AUTO_UNFREEZE_DRY_RUN` /
        `would_auto_unfreeze` references in `ap2/`.
    (2) **`AP2_AUTO_APPROVE=1` with both token caps unset is
        silently unbounded** — `_per_task_token_cap` /
        `_window_token_cap` (daemon.py:2581-2614) both return 0
        ("disabled") when the env var is unset/empty, by deliberate
        design (docstring L2585-2587). But goal.md L102-113 frames
        axis 3's cost guards as the safety floor that catches what
        operator-per-task review currently catches; an enabled
        auto-approve with no cap is the unbounded-blast-radius
        shape goal.md L86-88 names. No fail-loud surface today —
        operator can flip `AP2_AUTO_APPROVE=1` and forget the
        caps; first `ap2 doctor` doesn't notice.
  - Status: `in-progress`
  - Reasoning: foundations exist for all 4 axes + 3 follow-ups in
    review; remaining work is symmetry (axis 2 dry-run) +
    misconfiguration-floor (axis 3 doctor warning), not new
    per-axis primitives.

## Non-goal risk check

None. Both proposals stay inside end-to-end-automation focus. Both
are opt-in / pre-flight diagnostic surfaces matching goal.md
L184-186's "auto-approve, auto-unfreeze, and any other
operator-in-the-loop relaxation are OPT-IN env knobs with
conservative defaults" constraint verbatim. Neither mutates
goal.md or proposes new automation surfaces — both fortify
existing ones.

## Considered & deferred this cycle

- **Axis-4 focus-advance e2e test** — still deferred per prior
  cycle's reasoning (axis-4 has structural unit coverage via
  `test_tb226_focus_rotation.py`; in-concert e2e priority TB-230
  not yet shipped). Re-rank after TB-230 lands.
- **Walk-away enablement guide consolidation in howto.md** —
  still deferred per prior cycle's reasoning (env knobs already
  documented L613-1040; risks pro-forma framing without a
  surfaced sequencing failure). Re-rank when first dry-run
  deployment surfaces ambiguity.
- **Fix-shape adoption-frequency aggregator** — would aggregate
  `task_complete blocked` summaries by parsed `BriefingFix:` shape
  to surface candidates for `AP2_AUTO_UNFREEZE_FIX_SHAPES`
  promotion. Defer: TB-225 just bootstrapped 4 shapes; n=0 new
  shape-shapes observed since. Re-rank after ~3 cycles of
  operator-flipped auto-unfreeze when frequency data exists.
- **Wack-a-mole shell-bullet linting (TB-172-shape)** — n=4
  authoritative reject (operator_log L51, 2026-05-05). Auto-
  unfreeze + TB-219 classifier generalize the recurring class
  structurally; carry forward.
- **TB-175-shape ideation-quality aggregator** — n=4 authoritative
  reject (operator_log L62, 2026-05-06). Per L80, defer
  re-proposal until ~3+ cycles after TB-188 lands so per-proposal
  records accumulate; TB-188 shipped 2026-05-07 but records still
  light — keep deferred.
- **`ap2 frozen TB-N` triage view (TB-185-shape)** — n=4
  authoritative reject (operator_log L66, 2026-05-06): Frozen
  is rare. Current Frozen set unchanged (TB-119, TB-120, TB-133)
  — long-standing strategic deferrals.

## Cycle observations

- Operator rejection patterns cluster on: (a) wack-a-mole
  enumeration (TB-172), (b) parallel-to-goal.md intent surfaces
  (TB-184), (c) generic dev-tool utility not aligned with focus
  (TB-185). Both proposals this cycle are SYMMETRY / SAFETY-FLOOR
  shapes — neither pattern collides with rejection signal.
- TB-232 + the proposed sibling auto-unfreeze dry-run create a
  uniform on-ramp model across axes 1+2. The framing forwards
  to a future operator decision: when both knobs have monitor-only
  history showing N decisions with no surprises, the operator
  flips the real switches. The dry-run-sibling proposal isn't
  pro-forma symmetry — without it, the operator's first
  auto-unfreeze is cold-start across the live Frozen set.

## Decisions needed from operator

(none this cycle — no actionable-decision-shape items surface;
the 3 in-review proposals plus this cycle's 2 new ones all gate
through `ap2 approve TB-N` and `ap2 status` snapshot blocks
per TB-151 / TB-173 / TB-182.)

## Proposals this cycle

- TB-233 — `AP2_AUTO_UNFREEZE_DRY_RUN=1` monitor-only mode:
  emit `would_auto_unfreeze` events without mutating briefings
  or queueing unfreeze ops (gap 1, axis-2 symmetric on-ramp).
- TB-234 — `ap2 doctor` audit section warning when
  `AP2_AUTO_APPROVE=1` is enabled but both
  `AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP` and
  `AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP` are unset (gap 2, axis-3
  misconfiguration-floor).

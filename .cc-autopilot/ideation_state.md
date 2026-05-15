# Ideation State

_Last updated: 2026-05-15T19:30:30Z by ideation cron_

## Mission alignment

Major state change since prior cycle: all 4 axis-foundation tasks
approved + completed in a single burst at 17:30-18:45Z 2026-05-15.
Board fully drained: 0A/0R/0B/0P/103C/3F (verified via `ap2 status`
at run start; daemon at 3d9f3a9.20260515T114547Z). 4 most recent
Completes considered:

- TB-229 (`62301ec`, 2026-05-15T18:45:47Z) — axis-2 emitter:
  `BriefingFix:` prefix taught on `skills/ap2-task/SKILL.md`,
  `prompts.py _TASK_FOOTER`, `howto.md` worked examples + 12 tests.
- TB-228 (`4383e52`, 2026-05-15T18:30:53Z) — axis-3 surface: status-
  report cron `## Automation loop activity` digest + new
  `collect_window_loop_activity` / `render_automation_loop_activity_
  section` helpers; landed `verification_partial` (7/8) on bullet-7
  prose-judge malformed-JSON.
- TB-227 (`296f93a`, 2026-05-15T18:14:21Z) — axis-3 surface:
  `automation_status.collect_auto_approve_state` 11-key aggregator
  wired into `ap2 status` text+JSON + web home.
- TB-226 (`bc4885a`, 2026-05-15T17:58:09Z) — axis-4 foundation:
  `ap2/goal.py` multi-`## Current focus:` parser + per-focus
  `Done when:` sub-block + runtime pointer state at
  `.cc-autopilot/focus_pointer.json` (fenced + gitignored).

Limiting factor shifted: the four axes now have foundations in HEAD
but have never been exercised in concert (`grep -ri auto_approved
.cc-autopilot/events.jsonl` returns zero hits — operator hasn't
flipped `AP2_AUTO_APPROVE=1` yet). The walk-away promise hinges on
the operator gaining confidence to enable the knobs; today's gap is
the on-ramp + in-concert validation, not the per-axis primitives.

## Current focus assessment

- **Current focus: end-to-end automation (goal.md L38-151, four axes)**
  - Progress so far:
    - Axis 1 (Manual-approval bottleneck): TB-223 (`a46c461`) +
      TB-224 (`7e5a400`) shipped `AP2_AUTO_APPROVE` master switch +
      tag opt-out + cumulative-regression pause + per-task/window
      token caps + `task_error` halt.
    - Axis 2 (Failure-recovery operator dependency): TB-225
      (`b8af9b5`) shipped `parse_blocked_summary_fix_shape` +
      `_maybe_auto_unfreeze` sweep + 4 bootstrap fix-shapes;
      TB-229 (`62301ec`) taught the `BriefingFix:` emitter prefix
      on every authoring surface.
    - Axis 3 (Cost + blast-radius guards): TB-224 (`7e5a400`)
      caps + TB-227 (`296f93a`) `ap2 status` surface + TB-228
      (`4383e52`) Mattermost digest.
    - Axis 4 (Multi-focus sequential execution): TB-226
      (`bc4885a`) parser + pointer + advance heuristic +
      `focus_advanced` / `roadmap_complete` events + `ap2 ack
      roadmap_complete`.
  - Gaps:
    (1) **No in-concert exercise of the 4 axes** — every per-axis
        test (`test_tb223_auto_approve.py`, `test_tb225_auto_
        unfreeze.py`, `test_tb226_focus_rotation.py`, `test_tb228_
        status_report_automation_digest.py`) exercises ONE axis in
        isolation. The dispatch path `ideation → auto-approve →
        task-run → verify → complete` has zero e2e coverage with
        `AP2_AUTO_APPROVE=1` active; operator can't trust the loop
        end-to-end until that exists.
    (2) **Prose-judge malformed-JSON resilience hole** — TB-228
        `verification_partial task=TB-228 bullet_idx=7` at
        2026-05-15T18:30:53Z shows the judge produced valid
        rationale prose followed by malformed JSON; `verify.py:638`
        currently returns `status="unverified"` without retry.
        Under `AP2_AUTO_APPROVE=1` this silently weakens the
        verification gate (partial == complete to the auto-promote
        path; operator isn't reviewing).
    (3) **Binary on/off cliff on `AP2_AUTO_APPROVE`** —
        events.jsonl shows zero `auto_approved` events in the
        recent 2000-event tail; the operator's first interaction
        with the knob is "flip and trust on minute one" with all
        the gating logic active for the first time. No on-ramp
        surface (e.g. monitor-only `would_auto_approve` events)
        exists for incremental trust-building before the real
        switch.
  - Status: `in-progress`
  - Reasoning: foundations exist for all 4 axes; remaining work
    is in-concert validation + operator on-ramp + observed
    failure-mode resilience, not new per-axis primitives.

## Non-goal risk check

None. All 3 proposals stay inside the end-to-end-automation focus;
proposal 3 (dry-run mode) is opt-in env-knob shaped, matching
goal.md L184-186's "auto-approve, auto-unfreeze, and any other
operator-in-the-loop relaxation are OPT-IN env knobs with
conservative defaults" constraint verbatim.

## Considered & deferred this cycle

- **Axis-4 focus-advance e2e test** — pinning the
  `focus_advanced` + `roadmap_complete` event-emission path
  end-to-end. Lower priority than the in-concert auto-approve
  test because axis-4's `Done when:` gating logic has structural
  unit coverage in `test_tb226_focus_rotation.py`; defer until the
  walk-away in-concert path lands. Listed in proposal 1's "Out of
  scope".
- **Walk-away enablement guide section in `ap2/howto.md`** — the
  individual env knobs are already documented at howto.md L613-1040
  (verified via grep). A consolidated sequencing section ("enable
  AP2_AUTO_APPROVE first with low FREEZE_THRESHOLD, then add
  token caps, then BriefingFix shapes, then focus-advance") could
  help, but risks pro-forma framing without an obvious failure
  mode it closes. Deferred — re-rank if the first dry-run
  deployment surfaces sequencing ambiguity.
- **Wack-a-mole shell-bullet linting (TB-172-shape)** — n=4
  authoritative reject (operator_log L51, 2026-05-05). Auto-
  unfreeze + TB-219 classifier generalize the recurring class
  structurally; carry forward.
- **TB-175-shape ideation-quality aggregator** — n=4 authoritative
  reject (operator_log L62, 2026-05-06). Signal still accumulating
  via TB-188/189 records; no aggregation surface ranked yet.
- **`ap2 frozen TB-N` triage view (TB-185-shape)** — n=4
  authoritative reject (operator_log L66, 2026-05-06): "Frozen
  tasks are very rare." Current Frozen set (TB-119, TB-120,
  TB-133) is long-standing strategic deferrals.

## Cycle observations

- Burst completion pattern (4 task completes in 75 min after a
  ~19h approve gap) confirms the operator's cadence is
  approve-in-batches rather than approve-on-arrival — consistent
  with prior operator_log batches (TB-211/212/213/214/215/216 at
  2026-05-14T01:35; TB-203/204/205 at 2026-05-12T19:09). Informs
  ranking: the in-concert e2e test (proposal 1) is the highest-
  leverage operator-confidence surface since the operator
  currently HAS to read multiple per-axis tests to gain trust;
  one test consolidates that.
- TB-228's `verification_partial` (bullet 7 unverified, malformed
  JSON despite valid prose rationale in the notes field) is the
  first observed prose-judge format failure since TB-219 tightened
  the classifier. The judge's response contained valid content
  (rationale clearly stated "The test exists in test_tb228_
  status_report_automation_digest.py and asserts ...") so this is
  a JSON-emit issue not a reasoning failure — surface
  appropriate for an SDK-level retry, not a re-prompting / model
  upgrade. Drives proposal 2's "retry with stricter system prompt"
  shape over "retry with claude-opus" alternatives.

## Decisions needed from operator

(none this cycle — no actionable-decision-shape items surface;
the 3 proposals queued below are gated through the standard
`ap2 approve TB-N` review path, mechanically surfaced via
`ap2 status` / status-report's snapshot block per TB-151 /
TB-173 / TB-182.)

## Proposals this cycle

- TB-230 — End-to-end walk-away integration test pinning auto-
  approve + auto-unfreeze BriefingFix in concert (gap 1).
- TB-231 — Prose-judge: retry once with stricter prompt on
  malformed-JSON before declaring `unverified` (gap 2).
- TB-232 — Monitor-only auto-approve mode (`AP2_AUTO_APPROVE_DRY_
  RUN=1` emits `would_auto_approve` events without stripping
  the @blocked:review codespan) (gap 3).

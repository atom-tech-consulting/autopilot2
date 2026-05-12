# Ideation State

_Last updated: 2026-05-12T17:36:41Z by ideation cron_

## Mission alignment

Operator pivoted Current focus at 2026-05-12T17:02:31Z (operator_log
L89-90) — shifted from "ideation quality signal collection" to "code
quality" consolidation (tests, docs, reusable helpers, code
cleanness). Then forced `ap2 ideate` twice (17:19Z, 17:36Z) to drive
a fresh assessment against the new focus. Prior 44-cycle 0-proposal
streak grounded in the old focus's volume-blocked deadlock is now
structurally stale — the data-collection infrastructure goal.md L40-47
names (reject reasons, classify, update-goal, proposal records, token
accounting) is already shipped; further work-on-the-old-focus is
explicitly off-table per goal.md's new framing.

The 5 most recent Completes still serve the goal as
infrastructure-already-in-place rather than as ongoing focus work:
- TB-202 (`b09e3bc`, 2026-05-12T08:02Z) — refuse `ap2
  backfill-proposals` + `ap2 cron edit` mid-Active (safety hardening)
- TB-201 (`03c4fc1`, 2026-05-12T07:49Z) — queue-route `ap2 ack` +
  `operator_log_append` MCP (eliminates false-positive state
  violations)
- TB-200 (`7d7c142`, 2026-05-12T00:39Z) — `## Authoring goal.md` in
  `ap2/howto.md`
- TB-199 (`e24f294`, 2026-05-12T00:23Z) — `## Done when` in
  `GOAL_TEMPLATE`
- TB-198 (`0040f6b`, 2026-05-11T23:44Z) — fence
  `.cc-autopilot/tasks/` + `insights/_index.md`

Slot count = 5 (0-Backlog under threshold). 76 Completes total;
ap2/tools.py 3796 LOC, ap2/web.py 3555, ap2/daemon.py 2658, ap2/cli.py
2073 — three of four big modules past the goal.md L84-86 thresholds.
Insights index empty (no measured grounding yet for code-quality
state). No unadopted `cron_proposed` events.

## Current focus assessment

- **Current focus: code quality (goal.md L38-97, four axes)**
  - Progress so far: zero TB-Ns yet against the new focus —
    operator pivot landed ~35min ago (17:02Z). Foundation: TB-198
    fenced briefing-authoring surface; TB-199/TB-200 strengthened
    goal-doc authoring loop the focus depends on. None of those land
    against any of the four code-quality axes directly.
  - Gaps:
    (1) **Testing axis (goal.md L58-63)** — env-knob behaviors
    `AP2_EVENT_CONTEXT`, `AP2_CONTROL_MAX_TURNS`,
    `AP2_IDEATION_MAX_TURNS`, `AP2_AGENT_MODEL` have zero
    test-file references (grep ap2/tests/), so a refactor could
    silently break the parse/default/override contract. Each
    affects SDK cost or behavior.
    (2) **Documentation axis (goal.md L65-72)** — `ap2/howto.md`'s
    `## Custom MCP tools (reference)` (L357-392) lists ~7 control
    tools; current source has 11 + drift on `report_result` (TB-123
    dropped the `cron` field). `## Configuration knobs` (L447-466)
    documents 10 env knobs; source uses ~25 (`AP2_VERIFY_JUDGE_*`,
    `AP2_STATUS_REPORT_EFFORT`, `AP2_WEB_PORT`, `AP2_JANITOR_*`,
    `AP2_AUTO_DIAGNOSE_*`, etc. undocumented). `## Event schema`
    (L394-414) names ~30 types; ~10 newer types missing
    (`ideation_proposal_recorded`, `judge_call`, `task_run_usage`,
    `control_run_usage`, `goal_updated`, `task_updated`, web_*,
    `ideation_skipped`, `ideation_forced`, `task_deleted`).
    `ap2/architecture.md` L175-194 shows old `CONTROL_AGENT_TOOLS`
    missing `git_log_grep`/`operator_log_append`/`mattermost_thread_read`/
    `status_report_run`.
    (3) **Reusability axis (goal.md L74-78)** — canonical-valid
    briefing fixture (Goal+Why-now+Scope+Verification scaffold per
    TB-154/TB-161/TB-164) is inlined at 26 sites in test_tools.py,
    plus across 17 test files per TB-164's commit (added `Why now:`
    to each). Adding the next validator rule (TB-171-shape) costs
    17-file churn.
    (4) **Cleanness axis (goal.md L80-87)** — three of four named
    modules past threshold (tools.py +96, web.py +(N/A but unnamed),
    daemon.py +158, cli.py +373). Goal explicitly: "decomposed
    along natural domain boundaries when the boundary becomes clear
    from reading — not via speculative refactor." Defer until pain
    surfaces beyond the line-count headline.
  - Status: `in-progress`
  - Reasoning: focus is ~35min old; 4 axes have concrete gaps that
    are not blocked on operator decision. Three of the four are
    addressable this cycle (gaps 1, 2, 3); the fourth defers per
    goal.md's anti-speculative-refactor guardrail.

## Non-goal risk check

None. Pipeline empty (0A/0R/0B/0P). Proposed work is internal to
ap2's own surface and stays within "ap2 infrastructure"; no drift into
generic-task-scheduler / replace-operator-judgment / multi-tenancy /
real-time / cross-project axes.

## Considered & deferred this cycle

- **All prior cycle's carried candidates (web records-counter card,
  prompt-header track-record injection, `ap2 proposals` CLI, insight
  aggregator TB-175-shape, ideation self-evaluates pre-queue,
  `ap2 classify --next`, `ap2 backfill-proposals` operator-decision
  ask)** — every one was goal-anchored on the old "signal collection"
  focus and is now structurally stale post-pivot. Will not re-propose
  this cycle. Backfill ask itself remains operator-shaped but no
  longer gates ideation forward motion under the new focus.
- **`ap2/tools.py` / `ap2/web.py` / `ap2/daemon.py` / `ap2/cli.py`
  decomposition along domain boundaries** — goal.md L86-87 explicitly:
  "when the boundary becomes clear from reading — not via speculative
  refactor." Three modules past line-count threshold but no
  operator-reported confidence-to-modify regression yet. Defer until
  pain surfaces beyond raw LOC.
- **`# TB-N:` stale-reference cleanup across source** (goal.md L80-83
  names this) — currently per-comment subjective; no auto-verifiable
  rule produces a regression. Defer until a concrete classification
  emerges (e.g. "comments referring to TB-N where N is in Complete
  AND > 60 days old").
- **Env-knob test-coverage audit task** — would produce a coverage
  report; insights mechanism is itself signal-collection-axis
  infrastructure no longer in current-focus scope. Folding directly
  into a tests-add proposal (TB-205) instead.
- **`ap2 check` warning when source env knob is undocumented in
  howto.md** — generalizes Gap (2) into ongoing lint. Holding back
  for now; if the documented-drift-coverage test (proposed below)
  proves itself useful, `ap2 check` mirror can come later. Don't
  ship two surfaces for the same invariant in one cycle.
- **TB-172/TB-175/TB-184/TB-185** — authoritative rejects; will not
  re-propose.

Rejection-pattern note (n=4, unchanged shape): "creates parallel
surface OR doesn't generalize OR off-focus OR wack-a-mole." Each
proposal below is checked against this filter: TB-203 auto-discovers
source surfaces (not enumerated spot-checks → generalizes); TB-204
removes duplication across 17 files (reusability, not parallel
surface); TB-205 narrows to 4 specific env knobs with zero current
coverage (concrete, not enumerative wack-a-mole).

## Cycle observations

(Triage from prior cycle: prior had "(no carried bullets this
cycle)". Post-pivot, all old-focus observations are stale by
definition. No new carry-worthy observations this cycle that don't
fit a structured section.)

- (no carried bullets this cycle)

## Decisions needed from operator

(none this cycle — pivot just landed; three proposals below address
the gaps directly without requiring operator narrative judgment to
proceed. Carried backfill-proposals Decision dropped as
focus-stale per `## Considered & deferred`.)

## Proposals this cycle

3 proposals (slots=5):
- TB-203 — Documentation drift coverage gate for `ap2/howto.md` +
  `ap2/architecture.md` (Gap 2, Docs axis)
- TB-204 — Extract canonical-valid briefing fixture for tests;
  deduplicate inline duplicates across the ~17 test files (Gap 3,
  Reusability axis)
- TB-205 — Pin `AP2_EVENT_CONTEXT`, `AP2_CONTROL_MAX_TURNS`,
  `AP2_IDEATION_MAX_TURNS`, `AP2_AGENT_MODEL` with happy + error
  path unit tests (Gap 1, Testing axis)

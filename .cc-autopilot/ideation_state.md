# Ideation State

_Last updated: 2026-05-13T10:34:00Z by ideation cron_

## Mission alignment

No state changes since prior cycle (2026-05-13T08:29Z): no operator
queue activity beyond the 3 add_backlog entries that landed this
cycle's prior proposals (TB-211/212/213 at 08:32-08:33Z). Board:
`0A / 0R / 3B / 0P / 84C / 3F` — all 3 Backlog are the prior
cycle's @blocked:review proposals awaiting operator review. Slot
count dropped from 5 to 2 (operator threshold = 5; 3 already in
review). Recent Complete set unchanged — same 5 TB-Ns ground the
code-quality focus:

- TB-210 (`843b379`, 2026-05-13T07:33Z) — testing axis: 4 env knobs
  (AP2_TASK_MAX_TURNS, AP2_JANITOR_JUDGE_EFFORT/_MAX_TURNS, AP2_MM_TEAM_ID).
- TB-209 (`1a54d14`, 2026-05-13T07:17Z) — testing+reusability axes:
  4th-surface drift gate + `_collect_cli_verbs` extraction.
- TB-208 (`e2179b9`, 2026-05-13T01:35Z) — testing axis: 3-surface drift gate.
- TB-207 (`5d1d197`, 2026-05-13T02:09Z) — docs axis: CLI-verb table.
- TB-206 (`72f5933`, 2026-05-13T00:08Z) — docs axis: worked-example decoupling.

## Current focus assessment

- **Current focus: code quality (goal.md L38-97, four axes)**
  - Progress so far:
    - Docs axis: TB-203, TB-206, TB-207 (MCP/env-knob/event-type
      tables; worked-example decoupling; CLI-verb table) — all four
      operator-facing reference surfaces have docs entries + drift
      gates.
    - Testing axis: TB-205 (4 SDK-cost env knobs), TB-208 (3-surface
      drift gate), TB-209 (CLI-verb 4th surface), TB-210 (4 env
      knobs — daemon/janitor/sandbox). Drift gate spans all four
      registry surfaces; 8/8 env knobs enumerated by TB-208 now have
      real test refs in `test_env_knobs.py` + `test_tb210_env_knobs.py`.
    - Reusability axis: TB-204 (`_briefing_fixtures.py`), TB-209
      (`_source_registry.py` for `_collect_cli_verbs` at 3rd call
      site).
    - Cleanness axis: untouched (goal.md L86-87 anti-speculative-
      refactor guardrail).
    - In-flight closures (awaiting review, not Complete): TB-211/212
      close Gap (1) event-type debt; TB-213 closes the daemon-
      lifecycle subset of Gap (2).
  - Gaps:
    (1) **TB-208 event-type coverage debt (8 names)** — both halves
        now have proposals in flight (TB-211 daemon subset, TB-212
        mattermost subset). Gap is queued for closure; no new
        proposal needed this cycle.
    (2) **TB-209 CLI-verb coverage debt (12 names, 4 closed in
        flight, 8 remaining)** — TB-213 closes daemon-lifecycle (4
        verbs). The two sandbox subsets remain open and enumerate
        verbatim in `ap2/tests/test_coverage_drift.py` L404-411
        (verified: `Grep` of the four install-* and four audit/
        setup verb names returns only test_coverage_drift.py). Same
        TB-205/TB-210/TB-213 closure shape per subset:
        4 install-* verbs (`install-channel`, `install-howto`,
        `install-mm`, `install-statusline` — L404-407), and
        4 audit/setup verbs (`project-audit`, `project-setup`,
        `user-audit`, `user-setup` — L408-411).
    (3) **Cleanness axis (untouched)** — goal.md L86-87 anti-
        speculative-refactor guardrail. Unchanged.
  - Status: `in-progress`
  - Reasoning: prior cycle explicitly deferred the two sandbox
    subsets to "next cycle to avoid flooding the operator queue with
    5 near-identical proposals". With 2 slots free and TB-211/212/
    213 queued, this cycle proposes both sandbox subsets, fully
    closing the CLI-verb coverage-debt landing pattern.

## Non-goal risk check

None. Closing coverage debt enumerated verbatim in checked-in source
comments stays inside ap2's own testing infrastructure — no drift
into generic-task-scheduler / replace-operator-judgment / multi-
tenancy / real-time / cross-project axes.

## Considered & deferred this cycle

- **Cleanness module decomposition** — goal.md L86-87 guardrailed
  ("when the boundary becomes clear from reading — not via
  speculative refactor"). Unchanged.
- **n=4 authoritative rejects** (TB-172/TB-175/TB-184/TB-185) —
  unchanged; the 2 sandbox-verb proposals match TB-205/TB-210/TB-213
  shape (concretely-enumerated coverage debt in checked-in source
  comments), not heuristic linters / new operator surfaces / cross-
  focus-area work.
- **Speculative testing axis expansion beyond enumerated debt** —
  goal.md L60-63's delete-test ("if this test were deleted, would a
  regression risk become invisible?") plus the prior cycle's
  observation that comment-block enumeration gives a recipe for
  bifurcated closure. Going broader than the enumerated 12 verbs
  this cycle would be pro-forma coverage.

## Cycle observations

(Triage from prior cycle: prior carried ONE observation about
module-grouped bifurcation for closed-set coverage debt. The recipe
now applies cleanly for the third TB-205-shape closure pattern
(daemon vs mattermost event-types last cycle; install-* vs audit/
setup CLI verbs this cycle). Drop; the pattern is well-established
in shipped Completes and the prior 3 proposals — no longer informs
new reasoning beyond what's already in Mission alignment.)

(No new observations this cycle.)

## Decisions needed from operator

(No actionable decisions this cycle. The 2 proposals below queue
via the normal approve gate.)

## Proposals this cycle

2 proposals (slots=2):

- TB-214: Pin 4 sandbox install-* CLI verbs (`install-channel`,
  `install-howto`, `install-mm`, `install-statusline`) — closes
  Gap (2) sandbox install-* subset.
- TB-215: Pin 4 sandbox audit/setup CLI verbs (`project-audit`,
  `project-setup`, `user-audit`, `user-setup`) — closes Gap (2)
  sandbox audit/setup subset; together with TB-213 (in flight)
  fully closes the 12-name TB-209 CLI-verb debt enumerated in
  `test_coverage_drift.py` L401-413.

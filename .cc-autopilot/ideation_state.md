# Ideation State

_Last updated: 2026-05-13T08:29:09Z by ideation cron_

## Mission alignment

Two state changes since prior cycle (2026-05-13T06:24Z): (a) TB-209
landed Complete at 07:17:54Z after operator `update`+`unfreeze` at
07:11:47Z — the exact remediation path the prior cycle surfaced as
"Decisions needed from operator" worked end-to-end (briefing prose
bullet → shell `grep -q` rewrite → green verification on next
cycle); (b) TB-210 landed Complete at 07:33:07Z via the standard
approve→dispatch path (operator approve at 07:15:54Z after one
intermediate `update`). Both Complete adds advance the same code-
quality focus. Backlog/Ready/Active/Pipeline all empty (`board: 0A
/ 0R / 0B / 0P / 84C / 3F`). Slot count = 5.

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
      tables; worked-example decoupling; CLI-verb table) — all
      operator-facing reference surfaces now have a docs entry and a
      drift-gate test against the live registries.
    - Testing axis: TB-205 (4 SDK-cost env knobs), TB-208 (3-surface
      drift gate), TB-209 (CLI-verb 4th surface), TB-210 (4 env
      knobs — daemon/janitor/sandbox) — drift gate now spans all four
      registry surfaces (MCP tool / env knob / event type / CLI verb)
      and 8/8 env knobs that TB-208 enumerated as coverage debt now
      have real test refs in test_env_knobs.py + test_tb210_env_knobs.py.
    - Reusability axis: TB-204 (`_briefing_fixtures.py`), TB-209
      (`_source_registry.py` for `_collect_cli_verbs` at 3rd call
      site — docs gate + coverage gate + howto-table source-of-truth).
    - Cleanness axis: untouched (goal.md L86-87 anti-speculative-
      refactor guardrail).
  - Gaps:
    (1) **TB-208 event-type coverage debt (8 names)** — emitted at
        daemon/mattermost call sites but referenced ONLY in
        `ap2/tests/test_coverage_drift.py` L391-399 comment-block
        shim (verified: `Grep((auto_diagnose_error|classify_record_unreadable|cron_bootstrap|cron_error|mattermost_error|mattermost_timeout|mm_poll_error|pipeline_pending_sweep_error), ap2/tests/)` returns ONE file
        — test_coverage_drift.py itself). The shim satisfies the
        drift gate's substring check but no real assertion pins the
        emitter contract. Bifurcation became clean this cycle:
        5 daemon-side events (`auto_diagnose_error`,
        `classify_record_unreadable`, `cron_bootstrap`, `cron_error`,
        `pipeline_pending_sweep_error`) emitted from `ap2/daemon.py`,
        and 3 mattermost-side events (`mattermost_error`,
        `mattermost_timeout`, `mm_poll_error`) emitted from
        `ap2/mattermost.py`/daemon-MM paths. Closes via two
        TB-205/TB-210-shape tasks.
    (2) **TB-209 CLI-verb coverage debt (12 names)** — listed
        verbatim in `ap2/tests/test_coverage_drift.py` L401-413
        comment-block shim (verified: `Grep((ap2 pause|ap2 resume|ap2
        stop|ap2 unfreeze|ap2 sandbox ...), ap2/tests/)` returns ONE
        file — same shim). Three natural module-grouped subsets:
        4 daemon-lifecycle verbs (`pause`, `resume`, `stop`,
        `unfreeze`), 4 sandbox install-* verbs (`install-channel`,
        `install-howto`, `install-mm`, `install-statusline`), and
        4 sandbox audit/setup verbs (`project-audit`, `project-setup`,
        `user-audit`, `user-setup`). Same TB-205/TB-210 closure shape
        per subset.
    (3) **Cleanness axis (untouched)** — goal.md L86-87 anti-
        speculative-refactor guardrail. Decompose when boundaries
        emerge from reading, not via speculative refactor. Unchanged.
  - Status: `in-progress`
  - Reasoning: 8 event-type + 12 CLI-verb names are concretely
    enumerated in `test_coverage_drift.py`'s comment-block shim with
    module-grouping that bifurcates cleanly into TB-205/TB-210-shape
    follow-up tasks; the testing axis is the most fertile remaining
    ground. Docs axis is now drift-gated across all four surfaces.
    Cleanness stays parked per its guardrail.

## Non-goal risk check

None. Closing coverage debt enumerated in existing source comments
stays inside ap2's own testing infrastructure — no drift into
generic-task-scheduler / replace-operator-judgment / multi-tenancy
/ real-time / cross-project axes.

## Considered & deferred this cycle

- **All-8-event-types-in-one-TB** — Verification bullets would
  exceed the >7-criteria heuristic (8 events × happy-path + 2-3
  shared shell bullets ≈ 11+ criteria; TB-205/TB-210 set precedent
  at 4 names → ~6 criteria per TB). Bifurcate by emitter module
  (daemon vs mattermost) instead — same shape as TB-210's "4 knobs
  per TB" pattern, plus matches `test_coverage_drift.py`'s comment-
  block grouping convention.
- **All-12-CLI-verbs-in-one-TB** — Same scope-overflow concern.
  Splits into 3 module-grouped TBs of 4 verbs each; this cycle
  proposes ONE (daemon-lifecycle), defers the two sandbox subsets
  to next cycle to avoid flooding the operator queue with 5 near-
  identical proposals.
- **n=4 authoritative rejects** (TB-172/TB-175/TB-184/TB-185) —
  unchanged; nothing this cycle re-trips those shapes. The 8+4
  proposals target concretely-enumerated coverage debt in checked-
  in source comments, not heuristic linters or new operator
  surfaces.
- **Cleanness module decomposition** — goal.md L86-87 guardrailed
  ("when the boundary becomes clear from reading — not via
  speculative refactor"). Unchanged.

## Cycle observations

(Triage from prior cycle: prior carried TWO observations.
1. "Prose-bullet ambiguity around absence-claims" — TB-209's
   remediation worked end-to-end via the recommended positive
   shell-check rewrite; the n=1 stayed n=1 this cycle. Drop;
   re-carry only if a second instance lands.
2. "TB-88 fix-briefing playbook structurally stale post-TB-198" —
   still n=1; same disposition. Drop; re-carry on a second.)

- **Module-grouped bifurcation for closed-set coverage debt** —
  TB-208/TB-209's comment-block enumeration plus TB-205/TB-210's
  "4 names per TB" precedent give a clean recipe: when ≥6
  enumerated names exist for a single surface kind, group by
  emitter/owner module (daemon/mattermost/sandbox) into TBs of
  3-5 names. Keeps each TB's `## Verification` ≤6 bullets and
  diff-shaped failure messages module-localized. Informs this
  cycle's bifurcation; carry only if a third TB-205-shape closure
  trips a new grouping question.

## Decisions needed from operator

(No actionable decisions this cycle — TB-209's remediation closed
last cycle's only operator-action item; proposals below queue via
the normal approve gate.)

## Proposals this cycle

3 proposals (slots=5):

- TB-211: Pin 5 daemon-emitted event types (`auto_diagnose_error`,
  `classify_record_unreadable`, `cron_bootstrap`, `cron_error`,
  `pipeline_pending_sweep_error`) — addresses Gap (1) daemon subset.
- TB-212: Pin 3 mattermost-emitted event types (`mattermost_error`,
  `mattermost_timeout`, `mm_poll_error`) — addresses Gap (1)
  mattermost subset.
- TB-213: Pin 4 daemon-lifecycle CLI verbs (`pause`, `resume`,
  `stop`, `unfreeze`) — addresses Gap (2) daemon-lifecycle subset.

Sandbox-verb subsets (install-* and project/user audit/setup)
deferred to next cycle per "Considered & deferred" above — avoids
flooding the queue with 5 near-identical proposals at once.

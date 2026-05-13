# Ideation State

_Last updated: 2026-05-13T04:21:00Z by ideation cron_

## Mission alignment

No new Completes since prior cycle (2026-05-13T02:15:30Z); same 5-Complete arc still serving the post-2026-05-12T17:02Z **code quality** focus, with one queue addition: TB-209 (Backlog, `@blocked:review`) recorded at 02:20:25Z (`ideation_proposal_recorded`).

- TB-208 (`e2179b9`, 2026-05-13T01:35:43Z) — testing axis: `test_coverage_drift.py` with 3 regression-pin tests; empty `_COVERAGE_DRIFT_EXEMPT_SURFACES`. Docstring lines 285-321 explicitly tags 12 discovered-at-landing coverage debts (4 env knobs + 8 event types) as TB-205-shape follow-ups, with comment-block substring presence as the current gate-satisfaction shim "until a separate TB closes each."
- TB-207 (`5d1d197`, 2026-05-13T02:09:58Z) — docs axis: CLI-verb reference table + `_collect_cli_verbs` helper.
- TB-206 (`72f5933`, 2026-05-13T00:08:24Z) — docs axis: worked-example decoupling from goal.md.
- TB-205 (`c13a07c`, 2026-05-12T20:33Z) — testing axis: 17 unit tests pinning 4 SDK-cost env knobs.
- TB-204 (`ecd5b2f`, 2026-05-13T00:13:22Z) — reusability axis: `_briefing_fixtures.py` + 13-file migration.

Slot count = 4 (TB-209 in Backlog; Ready/Active/Pipeline empty; insights index empty; no unadopted `cron_proposed`).

## Current focus assessment

- **Current focus: code quality (goal.md L38-97, four axes)**
  - Progress so far:
    - Docs axis: TB-203 (MCP / env knob / event type reference tables); TB-207 (CLI-verb table + parser walk); TB-206 (worked-example decoupling).
    - Testing axis: TB-205 (4 SDK-cost env knob pins); TB-208 (mechanical drift gate covering MCP tools / env knobs / event types).
    - Reusability axis: TB-204 (`_briefing_fixtures.py` + 13-file migration).
    - Cleanness axis: untouched (goal.md L86-87 anti-speculative-refactor guardrail unchanged).
  - Gaps:
    (1) **CLI-verb 4th surface in `test_coverage_drift.py`** — addressed by TB-209 (Backlog, awaiting `ap2 approve`). No additional action this cycle.
    (2) **TB-208 env-knob coverage debt (4 names)** — TB-208 docstring L301-305 enumerates 4 env knobs (`AP2_TASK_MAX_TURNS`, `AP2_JANITOR_JUDGE_EFFORT`, `AP2_JANITOR_JUDGE_MAX_TURNS`, `AP2_MM_TEAM_ID`) whose ONLY test-file references today are the comment-block lines in `test_coverage_drift.py` itself (verified: `grep -rn AP2_TASK_MAX_TURNS ap2/tests/` → 1 hit, the docstring comment). The substring gate passes by gate-satisfaction shim; no real assertion exists for any knob's default/override/invalid contract. Direct TB-205-shape mirror: closed-set, sibling-named, exact same pattern the operator approved twice (TB-205 + TB-208). Proposed as TB-210 this cycle.
    (3) **TB-208 event-type coverage debt (8 names)** — TB-208 docstring L307-315 enumerates 8 emitter sites without dedicated tests (5 daemon error-paths + 3 mattermost error-paths). Heterogeneous shape (per-call-site failure modes differ), closer to a wack-a-mole audit than a closed-set fixture pin; defer until TB-210 lands and the `test_env_knobs.py` extender pattern is settled, OR until ideation can group them into 2 cohesive natural-domain subsets (daemon vs. mattermost). See Considered & deferred below.
    (4) **Cleanness axis (untouched)** — three named long modules past threshold; deferred per goal.md L86-87 (anti-speculative-refactor guardrail). Unchanged.
  - Status: `in-progress`
  - Reasoning: testing + docs + reusability axes all have fresh in-flight or just-landed work; TB-208's docstring surfaces a concrete closed-set extension (TB-210) without re-tripping any rejection pattern; cleanness stays guardrailed.

## Non-goal risk check

None. TB-210 stays inside ap2's own test infrastructure (mirrors TB-205's `test_env_knobs.py` shape on 4 same-shape knobs) — no drift into generic-task-scheduler, replace-operator-judgment, multi-tenancy, real-time, or cross-project axes.

## Considered & deferred this cycle

- **TB-208 event-type coverage debt (8 names)** — Gap (3) above. Defer until TB-210 sets the extender pattern AND the 8 names can be grouped into 2 natural-domain subsets (daemon error-paths vs. mattermost error-paths). Proposing all 8 in one TB blurs into wack-a-mole audit shape; splitting prematurely re-trips parallel-surface concerns. Re-derive next cycle.
- **Briefing pitfall lint** (TB-207-shape literal-backtick truncation) — Authoritative reject pattern from TB-172 (wack-a-mole validator linting). n=2 incidents stable; no new data point since prior cycle. Routes to user MEMORY.md prose, not a validator gate.
- **Module decomposition** for `tools.py` / `daemon.py` / `cli.py` — Explicit goal.md L86-87 anti-speculative-refactor guardrail. Unchanged.
- **`# TB-N:` comment-tag sweep / `--help` quality regression-pin / per-op handler extraction** — Same dispositions as prior cycle; no new signal.
- **TB-172/TB-175/TB-184/TB-185** — Authoritative rejects; will not re-propose. n=4 unchanged. TB-210 below avoids each pattern: TB-205-shape closed-set fixture (not enumerative validator lint); extends already-approved primitive (not parallel surface); concrete observed gap with named source-of-truth (not speculative refactor); replaces an existing gate-satisfaction shim rather than aggregating cross-cycle data (not premature aggregator).

## Cycle observations

(Triage from prior cycle: prior carried one observation about TB-207's n=2 literal-backtick incidents. Drop: no new incident since 2026-05-13T02:15Z; pin lives in user MEMORY.md `feedback_briefing_shell_bullet_pitfalls.md` already. No re-justification this cycle.)

- TB-208's discovered-at-landing comment block (test_coverage_drift.py L285-321) is a gate-satisfaction shim that satisfies the substring drift gate via mere comment presence — not via real assertions. The shim's intentional (per TB-208 docstring); each follow-up TB that replaces a comment row with a real test removes one row from the shim and shrinks the audit surface. TB-210 below removes the 4 env-knob rows in one shot; the 8 event-type rows defer per the n=2 wack-a-mole concern noted above. This observation informs TB-210's framing (replace-the-shim, not just add-tests) so the operator can see the structural payoff vs. pro-forma coverage.

## Decisions needed from operator

(None this cycle. TB-209 awaiting `ap2 approve` is surfaced mechanically by `ap2 status` and the cron status-report snapshot block per TB-151/TB-173; deliberately not duplicated here per TB-182.)

## Proposals this cycle

1 proposal (slots=4):
- TB-210 — Pin 4 env knobs from TB-208 coverage-debt block (`AP2_TASK_MAX_TURNS`, `AP2_JANITOR_JUDGE_EFFORT`, `AP2_JANITOR_JUDGE_MAX_TURNS`, `AP2_MM_TEAM_ID`) with TB-205-shape happy + error path tests in `test_env_knobs.py`; remove the corresponding 4 rows from test_coverage_drift.py's comment-block shim. Closes Gap (2): testing-axis closed-set debt explicitly tagged by source-of-truth docstring.

Slots 2-4 intentionally unused: Gap (3) (8 event types) needs another cycle to either bifurcate cleanly or wait for TB-210's pattern; Gap (4) (cleanness) stays guardrailed; remaining candidates fall into the n=4 rejection patterns. Land TB-210, observe operator disposition, re-derive Gap (3) split next cycle.

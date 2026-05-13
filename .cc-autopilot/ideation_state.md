# Ideation State

_Last updated: 2026-05-13T02:15:30Z by ideation cron_

## Mission alignment

Five Completes since the prior cycle, all serving the post-2026-05-12T17:02Z **code quality** focus; both axes targeted last cycle (testing + docs) shipped:

- TB-208 (`e2179b9`, 2026-05-13T01:35:43Z) — testing axis: `ap2/tests/test_coverage_drift.py` shipped with three regression-pin tests (`test_every_mcp_tool/env_knob/event_type_has_test_reference`); empty `_COVERAGE_DRIFT_EXEMPT_SURFACES`. Author explicitly deferred a 4th CLI-verb test slot in the module docstring (lines 41-46) as a follow-up.
- TB-207 (`5d1d197`, complete 2026-05-13T02:09:58Z after operator briefing fix at 02:03:16Z) — docs axis: `## Operator CLI verbs (reference)` (35 verbs) + `_collect_cli_verbs` helper in `test_docs_drift.py` walking `build_parser()`. Three retries failed on bullet #4 (literal-backtick truncation in regex anchor); operator unfroze after switching the bullet's `\`...\`` fence to `.` regex.
- TB-206 (`72f5933`, 2026-05-13T00:08:24Z) — docs axis: `ap2/howto.md` worked-example decoupling from goal.md.
- TB-205 (`c13a07c`, 2026-05-12T20:33Z) — testing axis: 17 unit tests pinning 4 SDK-cost env knobs (pre-TB-205: 0 refs).
- TB-204 (`ecd5b2f`, complete 2026-05-13T00:13:22Z after operator unfreeze) — reusability axis: `ap2/tests/_briefing_fixtures.py` + 13-file migration.

Slot count = 5 (Backlog/Ready/Active/Pipeline all empty; insights index empty; no unadopted `cron_proposed`).

## Current focus assessment

- **Current focus: code quality (goal.md L38-97, four axes)**
  - Progress so far:
    - Docs axis: TB-203 reference tables for MCP tools / env knobs / event types; TB-207 added the CLI-verb table + `_collect_cli_verbs` walk; TB-206 decoupled worked examples from goal.md.
    - Testing axis: TB-205 closed 4 SDK-cost env-knob test gaps; TB-208 mechanical drift gate covers MCP tools / env knobs / event types in `test_coverage_drift.py`.
    - Reusability axis: TB-204 introduced `_briefing_fixtures.py` (4 builders + 13-file migration in HEAD).
    - Cleanness axis: untouched (goal.md L86-87 anti-speculative-refactor guardrail unchanged).
  - Gaps:
    (1) **CLI-verb 4th surface in `test_coverage_drift.py`** — TB-208's module docstring (lines 41-46) explicitly defers `test_every_cli_verb_has_test_reference` as a follow-up: "A separate follow-up task adds that fourth test once the helper's `_collect_cli_verbs` walk is reusable across both gates." The reusability axis (goal.md L74-77 threshold-three rule) flips with this third call site — `_collect_cli_verbs` would have 3 readers (docs gate, coverage gate, plus the howto table source) and become extraction-eligible. Single proposal addresses gap (1) on the testing axis AND triggers the deferred extraction on the reusability axis. Proposed as TB-209 this cycle.
    (2) **TB-207-shape briefing pitfall: literal-backtick truncation in shell-bullet regex anchors** — n=2 incidents now (TB-204 bullet #4 grep, TB-207 bullet #4 regex anchor). Pattern is "agent writes a `\`...\`` fence inside a bullet meant as a verbatim regex anchor; validator/operator-fix needed both times." Briefing-validator lint of this specific shape would re-trip the TB-172 wack-a-mole rejection pattern; the structural alternative is the briefing-skill prose itself documenting the pitfall (already in user MEMORY.md), which is a doc-only change with no enforceable gate. Defer until n=3 OR an operator-driven structural ask.
    (3) **Cleanness axis (untouched)** — three named long modules past threshold; deferred per goal.md L86-87 (anti-speculative-refactor guardrail). Unchanged.
  - Status: `in-progress`
  - Reasoning: 3 of 4 axes shipped concrete work this week; cleanness guardrailed; one fresh gap (axis 1 + axis 3 dual-anchor) is today's TB-209 target.

## Non-goal risk check

None. TB-209 stays inside ap2's own test infrastructure — no drift into generic-task-scheduler, replace-operator-judgment, multi-tenancy, real-time, or cross-project axes.

## Considered & deferred this cycle

- **TB-204 follow-up dedup sweep** (residual ~4 of 17 files in TB-204's briefing scope) — TB-204 is now Complete. Sampling intent: confirm whether the 4 unmigrated files genuinely lack the canonical-valid-briefing shape (skip) or are eligible (low-priority follow-up). Defer until a concrete file count surfaces in a regression — speculative dedup absent observed pain re-trips the n=4 enumerative-wack-a-mole pattern.
- **Briefing pitfall lint** (TB-207-shape literal-backtick truncation) — see Gap (2). Routes to TB-172's authoritative reject pattern.
- **Module decomposition** for `tools.py` / `daemon.py` / `cli.py` — explicit goal.md L86-87 anti-speculative-refactor guardrail; no operator-reported confidence-to-modify regression.
- **`# TB-N:` comment-tag sweep / `--help` quality regression-pin / per-op handler extraction** — same dispositions as prior cycle; no new signal.
- **TB-172/TB-175/TB-184/TB-185** — authoritative rejects; will not re-propose. n=4 unchanged. TB-209 below avoids each pattern: closed-set completeness check (not enumerative); extends TB-208's approved primitive (not parallel surface); dual-axis goal-anchored (testing + reusability).

## Cycle observations

(Triage from prior cycle: prior carried two observations — (a) AST-walk escalation pin if substring-presence misses a real TB-205-shape recurrence, (b) TB-206 4-attempt landing as single-incident. Drop (a): TB-208 just landed; no recurrence yet to act on; pin lives in TB-208's docstring naturally now. Drop (b): TB-207 was a 4-attempt landing too, but the failure mode was different (briefing bullet truncation, not implementation), so no n=2 pattern across implementation incidents.)

- TB-207's failure cascade adds an n=2 data point on a specific shell-bullet shape — literal-backtick fences inside regex/grep-anchor bullets get truncated mid-pattern by the validator's quote pairing. Resolved inline by operator both times. If n=3 surfaces, the structural fix is documenting "regex anchors must not contain backticks; use `.` or POSIX classes" in the briefing-skill prose, NOT a validator lint (that's the TB-172 wack-a-mole shape).

## Decisions needed from operator

(None this cycle. All prior open items resolved: TB-204 unfrozen + Complete, TB-206 Complete, TB-207 Complete, TB-208 Complete.)

## Proposals this cycle

1 proposal (slots=5):
- TB-209 — Add `test_every_cli_verb_has_test_reference` to `test_coverage_drift.py` and extract `_collect_cli_verbs` to a shared helper module (3rd call site triggers goal.md L74-77 threshold-three extraction). Closes Gap (1): both the testing-axis 4th surface and the reusability-axis deferred extraction in one task.

Slots 2-5 intentionally unused: remaining candidates fall into the n=4 rejection patterns (wack-a-mole / parallel-surface / anti-speculative-refactor) or lack a concrete observed-gap signal. Land TB-209, observe operator disposition, re-derive next cycle against settled state.

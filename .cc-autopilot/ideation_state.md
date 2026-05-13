# Ideation State

_Last updated: 2026-05-13T06:24:25Z by ideation cron_

## Mission alignment

Two state changes since prior cycle (2026-05-13T04:21Z):
(a) TB-210 landed in Backlog at 04:24:08Z — the prior cycle's sole proposal, operator-queued add_backlog → matches the assessment's plan verbatim;
(b) TB-209 retry-exhausted at 04:55:21Z → Frozen, despite implementation being present at commit `1a54d14` (verified via `git_log_grep("TB-209")` + direct file inspection). The same `code-quality` focus remains served; the 5-Complete arc is unchanged (no new task_complete with `status=complete` since TB-207's 02:09Z re-verify).

- TB-207 (`5d1d197`, 2026-05-13T02:09Z) — docs axis: CLI-verb reference table.
- TB-206 (`72f5933`, 2026-05-13T00:08Z) — docs axis: worked-example decoupling.
- TB-205 (`c13a07c`, 2026-05-12T20:33Z) — testing axis: 4 SDK-cost env knob pins.
- TB-208 (`e2179b9`, 2026-05-13T01:35Z) — testing axis: MCP/env-knob/event-type drift gate.
- TB-204 (`ecd5b2f`, 2026-05-13T00:13Z) — reusability axis: `_briefing_fixtures.py` migration.

Slot count = 4 (Backlog: TB-210 pending review; Ready/Active/Pipeline empty; insights index empty; no unadopted `cron_proposed`).

## Current focus assessment

- **Current focus: code quality (goal.md L38-97, four axes)**
  - Progress so far:
    - Docs axis: TB-203, TB-206, TB-207 (MCP/env-knob/event-type tables + worked-example + CLI-verb table).
    - Testing axis: TB-205 (4 env knobs); TB-208 (3-surface drift gate). TB-209's 4th-surface implementation IS in HEAD (`1a54d14` — `test_every_cli_verb_has_test_reference` + extracted `_collect_cli_verbs` to `ap2/tests/_source_registry.py`) but the task is Frozen on a verifier prose-bullet ambiguity (see Failure review below).
    - Reusability axis: TB-204 (`_briefing_fixtures.py`); TB-209's helper extraction (`_collect_cli_verbs` to `_source_registry.py`) also in `1a54d14` — 3rd call site flips goal.md L74-77's threshold-three rule.
    - Cleanness axis: untouched (goal.md L86-87 anti-speculative-refactor guardrail).
  - Gaps:
    (1) **TB-209 docstring-rewrite prose bullet stuck failing** — implementation IS in HEAD (`1a54d14`); all 6 shell bullets pass across all 3 retries (`uv run pytest -q` green every run); only the prose bullet `\`ap2/tests/test_coverage_drift.py\`'s module docstring no longer describes the CLI-verb test as "deferred" (the lines 41-46 paragraph in the current docstring is rewritten or removed)` failed 3/3. Inspection: the rewrite DID land (file L41-47 now describes the test as DONE: `"CLI-verb fourth surface (TB-209): test_every_cli_verb_has_test_reference mirrors the three sibling tests' shape..."` + names `_source_registry.py`), but the SAME docstring still mentions "deferred" at L31 referring to a DIFFERENT surface (AST-walk-semantics tightening). The prose judge can't confidently scope the negation "no longer describes the CLI-verb test as 'deferred'" away from "is 'deferred' anywhere in the docstring?". Classic prose-bullet ambiguity around absence-claims with surviving lexical noise. Classify as **edit-briefing**; surfacing in "Decisions needed from operator" below.
    (2) **TB-208 env-knob coverage debt (4 names)** — TB-210 (Backlog, `@blocked:review`) addresses this. Pending operator approval; surfaced mechanically by `ap2 status` per TB-151/TB-173, NOT duplicated here per TB-182.
    (3) **TB-208 event-type coverage debt (8 names)** — defer until TB-210 lands AND the 8 names can be bifurcated cleanly into daemon vs. mattermost subsets. Unchanged from prior cycle.
    (4) **Cleanness axis (untouched)** — goal.md L86-87 anti-speculative-refactor guardrail. Unchanged.
  - Status: `in-progress`
  - Reasoning: TB-209's already-shipped implementation needs only an operator-driven `ap2 update` + `ap2 unfreeze` to close the testing-axis 4th surface (precedent across TB-204/TB-206/TB-207 in the last 24h); TB-210 in flight to operator covers Gap (2); event-type debt + cleanness stay parked per their rationales.

## Non-goal risk check

None. The recommended operator action (briefing-bullet rewrite + unfreeze) stays inside ap2's own infrastructure; no drift into generic-task-scheduler / replace-operator-judgment / multi-tenancy / real-time / cross-project axes.

## Considered & deferred this cycle

- **Proposing a `#fix-briefing` task per the TB-88 playbook for TB-209** — Structurally broken post-TB-198 (the playbook predates the `.cc-autopilot/tasks/` fence). A task agent cannot edit the same-task briefing file: TB-198 added `.cc-autopilot/tasks` to `TASK_AGENT_FENCED_PATHS` (`ap2/tools.py:3784`), and task agents have no `operator_queue_append` MCP tool (`TASK_AGENT_TOOLS` at `ap2/tools.py:3702-3717` — Read/Write/Edit/Glob/Grep/Bash + `pipeline_task_start`/`report_result`/`cron_propose` only). Established operator convention since TB-198 (TB-204 update 23:25 → unfreeze 00:13, TB-206 update 23:24 → unfreeze 00:02, TB-207 update 02:03 → unfreeze 02:03 — all within the last 24h) is operator-driven `ap2 update` + `ap2 unfreeze` directly. Surface as Decision needed rather than a stale-playbook task.
- **TB-208 event-type coverage debt (8 names)** — still premature; wait for TB-210 to land and the extender pattern to settle. Re-derive next cycle.
- **Updating the ideation prompt's TB-88 playbook to reflect the operator-convention shift** — meta-validator territory; n=4 reject pattern (TB-172-shape). Not proposing as a task; observation belongs in Cycle observations below for now.
- **n=4 authoritative rejects** (TB-172/TB-175/TB-184/TB-185) — unchanged; nothing this cycle re-trips those shapes.
- **Cleanness module decomposition** — goal.md L86-87 guardrailed, unchanged.

## Cycle observations

(Triage from prior cycle: prior carried ONE observation about TB-208's discovered-at-landing comment-block shim and how follow-up TBs would shrink it. Promote: that observation INFORMED the TB-210 proposal, now in Backlog awaiting review → planning value realized; drop. No carry.)

- **Prose-bullet ambiguity around absence-claims with surviving lexical noise** — TB-209's failed bullet asserts removal of the word "deferred" but the same docstring legitimately still mentions "deferred" for a DIFFERENT surface. The judge can't scope the negation. Anti-pattern: prose bullets that assert REMOVAL of a token that legitimately survives elsewhere in the same file. Positive-shape alternative: assert PRESENCE of the replacement text the rewrite added — shell-verifiable via `grep -q "<new-marker>"`. This observation informs the operator-action recommendation below; carry to next cycle ONLY if a second prose-bullet failure with the same shape lands (then it's a pattern, not an n=1).
- **TB-88 fix-briefing playbook structurally stale post-TB-198** — playbook says "propose ONE meta fix-task ... whose briefing instructs the agent to rewrite the broken bullets in the original briefing file"; the agent cannot, because (a) `.cc-autopilot/tasks/` is fenced (`TASK_AGENT_FENCED_PATHS`, `tools.py:3784`), (b) task agents lack `operator_queue_append`. n=1 today (TB-209); not yet a pattern requiring a meta-fix task. Watch for a 2nd instance next cycle; carry only if it recurs.

## Decisions needed from operator

- Decision needed: TB-209's implementation is in HEAD at `1a54d14` (passes all 6 shell verification bullets), but 1 prose bullet at `.cc-autopilot/tasks/add-test-every-cli-verb-has-test-referen.md` L39 — `\`ap2/tests/test_coverage_drift.py\`'s module docstring no longer describes the CLI-verb test as "deferred" (the lines 41-46 paragraph in the current docstring is rewritten or removed)` — failed 3/3 retries because the judge can't scope "no longer describes" away from a legitimate surviving "deferred" mention at L31 (about AST-walk tightening). To unblock: (1) run `ap2 update TB-209 --briefing-file <fixed>` replacing that single bullet with a positive shell check, e.g. `` ` ``\``grep -q "CLI-verb fourth surface (TB-209)" ap2/tests/test_coverage_drift.py`\``` `` — exit 0; the rewritten section anchored on TB-209's closure is present `` ` ``; (2) `ap2 unfreeze TB-209`. The next verification cycle then passes on shell bullets alone and the task lands Complete, closing the testing-axis 4th-surface gap. Unblock-condition: TB-209 moves to Complete; goal.md L58-63 testing-coverage axis fully covered on all four surfaces (MCP tool / env knob / event type / CLI verb).

## Proposals this cycle

0 proposals (slots=4):

Backlog has TB-210 in flight to operator (covers Gap 2). TB-209's gap (1) closes via operator-driven `ap2 update` + `ap2 unfreeze` — no task proposal needed (post-TB-198 fence, the TB-88 fix-briefing task pattern is structurally broken for same-task briefing rewrites; operator convention since TB-204/TB-206/TB-207 is direct update). Gap (3) defers; Gap (4) guardrailed. Remaining candidates trip n=4 reject patterns. Hold the proposal slots; re-derive next cycle after TB-209 unfreezes and TB-210 lands.

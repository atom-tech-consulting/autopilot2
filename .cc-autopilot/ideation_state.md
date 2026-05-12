# Ideation State

_Last updated: 2026-05-12T21:58:00Z by ideation cron_

## Mission alignment

Three of last cycle's identified blockers cleared and a fourth got
stuck on a self-inflicted shell-bullet bug. Recent Completes still
serve the goal (operator pivoted to **Current focus: code quality**
2026-05-12T17:02Z; subsequent work is all on that axis):

- TB-205 (`c13a07c`, 20:33Z) — testing axis: 17 env-knob tests pin
  `AP2_EVENT_CONTEXT` / `AP2_CONTROL_MAX_TURNS` / `AP2_IDEATION_MAX_TURNS`
  / `AP2_AGENT_MODEL` cost/behavior contracts.
- TB-203 (`452627e`, 20:18Z; original work in `1ed8a03`) — docs axis:
  docs-drift gate for MCP tools / env knobs / event types in howto.md
  + architecture.md. Follow-up commit `452627e` ALSO re-synced
  `ap2/howto.md` L140-156's Current-focus worked example + `test_docs.py`
  synthetic briefing to the post-pivot `code quality` heading — Shape A
  of TB-206 effectively shipped here, both `test_docs.py` quote/anchor
  tests now PASS on HEAD.
- TB-204 (`ecd5b2f`, Frozen at 20:42Z) — reusability axis: fixture
  module `_briefing_fixtures.py` + 4 builders + 13-file migration are
  IN HEAD; task FROZEN on `retry_exhausted` due to a non-recursive
  `grep -lE 'pattern' ap2/tests/ ap2/tests/e2e/` bullet that's
  unsatisfiable on BSD/GNU grep without `-r`. Implementation is correct.
- TB-202 (`b09e3bc`, 08:02Z) — Active-gate for `backfill-proposals` /
  `cron edit`.
- TB-201 (`03c4fc1`, 07:49Z) — queue-routed `ap2 ack` +
  `operator_log_append` MCP.

Slot count = 4 (Backlog=1, so threshold likely 5). Insights index
empty. No unadopted `cron_proposed` events.

## Current focus assessment

- **Current focus: code quality (goal.md L38-97, four axes)**
  - Progress so far: docs axis covered by TB-203 (`1ed8a03` + `452627e`
    syncing howto/architecture + post-pivot worked example); testing
    axis by TB-205 (`c13a07c`); reusability axis by TB-204 (`ecd5b2f`
    in HEAD but Frozen on broken verification bullet); cleanness axis
    untouched (guarded by goal.md L86-87 anti-speculative-refactor).
  - Gaps:
    (1) **TB-204 unfreeze path** — work is correct in HEAD (1267 tests
    pass; all 4 fixture builders exist; 12 test files import the
    fixture; test_tools.py `## Goal` count = 18 ≤20 and `Why now:` = 5
    ≤5). Verification bullet #4 (`grep -lE '...' ap2/tests/
    ap2/tests/e2e/` no `-r`) is structurally unsatisfiable on
    macOS/Linux grep. Briefing fenced from task-agent writes
    (TB-198), so the fix is operator-only — `ap2 update TB-204` with
    the corrected `grep -rlE ...` bullet, then `ap2 unfreeze TB-204`.
    Edit-briefing classification per failure-review playbook.
    (2) **TB-206 disposition** — TB-203's follow-up commit `452627e`
    already shipped Shape A (re-synced howto.md to post-pivot
    `code quality` heading verbatim + updated test_docs.py synthetic
    briefing). TB-206's Shape A is now redundant; Shape B (decouple
    via `<theme name>` placeholder + programmatic anchor read from
    goal.md) remains a structural improvement closing the rotation-
    coupling that re-fired this morning. Needs operator approve-Shape-B
    or delete-as-superseded call.
    (3) **Operator-facing docs surface gap** — TB-203 landed reference
    tables for MCP tools / env knobs / event types in howto.md
    (gated by `test_docs_drift.py`), but the symmetric `## Operator
    CLI verbs (reference)` section is missing: 24+ `ap2 <verb>`
    subcommands (status, start/stop, doctor, init, logs, backlog,
    add, update, delete, reject, classify, ack, approve, unfreeze,
    ideate, update-goal, backfill-proposals, pause/resume, cron list,
    sandbox user-audit/setup/install-token/project-setup/audit,
    check, web) are scattered in prose mentions across howto.md. An
    operator looking up "what does `ap2 classify` do? what verdicts?"
    has no single landing place — falls back to `ap2 <verb> --help`
    or source-reading (goal.md L65-72's exact failure mode).
    (4) **Cleanness axis (untouched)** — three of four named long
    modules past threshold; deferred per goal.md L86-87 anti-
    speculative-refactor guardrail (unchanged from prior cycles).
  - Status: `in-progress`
  - Reasoning: focus is ~5h old, three approved tasks have shipped or
    are in retry; Gaps (1) and (2) are operator-action items the
    operator owns; Gap (3) is the next docs-axis target with a clean
    completeness criterion and a TB-203-symmetric anti-drift pattern.

## Non-goal risk check

None. TB-207 proposal stays inside ap2's own docs surface
(`ap2/howto.md`, `ap2/tests/test_docs_drift.py`) — no drift into
generic-task-scheduler, replace-operator-judgment, multi-tenancy,
real-time, or cross-project axes.

## Considered & deferred this cycle

- **Enumerative env-knob coverage extensions** (`AP2_VERIFY_JUDGE_*`,
  `AP2_JANITOR_*`, `AP2_AUTO_DIAGNOSE_*`, `AP2_MM_*`,
  `AP2_VERIFY_TIMEOUT_S`) — TB-205 just landed 4 knobs; without a
  silent-regression signal worth pinning more against, this re-trips
  the operator-rejection wack-a-mole pattern (TB-172 / TB-185 shape,
  n=4 unchanged).
- **Module decomposition** for `ap2/tools.py` / `ap2/web.py` /
  `ap2/daemon.py` / `ap2/cli.py` — goal.md L86-87 explicit
  anti-speculative-refactor guardrail; no operator-reported
  confidence-to-modify regression yet.
- **Per-op handler extraction in `_apply_operator_op`** (tools.py
  L2431, 9+ ops in an if/elif chain with structural similarity:
  validate→act→emit-event→reconcile) — premature per goal.md L74-77
  "Threshold is three (not two) — premature abstraction is its own
  failure mode" without a concrete adding-a-verb pain signal. Each
  op has materially different validation/reconciliation; helper
  factoring needs a real triggering case.
- **`# TB-N:` comment-tag sweep** (goal.md L80-84 cleanness axis,
  specifically called out) — sweeping the codebase to strip rotting
  task-ID tags is exactly the enumerative shape that has been
  rejected before; defer until a concrete reading-friction signal
  surfaces.
- **TB-172/TB-175/TB-184/TB-185** — authoritative rejects;
  will not re-propose. Rejection pattern (n=4, unchanged shape):
  "creates parallel surface OR enumerative wack-a-mole OR off-focus."
  TB-207 below avoids each: it adds a single closed-set reference
  section (not enumerative — completeness is mechanical), uses the
  existing `test_docs_drift.py` gate (not parallel surface), and is
  explicitly code-quality docs-axis goal-anchored.

## Cycle observations

(Triage from prior cycle: prior had "(no carried bullets this cycle)";
nothing to triage forward.)

- TB-204's verification-bullet-shape failure (Shell pitfall: `grep -lE
  PATTERN DIR/` without `-r` returns "Is a directory" + empty stdout)
  matches goal.md L70-72's "documented surface that breaks the
  daemon silently" failure mode reframed as a briefing pitfall.
  Already an operator-facing `ap2 check` lint pattern (TB-138 +
  TB-171); the no-`-r` `grep DIR/` case is NOT in the lint set today.
  Carrying as observation: if the same shape recurs, escalate to
  a focused lint addition (not enumerative — single high-cost
  pitfall). Currently n=1.

## Decisions needed from operator

- Decision needed: TB-204 unfreeze path. Implementation is in HEAD at
  `ecd5b2f` (all four builders + 13-file migration; 1267 tests pass;
  fixture-import count = 12 ≥10; test_tools.py `## Goal` = 18 ≤20;
  `Why now:` = 5 ≤5). Only blocker is briefing verification bullet #4
  using `grep -lE 'pattern' ap2/tests/ ap2/tests/e2e/` (no `-r`) —
  structurally unsatisfiable on BSD/GNU grep without recursion.
  Operator action: `ap2 update TB-204 --briefing-file <patched>`
  (change bullet to `grep -rlE ...`), then `ap2 unfreeze TB-204`.
  Unblock condition: with the bullet fix, the next dispatch will
  pass all six shell bullets against the existing commit and TB-204
  closes Complete without a code re-run.
- Decision needed: TB-206 disposition. TB-203's follow-up commit
  `452627e` already shipped TB-206's Shape A (re-synced
  `ap2/howto.md` L140-156 + `test_docs.py` synthetic briefing to
  post-pivot `code quality` heading verbatim; both quote-verbatim
  and anchor-validator tests now PASS on HEAD). TB-206's Shape A
  scope is redundant; Shape B (decouple example from focus rotation
  via `<theme name>` placeholder + programmatic anchor read from
  goal.md) remains a structural improvement that prevents this
  morning's failure cascade from recurring on the next operator
  focus rotation. Operator action: `ap2 approve TB-206` (do Shape B
  refactor as a follow-up) OR `ap2 reject TB-206 --reason "Shape A
  shipped via TB-203 follow-up; Shape B not worth the test refactor
  right now"`. Unblock condition: either outcome frees the Backlog
  slot and lets ideation re-derive priorities against fresh state
  next cycle.

## Proposals this cycle

1 proposal (slots=4):
- TB-207 — Add `## Operator CLI verbs (reference)` reference section
  to `ap2/howto.md`, symmetric to TB-203's MCP-tools / env-knobs /
  event-types tables; extend `test_docs_drift.py` to gate the new
  table against the live CLI parser so adding a new subcommand
  fails CI without a docs entry (Gap 3, docs axis).

Slots 2-4 intentionally unused: remaining candidates fall into the
operator-rejected wack-a-mole / anti-speculative-refactor patterns,
and the operator has two open decisions (TB-204 fix, TB-206
disposition) that will reshape next cycle's prioritization. Land
TB-207, observe operator decisions on TB-204/206, re-derive next
cycle against a settled board.

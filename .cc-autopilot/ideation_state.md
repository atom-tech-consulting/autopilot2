# Ideation State

_Last updated: 2026-05-13T00:08:24Z by ideation cron_

## Mission alignment

TB-206 just shipped on its post-unfreeze pass at 00:08:24Z, resolving
the self-coupling between `ap2/howto.md`'s worked-example blocks and
`goal.md` that cascaded this morning's project-wide pytest failure;
the prior cycle's "Decisions needed" #2 is now closed. Recent
Completes all serve the post-2026-05-12T17:02Z **code quality**
focus:

- TB-206 (`72f5933`, complete at 00:08:24Z) — docs axis: full Shape B
  decoupling shipped (5 worked-example blocks rewritten as fictional
  Slack-bot-for-trade-alerts, programmatic anchor read from goal.md,
  verbatim-quote helpers dropped).
- TB-205 (`c13a07c`, 2026-05-12T20:33Z) — testing axis: 17 unit tests
  pin `AP2_EVENT_CONTEXT` / `AP2_CONTROL_MAX_TURNS` /
  `AP2_IDEATION_MAX_TURNS` / `AP2_AGENT_MODEL` (pre-TB-205: 0 test
  refs).
- TB-203 (`452627e`; original `1ed8a03`, 2026-05-12T20:18Z) — docs
  axis: docs-drift gate (`test_docs_drift.py`) for MCP tools / env
  knobs / event types.
- TB-204 (`ecd5b2f`, Frozen 2026-05-12T20:42Z) — reusability axis:
  fixture + 13-file migration in HEAD; briefing now fixed by operator
  (23:25:52Z update changed `grep -lE` to `grep -rlE`); awaits
  `ap2 unfreeze TB-204`.
- TB-202 (`b09e3bc`, 2026-05-12T08:02Z) — Active-gate for
  `backfill-proposals` / `cron edit`.

Slot count = 4 (Backlog=1: TB-207 awaiting review). Insights index
empty. No unadopted `cron_proposed` events.

## Current focus assessment

- **Current focus: code quality (goal.md L38-97, four axes)**
  - Progress so far:
    - Docs axis: TB-203 landed howto.md/architecture.md reference
      tables + `test_docs_drift.py` gate; TB-206 decoupled
      worked-example blocks from goal.md content; TB-207 (Backlog
      awaiting review) extends the gate to CLI verbs.
    - Testing axis: TB-205 closed 4 env-knob test gaps with 17 unit
      tests in `test_env_knobs.py`.
    - Reusability axis: TB-204 (`ecd5b2f`) introduced
      `ap2/tests/_briefing_fixtures.py` with 4 builders + 13-file
      migration in HEAD; awaits operator `ap2 unfreeze TB-204`
      now that bullet #4 is fixed.
    - Cleanness axis: untouched (goal.md L86-87 anti-speculative-
      refactor guardrail unchanged).
  - Gaps:
    (1) **Test-presence drift gate** — TB-205 is the canonical
    missed-coverage case: 4 env knobs shipped with ZERO `ap2/tests/`
    references and only surfaced when ideation Step 1.5 enumerated
    untested SDK-cost knobs. The docs-drift gate (TB-203's
    `test_docs_drift.py::test_every_env_knob_documented`,
    `_mcp_tool_documented`, `_event_type_documented`) catches the
    same gap shape on the docs axis; the symmetric test-axis gate
    doesn't exist yet. Proposed as TB-208 this cycle.
    (2) **Cleanness axis (untouched)** — three named long modules
    past threshold; deferred per goal.md L86-87 (anti-speculative-
    refactor guardrail). Unchanged from prior cycles.
    (3) **TB-207 disposition** — Backlog item from prior cycle,
    awaits operator review. Mechanically surfaced by `ap2 status`
    (TB-151 / TB-173); not a fresh gap.
  - Status: `in-progress`
  - Reasoning: focus is ~7h old; 3 of 4 axes (docs, testing,
    reusability) have shipped or are in retry; cleanness axis is
    guardrailed; one fresh gap (test-presence gate, axis 1) is
    today's TB-208 target.

## Non-goal risk check

None. TB-208 stays inside ap2's own test infrastructure
(`ap2/tests/test_coverage_drift.py`, sibling to existing
`test_docs_drift.py`) — no drift into generic-task-scheduler,
replace-operator-judgment, multi-tenancy, real-time, or
cross-project axes.

## Considered & deferred this cycle

- **Enumerative env-knob coverage extensions** (`AP2_VERIFY_JUDGE_*`,
  `AP2_JANITOR_*`, `AP2_AUTO_DIAGNOSE_*`, `AP2_MM_*`,
  `AP2_VERIFY_TIMEOUT_S`) — re-trips the n=4 operator-rejection
  wack-a-mole pattern (TB-172 / TB-185 shape); TB-208's mechanical
  gate is the structural alternative (catches them all + future
  additions with one test).
- **Module decomposition** for `ap2/tools.py` / `ap2/web.py` /
  `ap2/daemon.py` / `ap2/cli.py` — explicit goal.md L86-87
  anti-speculative-refactor guardrail; no operator-reported
  confidence-to-modify regression yet.
- **Per-op handler extraction in `_apply_operator_op`** (tools.py
  L2431, 9+ ops in an if/elif chain) — goal.md L74-77 threshold-three
  not yet met (each op has materially different validation logic;
  helper factoring needs a real triggering case).
- **`# TB-N:` comment-tag sweep** (goal.md L80-84) — exactly the
  enumerative shape rejected before; defer until a concrete
  reading-friction signal surfaces.
- **`ap2 <verb> --help` quality regression-pin** — goal.md L68 names
  `--help` as a documented surface, but no observed
  empty-or-trivial-help signal; speculative without that.
- **TB-204 follow-up dedup sweep** — TB-204 covered 13 of ~17 files
  named in its briefing scope. Wait for unfreeze + Complete before
  scoping a follow-up against residual count.
- **TB-172/TB-175/TB-184/TB-185** — authoritative rejects; will not
  re-propose. Rejection pattern (n=4, unchanged): "creates parallel
  surface OR enumerative wack-a-mole OR off-focus." TB-208 below
  avoids each: closed-set completeness check (not enumerative);
  extends an approved existing primitive (TB-203's
  `test_docs_drift.py`); axis-1 testing-coverage goal-anchored.

## Cycle observations

(Triage from prior cycle: prior carried one observation — the
no-`-r` `grep DIR/` shell-bullet pitfall, n=1. Drop: operator
resolved it inline by editing TB-204's bullet #4; same shape hasn't
recurred in any other briefing's verification; no n=2 signal to
act on.)

- TB-208 itself hedges against a recurring TB-205-shape gap
  (registered surface shipped without test refs). Today's count is
  n=1 (TB-205's 4 env knobs). If the same shape recurs after TB-208
  lands and the gate misses it, substring-presence is the wrong
  granularity and an AST-walk escalation is the right move — pin
  to revisit if recurrence happens.
- TB-206 took 4 attempts before landing; failure cascade was
  briefing-bullet shape (missing `!` exit-inversion on bullets
  #5/#6), not implementation. Single-incident observation; no
  pattern yet. Drop unless n=2 surfaces.

## Decisions needed from operator

(None this cycle. The prior cycle's two open items — TB-204 unfreeze
and TB-206 disposition — both resolved: TB-206 shipped via the
operator's update + unfreeze pass, and TB-204's briefing is now
fixed and just awaits the mechanical `ap2 unfreeze TB-204` already
surfaced by `ap2 status`.)

## Proposals this cycle

1 proposal (slots=4):
- TB-208 — `ap2/tests/test_coverage_drift.py`: parallel to TB-203's
  docs-drift gate on the testing axis. Three regression-pin tests
  assert every registered MCP tool / `AP2_*` env knob / event type
  has at least one substring reference in `ap2/tests/` (Gap 1,
  axis 1 testing coverage).

Slots 2-4 intentionally unused: remaining candidates fall into the
n=4 rejection patterns (enumerative wack-a-mole, anti-speculative-
refactor, parallel-surface) or lack a concrete observed-gap signal
(`--help` quality check, TB-204 dedup follow-up). Land TB-208,
observe operator dispositions on TB-204 unfreeze + TB-207 review +
TB-208 review, re-derive next cycle against settled state.

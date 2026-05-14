# Ideation State

_Last updated: 2026-05-14T04:59:21Z by ideation cron_

## Mission alignment

Code-quality consolidation remains mission-aligned. The TB-208/TB-209
enumerated debt (4 env knobs + 8 event types + 12 CLI verbs = 24
surfaces) is now FULLY closed by 6 closure-batch tasks landing in a
~24h window. The shim comment block in `test_coverage_drift.py` is
empty; every registry surface resolves to a real `test_tbXXX_*.py`
module. TB-216 closed the TB-214-shape dead-letter (titles with `*`)
at queue-append time. Board: `0A / 0R / 0B / 0P / 107C / 3F`. 3 most
recent Completes:

- TB-211 retry (`efccab5`, 2026-05-14T02:52Z) — `_stub_main_loop_internals`
  fixture drives daemon.main_loop end-to-end for 5 daemon event types;
  closes the final TB-208 event-type shim row.
- TB-216 (`fd4e77a`, 2026-05-14T02:42Z) — `_validate_single_line`
  field-specific `*`-in-title reject (TB-134 loud-reject shape).
- TB-215 (`c84e8da`, 2026-05-14T02:33Z) — 4 sandbox audit/setup CLI
  verbs pinned; TB-209 12-verb debt fully closed.

## Current focus assessment

- **Current focus: code quality (goal.md L38-97, four axes)**
  - Progress so far:
    - Testing axis: TB-205 + TB-210 (8 env knobs); TB-211 + TB-212 (8
      event types); TB-213 + TB-214 + TB-215 (12 CLI verbs). All 24
      enumerated registry surfaces have happy + error path tests; the
      drift-gate shim block is empty. TB-208/TB-209 substring gates
      remain green; future surface additions trip the gate at landing.
    - Docs axis: TB-203 (MCP/env/event drift), TB-206 (howto worked-
      examples decoupled from goal.md), TB-207 (CLI-verb reference
      section + drift). All four operator-facing registry surfaces
      have docs entries + drift gates.
    - Reusability axis: TB-204 (`_briefing_fixtures.py`, ~30+ inline
      fixtures deduplicated) + TB-209 (`_source_registry.py` for
      `_collect_cli_verbs`, 3rd-call-site threshold trigger).
    - Cleanness axis: untouched per L86-87 anti-speculative-refactor
      guardrail. No module-boundary clarity has surfaced from reading.
  - Gaps:
    (1) **Drift-gate sufficiency tightening still deferred.**
        `test_coverage_drift.py` docstring L26-32 explicitly defers
        substring → AST-walk semantics check ("the test imports the
        symbol AND asserts against it") until "the substring gate is
        observed missing a real pro-forma gap." None observed; the
        deferral stands. Premature tightening replays TB-172 wack-a-mole.
    (2) **Cleanness axis (untouched)** — goal.md L86-87. Unchanged
        from prior cycle. `ap2/tools.py` past 3700 lines,
        `ap2/daemon.py` past 2500, `ap2/cli.py` past 1700 — but no
        module-boundary has surfaced from reading.
    (3) **2-call-site helpers awaiting threshold-three trigger.**
        `_collect_env_knobs`, `_collect_event_types`,
        `_all_agent_mcp_tool_short_names` are inlined in both
        `test_docs_drift.py` and `test_coverage_drift.py`
        (`_source_registry.py` L23-31). No 3rd reader has appeared.
        Premature extraction would re-trip L74-77's "premature
        abstraction is its own failure mode" guardrail.
  - Status: `in-progress`
  - Reasoning: The TB-208/TB-209 closure batch (24 surfaces / 6 task
    landings) substantively advanced testing coverage to a complete
    state. Remaining axis gaps are either explicitly deferred (#1, #3)
    or guardrailed (#2). The three "registry surface" axes (tests +
    docs + reusability) are at parity; cleanness is the only axis with
    a structural opening, but it's the one most resistant to ideation
    proposing without a natural trigger from reading. Operator
    engagement (see Decisions needed) is the lowest-risk path forward.

## Non-goal risk check

None. All recent work stayed inside the four axes; no drift into
generic-task-scheduler / replace-operator-judgment / multi-tenancy /
real-time / cross-project axes.

## Considered & deferred this cycle

- **Meta-test pinning the `test_tb<NNN>_<scope>.py` convention** —
  Prior cycle observation: 6 closure-batch modules (TB-210/211/212/
  213/214/215) follow the convention, far past threshold-three. BUT
  the delete-test fails: a future module skipping the convention
  reduces traceability (a discoverability concern), not correctness;
  the TB-N is in the filename already. Pinning it as a test gate is
  low-value scope-shape work that doesn't advance any of the four
  axes. Matches the pro-forma-test failure mode goal.md L60-63 warns
  against. Drop, not just defer.
- **Surface `board_malformed_line` count in `ap2 status`** — Prior
  cycle defer; TB-216's write-time gate makes it more relevant, but
  it's still feature/observability work that adds a NEW operator-
  facing surface rather than tightening tests/docs on existing ones.
  Goal.md L38-46 pivots FROM accumulating signal TO consolidating
  foundations. Hold until a real malformed-line incident slips past
  TB-216 and causes operator-visible damage.
- **TB-175-shape ideation-acceptance-rate insight aggregator** —
  Operator log L80 carry-deferral (do not re-propose before ~3+
  ideation cycles after TB-188 lands). 7 days since TB-188; per-
  proposal records exist; BUT zero `ap2 classify` entries in
  operator_log.md — the operator-authored signal stream is the half
  TB-189 was meant to feed. Re-deferring until classify verdicts
  surface or the operator explicitly engages.
- **Tighten `test_coverage_drift` substring gate (AST-walk)** —
  Gate docstring L26-32 explicitly defers tightening until pro-forma
  gap observed. None observed; deferral holds. Premature tightening
  re-trips TB-172's wack-a-mole rejection.
- **Document MM chat verbs as a 5th docs surface** — Investigated:
  MM chat verbs aren't a uniformly-registered surface (handler agent
  interprets natural-language dispatch via prompt convention, no
  argparse parser to walk). No mechanical drift-gate is possible;
  prose-only documentation drifts silently. Skip — same conclusion as
  prior cycle's investigation of cron-job drift gate.
- **n=4 authoritative rejects** (TB-172/175/184/185) — Continued
  vigilance. This cycle's deferrals each match rejected shapes
  (wack-a-mole, defer-until-data, feature-not-consolidation,
  parallel-surface). Better to ship zero this cycle than recapitulate.

## Cycle observations

- Prior cycle's `test_tb<NNN>_<scope>.py` convention bullet promoted
  to `## Considered & deferred` (meta-test idea) and dropped here —
  the 6-module batch is now complete; no further closure batches are
  in flight, so the convention's emergence isn't an ongoing pattern
  to track.

## Decisions needed from operator

- Decision needed: the testing/docs/reusability registry-surface debt
  is now closed (TB-208/TB-209 batch fully landed); cleanness axis
  remains untouched per L86-87 anti-speculative-refactor guardrail;
  remaining gaps are explicitly deferred (substring tightening,
  threshold-three triggers) or awaiting external signal (classify
  data). Either edit goal.md to evolve the focus (add a 5th axis,
  surface a specific module-boundary intent for the cleanness axis,
  or rotate to a new theme) OR confirm the current focus should
  continue at zero-proposal cadence until natural triggers (a real
  pro-forma gap observed, a 3rd helper call site, a malformed-line
  incident, classify-data accumulation). Unblock-condition: next
  ideation cycle gets a fresh focus signal vs. continuing to defer
  back to this same operator decision each tick.

## Proposals this cycle

Backlog at 0 but no high-leverage gap surfaced that fits the
consolidation focus without recapitulating a rejected shape
(wack-a-mole, defer-until-data, feature-not-consolidation, parallel-
surface) or violating the L86-87 anti-speculative-refactor guardrail.
Proposing zero this cycle rather than manufacturing work; surfaced
the focus-rotation question to the operator above.

# Ideation State

_Last updated: 2026-05-14T02:52:28Z by ideation cron_

## Mission alignment

Code-quality consolidation continues to be the mission-aligned focus.
TB-208's full enumerated debt (4 env knobs + 8 event types + 12 CLI
verbs = 24 surfaces) is now closed; the shim comment block in
`test_coverage_drift.py` resolves every surface via a real
`test_tbXXX_*.py` module rather than a placeholder. TB-216 closes the
TB-214-shape dead-letter (titles containing `*`) at queue-append time.
Board: `0A / 0R / 0B / 0P / 105C / 3F`. 3 most recent Completes:

- TB-216 (`fd4e77a`, 2026-05-14T02:42Z) — `_validate_single_line`
  field-specific `*`-in-title reject (TB-134 loud-reject shape).
- TB-211 retry (`efccab5`, 2026-05-14T02:52Z) — `_stub_main_loop_internals`
  fixture drives daemon.main_loop end-to-end for the 5 daemon event types.
- TB-215 (`c84e8da`, 2026-05-14T02:33Z) — 4 sandbox audit/setup CLI
  verbs pinned; TB-209 CLI-verb debt fully closed (12/12).

## Current focus assessment

- **Current focus: code quality (goal.md L38-97, four axes)**
  - Progress so far:
    - Docs axis: TB-203 (MCP/env/event drift), TB-206 (howto worked-
      examples decoupled from goal.md), TB-207 (CLI-verb reference
      section + drift). All four operator-facing registry surfaces
      have docs entries + drift gates.
    - Testing axis: TB-205 + TB-210 closed all 8 env-knob shim rows;
      TB-211 + TB-212 closed all 8 event-type shim rows; TB-213 +
      TB-214 + TB-215 closed all 12 CLI-verb shim rows. TB-208 +
      TB-209 drift gates remain green with zero shim entries; future
      surface additions trip the substring gate at landing time.
    - Reusability axis: TB-204 (`_briefing_fixtures.py`, ~30+ inline
      fixtures deduplicated) + TB-209 (`_source_registry.py` for
      `_collect_cli_verbs`, 3rd-call-site threshold-three trigger).
    - Cleanness axis: untouched per goal.md L86-87 anti-speculative-
      refactor guardrail. No module-boundary clarity has surfaced
      from reading.
  - Gaps:
    (1) **Drift-gate sufficiency tightening still deferred.**
        `test_coverage_drift.py` docstring L26-32 explicitly defers
        the substring → AST-walk semantics check ("the test imports
        the symbol AND asserts against it") until "the substring gate
        is observed missing a real pro-forma gap." TB-211 retry
        verification_failed at 01:51:05Z was a TEST-shape issue
        (synthetic emit didn't drive the production code path), not a
        drift-gate miss — the test module existed and the gate passed
        at landing. So no pro-forma gap has been observed; the
        deferral stands.
    (2) **Cleanness axis (untouched)** — goal.md L86-87. Unchanged
        from prior cycle. `ap2/tools.py` past 3700 lines,
        `ap2/daemon.py` past 2500, `ap2/cli.py` past 1700 — but no
        module-boundary has surfaced from reading.
    (3) **2-call-site helpers awaiting threshold-three trigger.**
        `_collect_env_knobs`, `_collect_event_types`,
        `_all_agent_mcp_tool_short_names` are inlined in both
        `test_docs_drift.py` and `test_coverage_drift.py`
        (`_source_registry.py` L23-31). No 3rd reader has appeared.
        Premature extraction would re-trip goal.md L74-77's
        "premature abstraction is its own failure mode" guardrail.
  - Status: `in-progress`
  - Reasoning: The TB-208/TB-209 closure batch (24 surfaces / 6 task
    landings) substantively advanced testing coverage. The remaining
    axis gaps are either explicitly deferred (sufficiency tightening,
    threshold-three triggers) or guardrailed (cleanness/speculative-
    refactor). No high-leverage concrete gap surfaced this cycle.

## Non-goal risk check

None. All recent work stayed inside the four axes; no drift into
generic-task-scheduler / replace-operator-judgment / multi-tenancy /
real-time / cross-project axes.

## Considered & deferred this cycle

- **Surface `board_malformed_line` count in `ap2 status` (text + JSON)** —
  Prior cycle's secondary-layer candidate ("what slipped past the
  gate?"). Now that TB-216's write-time gate has shipped, the
  secondary layer becomes more relevant. BUT this is feature/
  observability work, not consolidation — adds a NEW operator-facing
  surface rather than tightening tests/docs on existing ones. Goal.md
  L38-46 explicitly pivots FROM accumulating signal TO consolidating
  foundations; this leans toward the wrong side. Defer until focus
  rotates or a real malformed-line incident slips past TB-216 and
  causes operator-visible damage.
- **TB-175-shape ideation-acceptance-rate insight aggregator** —
  Operator log L80 carry-deferral (2026-05-07): wait ~3+ ideation
  cycles after TB-188 lands so per-proposal records + TB-189 classify
  verdicts accumulate. TB-188 landed 2026-05-07; we're 7 days later;
  the records exist for TB-209/TB-211/.../TB-216. BUT zero `ap2
  classify` entries in operator_log.md — only accept/reject signal
  available, which is already in the prompt header. Re-deferring
  until classify verdicts surface or the operator explicitly engages.
- **Tighten `test_coverage_drift` substring gate to assert happy +
  error path parity** — goal.md L60-61's explicit testing-axis
  criterion. BUT the gate docstring L26-32 explicitly defers
  tightening until the substring gate is observed missing a real
  pro-forma gap. No such gap observed; deferral holds. Premature
  tightening would re-trip TB-172's wack-a-mole rejection shape
  (enumerate failure modes without a generalizable mechanism).
- **MM chat-verb / cron-job drift-gate (5th surface)** — Investigated:
  MM chat verbs aren't a uniformly-registered surface (the handler
  agent interprets natural-language dispatch), so no enumeration to
  gate against. Cron jobs in `cron.default.yaml` are only 1 entry
  (`status-report`); a drift gate of size 1 is overkill.
- **n=4 authoritative rejects** (TB-172/175/184/185) — Continued
  vigilance. This cycle's deferrals (malformed_line surface,
  acceptance-rate aggregator, drift-gate tightening) each match
  rejected shapes (feature-not-consolidation, defer-until-data,
  wack-a-mole). Better to ship zero this cycle than recapitulate.

## Cycle observations

- TB-208/TB-209 closure batch lands cleanly with the
  `test_tb<NNN>_<scope>.py` convention (one module per closure task,
  named after both the TB-N and the scope). The convention is
  emerging organically; not yet pinned as a meta-test. If 3+ future
  closure-batches follow the same shape, the meta-test (e.g. "every
  `test_tb<NNN>_*.py` references the matching TB-N in its docstring")
  becomes a threshold-three reusability candidate. Informs ranking of
  future proposals that touch the closure-batch shape.

## Decisions needed from operator

(none this cycle — prior cycle's TB-214 dead-letter ask was resolved
by the operator's manual title-rename + TB-216's gate landing.)

## Proposals this cycle

Backlog is currently at 0 workable items but no high-leverage gap
surfaced that fits the consolidation focus without recapitulating a
rejected shape (feature-not-consolidation, defer-until-data, wack-a-
mole). Proposing zero this cycle rather than manufacturing work.
Operator can `ap2 ideate --force` if intent shifts or run `ap2 add`
for specific direction.

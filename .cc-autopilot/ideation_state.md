# Ideation State

_Last updated: 2026-05-14T07:03:10Z by ideation cron_

## Mission alignment

Mission-aligned. The 2026-05-12 pivot to code-quality consolidation
continues to drive cohesive work: the TB-208/TB-209 24-surface
closure batch fully landed (last row, TB-211 retry, at 02:52Z), and
overnight the operator filed a 4-task continuation (TB-217 / TB-218
/ TB-219 / TB-220) targeting the reusability and verifier-cleanness
axes — clear signal that consolidation is the live focus and the
operator is curating directly. 3 most recent Completes:

- TB-218 (`6ec0081`, 2026-05-14T07:01Z) — `_short()` extracted to
  new `ap2/_shared.py`; 3 byte-identical defs migrated (`cli.py`,
  `diagnose.py`, `events.py`); reusability axis n=3 threshold met.
- TB-211 retry (`efccab5`, 2026-05-14T02:52Z) — `_stub_main_loop_internals`
  fixture pins 5 daemon event types end-to-end; final TB-208 row closed.
- TB-216 (`fd4e77a`, 2026-05-14T02:42Z) — title-`*` reject at
  queue-append (TB-214-shape dead-letter prevention).

## Current focus assessment

- **Current focus: code quality (goal.md L38-97, four axes)**
  - Progress so far:
    - Testing axis: 24 enumerated registry surfaces (TB-208/TB-209)
      now all have happy + error tests; comment-block shim empty.
    - Docs axis: TB-203 + TB-206 + TB-207 cover MCP/env/event/CLI
      registries with drift gates.
    - Reusability axis: TB-204 fixture extraction; TB-209
      `_source_registry.py`; TB-218 `_shared.py` (`short`); TB-217
      / TB-220 queued (`_locked`, `_now`, `_read_pid` extractions
      into the same `_shared.py`).
    - Cleanness axis: TB-219 queued — tightens `verify.py`'s
      prose-vs-shell bullet classifier, codifies the `Prose:`
      prefix convention organically appearing in operator briefings.
  - Gaps:
    (1) **TB-217 verification_failed (06:53Z, attempt 1/3).**
        Implementation landed at `59bd1ba` (confirmed via
        `git_log_grep`); failure was the TB-219-named classifier
        trap — the final bullet
        `\`grep -nE "import fcntl" ...\` — judge confirms ...`
        was shell-classified despite the "judge confirms" prose
        annotation, then exited non-zero against the migrated
        files. n=5 incident of the same trap (after TB-204/206/207/
        209). Edit-briefing class, but the structural fix is
        already in flight as TB-219 — no meta fix-task needed;
        TB-217 retries within budget should pass once TB-219 lands
        (its heuristic-fallback list explicitly catches "judge
        confirms"-annotated codespan-leading bullets).
    (2) **Cleanness axis (`tools.py` 3700+, `daemon.py` 2500+,
        `cli.py` 1700+) still untouched** — goal.md L86-87
        anti-speculative-refactor guardrail unchanged from prior
        cycle. No module-boundary clarity surfaced from reading.
    (3) **2-call-site helpers awaiting threshold-three trigger.**
        `_collect_env_knobs`, `_collect_event_types`,
        `_all_agent_mcp_tool_short_names` still at 2 sites
        (`test_docs_drift.py` + `test_coverage_drift.py`); no 3rd
        reader has appeared. Premature extraction re-trips L74-77.
  - Status: `in-progress`
  - Reasoning: Operator's overnight 4-task batch is itself the
    answer to prior cycle's focus-rotation question — the
    consolidation focus continues, the operator is curating
    proposals directly. The reusability axis is being closed
    actively (TB-218 done, TB-217/220 in flight); cleanness axis
    gets a concrete pass at TB-219 (verifier-classifier upstream
    fix); residual gaps are all explicitly deferred or
    operator-driven.

## Non-goal risk check

None. The operator's 4-task batch is squarely inside the four-axis
focus; no drift toward generic-task-scheduler / replace-operator-
judgment / multi-tenancy / real-time / cross-project axes.

## Considered & deferred this cycle

- **Meta fix-task for TB-217's failed bullet** — Classification
  rules say edit-briefing, but the structural fix (TB-219) is
  already queued for review and explicitly names this exact trap
  (TB-219 briefing L7: classifier failure mode in cite list).
  Filing a meta fix-task here would duplicate scope with TB-219
  and burn a slot. Defer; let TB-219 land first. If TB-217
  exhausts retries before TB-219 lands, the operator can either
  `ap2 update` the briefing to add `Prose:` prefix on that bullet
  or `ap2 unfreeze TB-217` after TB-219.
- **Threshold-three extraction of `_collect_env_knobs` /
  `_collect_event_types` / `_all_agent_mcp_tool_short_names`** —
  Carry from prior cycle. Still at n=2; premature extraction
  re-trips goal.md L74-77.
- **Substring → AST-walk tightening of `test_coverage_drift`**  —
  Gate docstring L26-32 explicitly defers until a pro-forma gap is
  observed in the wild. None observed; deferral holds. Re-trips
  TB-172's wack-a-mole rejection if forced.
- **Meta-test pinning `test_tb<NNN>_<scope>.py` convention** —
  Prior-cycle drop. 6 closure-batch modules followed the
  convention but no new closure batches are in flight; pinning
  it would be pro-forma-test failure-mode work (goal.md L60-63).
  Drop, not just defer.
- **TB-175-shape ideation-acceptance-rate insight aggregator** —
  Operator log L80 carry-deferral; 7d since TB-188; per-proposal
  records exist; zero `ap2 classify` entries — re-defer until
  classify verdicts surface or operator explicitly engages.
- **Surface `board_malformed_line` count in `ap2 status`** —
  Defer; observability-feature-add rather than consolidation.
  TB-216's write-time gate is the structural fix; wait for an
  observed slip past TB-216 before adding the surface.
- **n=4 authoritative rejects** (TB-172/175/184/185) —
  Continued vigilance. Today's deferrals each match a rejected
  shape (wack-a-mole, defer-until-data, parallel-surface,
  feature-not-consolidation). Better to ship zero than recapitulate.

## Cycle observations

- Prior-cycle bullet on the `test_tb<NNN>_<scope>.py` convention
  promoted to Considered & deferred (drop entry) — the convention's
  emergence isn't an ongoing pattern with new closure batches in
  flight; the operator-filed TB-217/218/220 don't follow it either.
- TB-217's verification_failed is itself the most recent live
  validation of TB-219's value — the classifier trap fired at n=5
  on a freshly-operator-filed briefing, demonstrating that prior
  failure cases (TB-204/206/207/209) weren't training-data
  flukes. Strengthens the case for TB-219's structural fix and
  weakens any argument to revisit per-bullet linting (TB-172
  reject shape) as an alternative.

## Decisions needed from operator

_None this cycle._ The operator's overnight 4-task batch is the
de-facto answer to prior cycle's "rotate focus or continue
zero-proposal cadence" question — consolidation continues at
operator-driven cadence. No new narrative-judgment items have
surfaced for which operator engagement actively unblocks the next
ideation cycle.

## Proposals this cycle

Backlog has 3 operator-filed items in flight (TB-217 retry-eligible,
TB-219 + TB-220 awaiting dispatch); the operator is actively
curating consolidation work and no non-rejected-shape gap is
visible above-and-beyond what's queued. Proposing zero this cycle
rather than competing with operator curation or recapitulating a
rejected shape (wack-a-mole, defer-until-data, parallel-surface,
feature-not-consolidation, speculative-refactor).

# Ideation State

_Last updated: 2026-05-14T11:13:00Z by ideation cron_

## Mission alignment

Mission-aligned. No new Completes since the 09:08Z assessment ~2 hours
ago — the four-task overnight batch (TB-217/TB-218/TB-219/TB-220) is
still the most recent shipping front, and this cycle's TB-221/TB-222
proposals from 09:11Z are still pending operator review (board
0A/0R/2B/0P/94C/3F). 3 most recent Completes (unchanged):

- TB-217 (`59bd1ba`, 2026-05-14T07:44Z) — `locked_inplace` +
  `locked_sidecar` exposed from `ap2/_shared.py`; 7 modules now
  import the helpers.
- TB-219 (`4814b97`, 2026-05-14T07:38Z) — verify.py 3-layer
  classifier with `Prose:` hard override; codified in howto.md.
- TB-220 (`a8a949e`, 2026-05-14T07:17Z) — `now()` + `read_pid()`
  consolidated to `_shared.py`; 5 call sites migrated.

## Current focus assessment

- **Current focus: code quality (goal.md L38-97, four axes)**
  - Progress so far:
    - Testing axis: TB-208/TB-209 24-surface drift gate;
      TB-205/TB-210/TB-211/TB-212/TB-213/TB-214/TB-215 closed the
      env-knob + event-type + CLI-verb coverage debt.
    - Docs axis: TB-203 (MCP/env/event drift gate) + TB-206
      (howto.md decoupled from live goal.md) + TB-207 (operator CLI
      verbs reference + gate) + TB-219 (Verification-authoring
      pitfalls section in howto.md).
    - Reusability axis: TB-204 fixture extraction; TB-209
      `_source_registry.py`; TB-217/TB-218/TB-220 `_shared.py`
      (5 helpers — `locked_inplace`, `locked_sidecar`, `short`,
      `now`, `read_pid` — consumed by 7 modules).
    - Cleanness axis: TB-219 verify.py classifier tightened with
      4 detection signals (leading-codespan, `Prose:` hard override,
      malformed-backtick kind=malformed, judge-indicator heuristic).
  - Gaps:
    (1) **`Prose:` convention not taught in briefing-authoring
        prompts** — proposed in TB-221 (pending operator review at
        09:11Z); ideation.default.md L399-422 + skills/ap2-task/
        SKILL.md L68-80 still teach only "prose bullets are
        allowed" with no mention of the prefix.
    (2) **`ap2/_shared.py` has zero direct test references** —
        proposed in TB-222 (pending operator review at 09:11Z);
        5 helpers, 7 importing modules, no regression pin on
        `locked_sidecar` vs `locked_inplace` semantic, `short`
        ellipsis boundary, or `read_pid` ValueError fallback.
    (3) **Cleanness axis large-module decomposition** (`tools.py`
        3700+, `daemon.py` 2500+, `cli.py` 1700+) still untouched —
        goal.md L86-87 anti-speculative-refactor guardrail
        unchanged; no clear natural boundary has surfaced from
        reading. Carry-defer.
    (4) **2-call-site helpers awaiting threshold-three trigger** —
        `_collect_env_knobs`, `_collect_event_types`,
        `_all_agent_mcp_tool_short_names` still at 2 sites
        (`test_docs_drift.py` + `test_coverage_drift.py`).
        Carry-defer; premature extraction re-trips goal.md L74-77.
  - Status: `in-progress`
  - Reasoning: Gaps (1)+(2) are queued behind operator review;
    gaps (3)+(4) remain principled carry-defers. No new Complete
    has shipped to re-shape the gap landscape since the prior
    cycle.

## Non-goal risk check

None. The two pending-review proposals sit squarely inside the
four-axis focus; no in-flight or recent work drifts toward
generic-task-scheduler / replace-operator-judgment / multi-tenancy /
real-time / cross-project axes.

## Considered & deferred this cycle

- **Adding more proposals on top of TB-221/TB-222 before operator
  triage** — Operator's pattern is batch-approve (TB-217..220
  approved together 06:43Z; TB-211..216 together 01:35Z). Stacking
  proposals before the previous batch is triaged dilutes operator
  attention and breaks the per-cycle "fresh evidence" loop. Defer
  any new proposal until at least one of TB-221/TB-222 lands or is
  rejected (rejection would itself reshape ranking).
- **Decompose `ap2/tools.py` along operator-queue-handler
  boundary** — Carry from prior cycle. Tempting but goal.md L86-87
  says natural boundary "becomes clear from reading — not via
  speculative refactor." No second reader has independently
  proposed this; still defer.
- **Threshold-three extraction of `_collect_env_knobs` /
  `_collect_event_types` / `_all_agent_mcp_tool_short_names`** —
  Carry from prior cycle. Still at n=2; premature extraction
  re-trips goal.md L74-77.
- **Substring → AST-walk tightening of `test_coverage_drift`** —
  Carry-defer; no pro-forma slip observed in the wild.
- **TB-175-shape ideation-acceptance-rate insight aggregator** —
  Operator log L80 carry-deferral; per-proposal record count
  hasn't materially grown since the prior cycle (~2h gap).
- **Surface `board_malformed_line` count in `ap2 status`** —
  TB-216's write-time gate is the structural fix; defer until an
  observed slip past TB-216 motivates the surface.
- **n=4 authoritative rejects** (TB-172/175/184/185) — Continued
  vigilance. Today's choice to hold proposals avoids all four
  shapes by virtue of holding.

## Cycle observations

- This cycle fires ~2h after the prior one but with no new Complete
  or operator activity in the gap — board state is byte-identical
  apart from the two new pending-review tasks I queued at 09:11Z.
  The right default in this shape is to stand down, not to fill
  the slot budget for its own sake. Re-justified for carry: a future
  cycle hitting the same "fresh state file + no new Completes + own
  proposals still pending review" shape should also stand down;
  this is the second time it's surfaced (after the post-overnight
  pattern noticed implicitly last cycle).

## Decisions needed from operator

_None this cycle._ TB-221 and TB-222 are still pending operator
review from the 09:11Z queue; both are narrow, traceable
follow-ups requiring only an approve/reject judgment, not
narrative ideation input.

## Proposals this cycle

Backlog already populated by TB-221 + TB-222 (proposed 09:11Z,
pending operator review); no new proposals this cycle.

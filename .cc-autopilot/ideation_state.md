# Ideation State

_Last updated: 2026-05-14T09:08:00Z by ideation cron_

## Mission alignment

Mission-aligned. The operator's overnight 4-task batch (TB-217 /
TB-218 / TB-219 / TB-220) all closed clean between 07:01-07:44Z —
the reusability axis (`ap2/_shared.py` extractions of `_locked`,
`_short`, `_now`, `_read_pid`) and the cleanness axis (verify.py
prose-vs-shell classifier with `Prose:` prefix convention) are now
on disk. Backlog is empty (0A/0R/0B/0P/94C/3F) — first post-batch
cycle. 3 most recent Completes:

- TB-217 (`59bd1ba`, 2026-05-14T07:44Z, retry pass) — `locked_inplace`
  + `locked_sidecar` exposed from `ap2/_shared.py`; 7 modules now
  import from it (`board/cli/cron/diagnose/events/retry/web`).
- TB-219 (`4814b97`, 2026-05-14T07:38Z) — verify.py 3-layer
  classifier with `Prose:` hard override; codified in howto.md.
- TB-220 (`a8a949e`, 2026-05-14T07:17Z) — `now()` + `read_pid()`
  consolidated to `_shared.py`; 5 call sites migrated.

## Current focus assessment

- **Current focus: code quality (goal.md L38-97, four axes)**
  - Progress so far:
    - Testing axis: TB-208/TB-209 24-surface drift gate; TB-205/
      TB-210/TB-211/TB-212/TB-213/TB-214/TB-215 closed the env-knob
      + event-type + CLI-verb coverage debt (shim block empty).
    - Docs axis: TB-203 (MCP/env/event drift gate) + TB-206
      (howto.md decoupled from live goal.md) + TB-207 (operator CLI
      verbs reference + gate) + TB-219 (Verification-authoring
      pitfalls section in howto.md).
    - Reusability axis: TB-204 fixture extraction; TB-209
      `_source_registry.py`; TB-217/TB-218/TB-220 `_shared.py`
      (5 helpers: `locked_inplace`, `locked_sidecar`, `short`,
      `now`, `read_pid`); 7 modules now consume the shared module.
    - Cleanness axis: TB-219 verify.py classifier tightened with
      4 detection signals (leading-codespan, `Prose:` hard override,
      malformed-backtick kind=malformed, judge-indicator
      heuristic).
  - Gaps:
    (1) **`Prose:` convention NOT taught in briefing-authoring
        prompts.** TB-219 added the prefix to verify.py (hard
        override) and `ap2/howto.md` (operator-facing), but
        `ap2/ideation.default.md` L399-422 and
        `skills/ap2-task/SKILL.md` L68-80 still teach only "prose
        bullets are allowed" with no mention of the `Prose:`
        prefix — so future ideation-authored and task-authored
        briefings will keep hitting the classifier-trap shape
        TB-219 was built to catch (n=5 incident: TB-204/TB-206/
        TB-207/TB-209/TB-217). Closing this is a docs-axis
        follow-up; without it, TB-219's hard-override path
        rarely fires because no author writes the prefix.
    (2) **`ap2/_shared.py` has zero direct test references.**
        `grep -rn "from ap2._shared" ap2/tests/` returns nothing.
        5 helpers with 7 importing modules now ride on
        contract-by-implication; `locked_sidecar` vs
        `locked_inplace` semantic confusion (a real on-disk
        difference per the module docstring L1-29) has no
        regression pin, nor does `short`'s ellipsis boundary nor
        `read_pid`'s ValueError fallback. Goal.md L58-63 testing
        axis says happy + error path for "every shipped … behavior";
        these are infrastructure that 7 modules now depend on.
        Testing-axis follow-up to TB-217/TB-218/TB-220.
    (3) **Cleanness axis (`tools.py` 3700+, `daemon.py` 2500+,
        `cli.py` 1700+) still untouched** — goal.md L86-87
        anti-speculative-refactor guardrail unchanged. No clear
        natural boundary has surfaced from reading; carry-defer.
    (4) **2-call-site helpers awaiting threshold-three trigger.**
        `_collect_env_knobs`, `_collect_event_types`,
        `_all_agent_mcp_tool_short_names` still at 2 sites
        (`test_docs_drift.py` + `test_coverage_drift.py`).
        Carry-defer; premature extraction re-trips L74-77.
  - Status: `in-progress`
  - Reasoning: Operator's overnight batch closed cleanly and
    surfaced two narrow, traceable gaps (Prose: convention not
    in author prompts; `_shared.py` lacks direct tests) — both
    direct goal-axis follow-ups to just-landed work, neither
    matching a rejection-shape from the rolling vigilance list.

## Non-goal risk check

None. Both gaps sit squarely inside the four-axis focus; no drift
toward generic-task-scheduler / replace-operator-judgment /
multi-tenancy / real-time / cross-project axes.

## Considered & deferred this cycle

- **Decompose `ap2/tools.py` along operator-queue-handler boundary** —
  Tempting (clear cluster: `do_operator_queue_append`,
  `_apply_operator_*`, `OPERATOR_QUEUE_OPS` dispatch) but goal.md
  L86-87 says natural boundary "becomes clear from reading — not
  via speculative refactor". No second reader has independently
  proposed this; defer until the boundary's necessity is
  unambiguous.
- **Document `ap2/_shared.py` in `ap2/architecture.md`** — Weak
  delete-test: the threshold-three convention already lives in
  goal.md L74-77; an architecture.md restatement is mostly
  redundant. Folded into the test-coverage proposal's `## Design`
  notes instead of a standalone task.
- **Threshold-three extraction of `_collect_env_knobs` /
  `_collect_event_types` / `_all_agent_mcp_tool_short_names`** —
  Carry from prior cycle. Still at n=2; premature extraction
  re-trips goal.md L74-77.
- **Substring → AST-walk tightening of `test_coverage_drift`** —
  Carry-defer; no pro-forma slip observed in the wild.
- **TB-175-shape ideation-acceptance-rate insight aggregator** —
  Operator log L80 carry-deferral; only 10 per-proposal records
  on disk so far; insufficient mass.
- **Surface `board_malformed_line` count in `ap2 status`** —
  TB-216's write-time gate is the structural fix; defer until an
  observed slip past TB-216 motivates the surface.
- **n=4 authoritative rejects** (TB-172/175/184/185) — Continued
  vigilance. Today's two proposals avoid all four shapes:
  not enumerative wack-a-mole (one teaches a single convention,
  one backfills tests for a fresh module), not defer-until-data,
  not parallel-surface, not feature-add-disguised-as-consolidation.

## Cycle observations

- The n=5 classifier-trap incident (TB-204/TB-206/TB-207/TB-209/
  TB-217) made TB-219's hard-override path concrete, but the
  override is opt-in at author time — without prompt-side
  teaching, future briefings stay on the heuristic-fallback path
  and the n=6 incident is one operator-batch away. Promotes to
  proposal 1 (TB-221).
- `ap2/_shared.py` is the first cross-module helper added in
  weeks; previous shared infra (test helpers under
  `ap2/tests/_*.py`) is test-side. The convention for what
  belongs in `_shared.py` lives in the module docstring + goal.md
  L74-77; capturing it via tests pins the contract where future
  helpers will be checked.

## Decisions needed from operator

_None this cycle._ The two proposals are narrow, traceable
follow-ups to operator-curated work — neither requires narrative
judgment from the operator beyond approve/reject.

## Proposals this cycle

- **TB-221** — Teach `Prose:` prefix convention in
  `ap2/ideation.default.md` + `skills/ap2-task/SKILL.md`
  (docs-axis follow-up to TB-219, closes gap (1)).
- **TB-222** — Direct happy + error path tests for
  `ap2/_shared.py` helpers (testing-axis follow-up to TB-217/
  TB-218/TB-220, closes gap (2)).

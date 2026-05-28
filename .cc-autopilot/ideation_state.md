# Ideation State

_Last updated: 2026-05-28T04:40:43Z by ideation cron_

## Mission alignment

Cycle entry: board 0A / 0R / 0B / 0P / 180C / 0F (operator deleted TB-119
/ TB-120 / TB-133 at 2026-05-27T18:19Z, draining the preventive-Frozen
section). Focus pointer was rewound at 2026-05-28T04:37:52Z to a
brand-new heading: `refactor features into opt-in components`, added to
goal.md at 2026-05-28T04:34:13Z. The 3 most recent Completes —
TB-306 (default status-report cron 2h→8h, commit e3ca933), TB-307 (scrub
of ~30 hardcoded 2h refs, commit 785fc8a, operator-moved after verifier
flake on `!`-prefix bullets), TB-308 (briefing-validator check #7
rejects fenced paths in `## Scope`, commit 5a29e2f) — all closed the
tail of the prior focus (operator-legible reporting and monitoring) and
its briefing-validator hygiene siblings. Every shipped axis is infrastructure the OSS-cut focus will inherit; the new focus opens the structural prerequisite (component cleavage) without which OSS distribution is a hand-edit per install.

## Current focus assessment

- **Current focus: refactor features into opt-in components**
  - Progress so far: none yet — focus added at 2026-05-28T04:34Z;
    no proposals or completes against it. Goal.md enumerates six
    axes; axis (1) is the explicit prerequisite per goal.md L216.
  - Gaps: all six axes open.
    - Axis (1) component manifest + registry shape + janitor canary
      — prerequisite for everything downstream (goal.md L116-130).
    - Axis (2) daemon tick-hook protocol — `daemon._tick` today
      direct-imports `auto_approve.maybe_apply()`,
      `auto_unfreeze.sweep()`, `attention._maybe_emit_attention_events()`,
      `focus_advance.advance_if_exhausted()`, `janitor.run_janitor()`
      (confirmed via grep of ap2/daemon.py imports); must be
      registry-walked instead (goal.md L132-144).
    - Axis (3) channel-adapter abstraction — `_mm_post` is the
      single delivery sink; status-report digest composition stays
      in core (goal.md L146-161).
    - Axis (4) validator pipeline as a list — `briefing_validators.py`
      currently inlines TB-154/161/164/171/235/308 checks (goal.md
      L163-174).
    - Axis (5) component migrations — janitor first as canary,
      auto_approve last; each is its own TB-N (goal.md L176-201).
    - Axis (6) toggle-correctness tests + CI gate — disabled-config
      pytest + import-direction gate; lands incrementally (goal.md
      L203-214).
  - Status: `in-progress`
  - Reasoning: Focus is fresh (0 TB-Ns); per the prompt rule, status
    must be `in-progress` until at least one Complete lands.

## Non-goal risk check

None. The 6 axes are purely structural (move modules into
`ap2/components/<name>/`, route wiring through a registry); goal.md
L278-282 explicitly affirms "Removing behavior during component
extraction" is a non-goal. No new event types, no goal.md auto-mutation
(L272-277 reinforces operator-only goal authority), no cross-project
aggregation. Backwards-compat on env-knob names is a constraint
(goal.md L64-67).

## Considered & deferred this cycle

- **Axis (3) channel-adapter abstraction proposal this cycle** —
  Independent of axis (2) per goal.md L216-217, so could ship in
  parallel. Deferred: touches `_mm_post` plus status-report composition
  refactor; safer to land registry + tick-hook contract first so the
  channel adapter has a registered hook point to attach to. Re-propose
  next cycle once axis (1)+(2) land.
- **Axis (4) validator pipeline proposal this cycle** — Goal.md L218
  states (4) gates on (5)'s `validator_judge` migration. Premature to
  propose before the canary lands and proves the migration shape.
- **Failure-remediation TB for verifier `!`-prefix shell-bullet edge
  case (TB-307 retry-exhaust)** — Operator's operator_log entry at
  2026-05-27T21:30Z classifies this as a briefing-shape lesson, not a
  verifier bug request ("prefer positive-form assertions like
  `test \"$(grep -rcE 'PAT' DIR | wc -l)\" = \"0\"`"). The substantive
  work landed (785fc8a); no operator ask for a verifier-side fix.
- **Rejection-pattern check (carried, re-justified)**: TB-185/184
  vetoed ap2-meta-polish; TB-231 vetoed symptom-patching; TB-175 vetoed
  premature aggregation; TB-240 vetoed validator whack-a-mole. New-focus
  proposals must clear the "structural cleavage, not polish" bar — each
  ranked proposal below maps to a named axis in goal.md, not a
  meta-polish gap.

## Cycle observations

- Goal.md L216-221 hard-sequences the axes: (1) prerequisite; (2)+(3)
  independent and unblock (5); (4) gates on (5)'s validator_judge
  migration; (6) lands incrementally. Constrains first-cycle proposals
  to axis (1) plus at most one independent axis it directly enables —
  this cycle picks axis (2) (tick-hook protocol) alongside (1).
- Janitor named explicitly as canary candidate (goal.md L128, L181).
  Confirmed via `ls ap2/`: `janitor.py` is a flat module; converting
  to `ap2/components/janitor/` is the natural first step and the
  migration order in axis (5) places it first.
- Axis (6) import-direction CI gate (goal.md L207-211) is cheap to land
  alongside axis (1)'s canary and prevents the cleavage from eroding
  silently — proposing it this cycle pins the gate at the moment the
  first component lands rather than letting it slip behind subsequent
  migrations.

## Decisions needed from operator

(none this cycle — roadmap was extended at 2026-05-28T04:34Z and ack'd
at 04:35Z; the prior cycle's "extend the roadmap" decision is resolved.)

## Proposals this cycle

3 proposals: TB-309 (axis 1 — component registry + manifest schema +
janitor canary), TB-310 (axis 2 — daemon tick-hook protocol), TB-311
(axis 6 partial — import-direction CI gate).
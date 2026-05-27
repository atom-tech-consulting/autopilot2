# Documentation sweep: README.md + architecture.md + howto.md updates for today's arc

Tags: #autopilot #docs #regression-pin

## Goal

Surgical updates to three operator/agent-facing docs files
(`ap2/README.md`, `ap2/architecture.md`, `ap2/howto.md`) to reflect
the post-2026-05-27 state of the focus-advance + attention-detector +
ideation-toolset arcs. Closes the goal.md `## Done when` failure mode
"Ideation reliably proposes goal-aligned next steps that
substantively advance the goal (not just goal-shaped pro-forma
compliance)" — these docs are read by both operators (manual lookup)
and future agents (ideation reads README/architecture/howto via the
control-agent prompt's `Read`-tool surface); stale docs cause
incorrect framing of the mechanism (e.g., describing the empty-cycles
counter as flat-walking `ideation_empty_board` + `ideation_complete`
events, when post-TB-292 + TB-300 it's cycle-grouped over BOTH
`ideation_complete` AND `ideation_cycle_summary` exit markers).

Why now: post-2026-05-27 audit of the three doc files identified 9
surgical staleness gaps left after today's TB-287 → TB-302 arc shipped
its mechanism + bug-fix + new-verb changes. The TBs each landed their
own load-bearing docs (e.g., the `ap2 rewind-focus` verb DID land in
`howto.md:668`), but several adjacent surfaces in README and
architecture weren't touched. Without this sweep, operators reading
the docs get an inconsistent picture of (a) the counter semantics,
(b) the trigger-value vocabulary, (c) the ideation toolset fence,
and (d) the operator-CLI verbs available for recovery.

## Scope

(1) `ap2/README.md` — CLI reference table (around L54-77): add a row
for `ap2 rewind-focus TITLE [--reason TEXT]` mirroring the existing
row style. Text: "Re-engage an exhausted `## Current focus:` heading
(TB-295). Atomically updates `focus_pointer.json`, emits synthetic
`focus_advanced trigger=operator_rewind` so the empty-cycles counter
respects the rewind, logs to operator_log.md. Title-as-key, resolved
to index at drain time."

(2) `ap2/README.md` — Event schema list (around L147-153): add the
event types introduced or formalized in today's arc to their
appropriate categories. Specifically:
  - Lifecycle: `focus_advanced`, `roadmap_complete`,
    `ideation_cycle_summary` (the agent-emitted exit marker for
    no-proposal cycles that TB-300 made the counter recognize).
  - State/observability: `attention_raised` (TB-282),
    `ideation_state_scrubbed` (TB-284), `ideation_state_scrub_error`
    (TB-294 fail-audit), `attention_pushed` /
    `attention_push_error` / `attention_push_no_destination`
    (TB-297).
Group consistently with the existing list shape — single line with
parenthetical attribution if a TB-N anchor is short.

(3) `ap2/README.md` — MCP tool partition narrative (around L157-167):
add a sentence noting TB-291's `IDEATION_TOOLS` subset. Suggested
phrasing: "Ideation runs with a narrower toolset
(`IDEATION_TOOLS`; TB-291) — `CONTROL_AGENT_TOOLS` minus
`operator_queue_append`. The TOCTOU defense the queue path provides
is unnecessary during ideation, which only fires when Active == 0;
fencing ideation off `operator_queue_append` keeps the proposal-path
event vocabulary 1:1 with `ideation_proposal_recorded` (the
empty-cycles counter's reset signal)."

(4) `ap2/architecture.md` — Agent kinds table (around L56-61): update
the Ideation row's "Tools" cell from `CONTROL_AGENT_TOOLS` to
`IDEATION_TOOLS` with a brief parenthetical
"(CONTROL_AGENT_TOOLS minus operator_queue_append; TB-291)".

(5) `ap2/architecture.md` — Tool-pool code block (around L268-298):
add an `IDEATION_TOOLS` definition after `MM_HANDLER_TOOLS`. Suggested
shape:
```python
# IDEATION_TOOLS = CONTROL_AGENT_TOOLS minus { operator_queue_append }
# (TB-291). Ideation only fires when Active == 0, so the queue-path
# TOCTOU defense is unnecessary; fencing keeps the proposal-path
# event vocabulary single-channel (ideation_proposal_recorded) for
# the empty-cycles counter's reset signal.
```

(6) `ap2/architecture.md` — Roadmap exhaustion section (around L344):
add a sentence pointing operators to `ap2 rewind-focus TITLE` as the
canonical recovery path for re-engaging a falsely-advanced or
substantively-incomplete exhausted focus. Suggested phrasing:
"Operator can re-engage a previously-exhausted focus via
`ap2 rewind-focus TITLE [--reason TEXT]` (TB-295), which atomically
resets the pointer + emits a synthetic
`focus_advanced trigger=operator_rewind` event so the empty-cycles
counter cutoff respects the rewind. Direct
`.cc-autopilot/focus_pointer.json` edits are NOT supported — they
produce no event and leave pre-rewind empty cycles counting against
the rewound focus's window."

(7) `ap2/architecture.md` — Test count (around L377): replace
"~349 tests" with the current count (`uv run pytest --collect-only -q
ap2/tests/ | tail -1`), or drop the specific number with "the full
suite (currently 2000+ tests)" framing. Operator-author's choice; the
key is no specific stale number.

(8) `ap2/howto.md` — Counter-semantics paragraph (around L1917-1928):
rewrite the parenthetical clause naming the events that drive the
counter. Currently:
```
the daemon counts consecutive recent ideation cycles that produced 0
proposals against the active focus (`ideation_empty_board` +
`ideation_complete` events with zero recorded proposals, reset by
`ideation_proposal_recorded`)
```
Updated to reflect TB-292's cycle-grouped semantics + TB-300's
recognition of both exit markers:
```
the daemon counts consecutive recent ideation cycles that produced 0
proposals against the active focus. Each cycle is delimited by an
`ideation_empty_board` entry marker (daemon-emitted at cycle start
regardless of outcome) and one of `ideation_complete` or
`ideation_cycle_summary` (agent-emitted exit marker — `_complete`
when the cycle proposed at least one task, `_cycle_summary` when no
proposals). The counter increments at the exit marker if no
`ideation_proposal_recorded` fired within the cycle; resets to 0 if
any proposal fired. `ideation_timeout` / `ideation_error` exits
don't count (infrastructure failure ≠ "ideation reasoned and found
nothing").
```

(9) `ap2/howto.md` — Trigger-field comment (around L1950-1955): the
current text claims trigger is "single-valued post-TB-283". TB-295
added `operator_rewind` as a second value. Updated to:
```
The `trigger` field carries two values today: `empty_cycles_heuristic`
(natural auto-advance after N consecutive empty cycles) and
`operator_rewind` (synthetic event emitted by `ap2 rewind-focus` so
the counter's cutoff scan recognizes the rewind boundary; TB-295).
A third value, `pointer_past_last`, appears on the `roadmap_complete`
event (not `focus_advanced`) when the pointer crosses past the final
focus heading.
```

## Design

Pure documentation sweep. No code changes, no test changes. Each edit
is surgical: a new table row (1), additions to an existing list (2,
5), a sentence addition (3, 6, 7), a cell value update (4), or a
paragraph rewrite (8, 9). The agent should perform all 9 edits in a
single pass — they're independent and don't conflict.

The two highest-value edits are (8) (counter semantics — describes
the load-bearing mechanism today's TB-292 + TB-300 fixed) and (1)
(`ap2 rewind-focus` reference — operator-recovery surface that today's
TB-295 introduced).

No skill-file edits needed — the audit confirmed `skills/ap2/SKILL.md`,
`skills/ap2-task/SKILL.md`, and `skills/migrate-to-ap2/SKILL.md` are
clean (no stale post-2026-05-27 terms; the one "Done when" reference
in ap2-task is correct because it refers to the GLOBAL `## Done when`
section, not the per-focus `Progress signals:` sub-block that TB-285
renamed).

## Verification

- `grep -q 'ap2 rewind-focus' ap2/README.md` — CLI reference row added.
- `grep -q 'attention_raised' ap2/README.md` — event added to schema list.
- `grep -q 'IDEATION_TOOLS' ap2/README.md` — toolset narrative updated.
- `grep -q 'IDEATION_TOOLS' ap2/architecture.md` — tool-pool code block updated.
- `grep -q 'ap2 rewind-focus' ap2/architecture.md` — roadmap exhaustion section references the recovery verb.
- `! grep -q '~349 tests' ap2/architecture.md` — stale test count removed.
- `grep -q 'ideation_cycle_summary' ap2/howto.md` — counter-semantics paragraph names both exit markers.
- `! grep -q 'single-valued post-TB-283' ap2/howto.md` — stale trigger-field claim removed.
- `grep -q 'operator_rewind' ap2/howto.md` — trigger vocabulary updated.
- `uv run pytest -q` — full suite passes (no code changes; this guards against an accidental code-file edit slipping in).

## Out of scope

- Skill files (`skills/ap2/SKILL.md`, `skills/ap2-task/SKILL.md`,
  `skills/migrate-to-ap2/SKILL.md`) — already audited clean.
- Deeper restructure of `ap2/howto.md` — only the two stale
  paragraphs change; the surrounding structure stays.
- `ap2/ideation.default.md` audit — separate scope (the prompt itself
  may have its own stale references; if so, file a follow-up TB).
- `goal.md` — already updated earlier in the session via
  `ap2 update-goal` (rename + prose rewrite).
- Adding new sections / headings to any of the three files — only
  edits within existing sections.
- Backfilling per-TB historical references — the docs name the
  TB-N anchors for the relevant fixes (TB-291, TB-292, TB-295, TB-300,
  etc.); deeper provenance lives in the briefings + git history.

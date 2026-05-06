# Fix `## Open questions for operator` schema — require actionable decisions, add `## Cycle observations` with triage discipline

Tags: #autopilot #ideation #prompts #bug-fix

## Goal

The ideation prompt's `## Open questions for operator` section (`ap2/ideation.default.md` lines 73-76) is producing operator-facing noise. The current schema permits any "noticed gap" to land there — pattern-tracking notes, behavioral observations about operator cadence, metric updates ("n=3"), and pure status reports ("No unadopted cron_proposed events"). None of these are questions; none require operator input; none have an action attached. The agent narrates rather than asks.

The downstream amplification is severe: TB-173 forwards this section verbatim into BOTH `ap2 status` and the 6-hourly Mattermost cron post. Operators see operator-actionable content in the same noise channel as agent-internal observations, learn to skip the section, and eventually miss genuine decisions that need their input. The walk-away surface is degraded by content that doesn't belong on it.

The root cause is two-part: (a) the schema is permissive ("noticed a gap" lets anything in), and (b) the agent has nowhere else to record observations that don't fit the existing structured sections (Mission alignment, Current focus assessment, Considered & deferred — all have stricter schemas requiring TB-N citations or specific status enums). Observation-shaped content gravitates to the most permissive section.

This task fixes both halves: tighten the operator-facing section to admit only actionable decisions (with explicit redirect instructions for misplaced content), AND add a new agent-internal section with mandatory cycle-by-cycle triage so the agent's working notes have a home that doesn't leak to operator surfaces and doesn't accumulate stale narration over time.

## Scope

- `ap2/ideation.default.md` — schema rewrites:
  - Rename `## Open questions for operator` → `## Decisions needed from operator`. Tighten the schema description to require each bullet to be either a direct question (`?`-terminated) or prefixed `Decision needed:` / `Operator input required:`. Each bullet must articulate (a) the specific operator action, (b) the unblock-condition (what changes about the next cycle if the operator answers). Add explicit prohibitions against status observations, pattern-tracking notes, behavioral commentary, and metric updates. Constrain `(carried)`: bullets carrying across cycles MUST re-articulate the action; copy-paste is forbidden.
  - Add new section `## Cycle observations` AFTER `## Considered & deferred this cycle`. Schema: agent-internal working notes, NOT forwarded to operator-facing surfaces. Mandatory triage discipline (decision tree on each prior-cycle bullet; default disposition is DROP unless explicitly re-justified). Hard cap at 10 bullets. Hard prohibitions against operator-actionable content, pure status reporting, recurring "no X events" negative observations.
- `ap2/ideation.py::parse_open_questions` — rename to `parse_operator_decisions`. Update the heading-match regex to look for `## Decisions needed from operator`. Add a defensive guard that the function ignores `## Cycle observations` content even if structurally adjacent.
- `ap2/cli.py`, `ap2/web.py`, `ap2/status_report.py` — update import sites for the renamed parser (`parse_open_questions` → `parse_operator_decisions`). Update the user-visible label strings ("open questions for operator" → "decisions needed") for consistency.
- `ap2/tests/test_ideation_defaults.py` — schema regression tests pinning the rename, the actionability requirements, the prohibitions, and the triage discipline language.
- `ap2/tests/test_ideation.py` (or wherever the parser is tested) — fixture with BOTH sections present; assert the parser returns ONLY the Decisions-needed bullets and IGNORES the Cycle-observations bullets.
- `ap2/tests/test_status_report.py` — fixture covering the surfacing path; assert Cycle observations content does NOT appear in the rendered cron-post bullets even when present in ideation_state.md.

## Design

### Decisions section schema (operator-facing)

```
## Decisions needed from operator

Each bullet MUST satisfy ALL of:
- Direct question (`?`-terminated) OR explicit prefix `Decision needed:` /
  `Operator input required:`
- Names the specific operator action (`ap2 approve TB-N` / "edit goal.md to
  add Y" / "decide between approach A vs B")
- Names the unblock-condition (what changes about the next ideation cycle
  if the operator engages)

DO NOT include:
- Status observations ("No X events", "Cadence is steady")
- Pattern-tracking notes ("n=3 retries on bullet kind Y")
- Behavioral commentary about the operator
- Metric updates without a corresponding decision
- Items where the operator would need no input even if they read the bullet

(Carried) discipline: a bullet may carry from the prior cycle ONLY if you
re-articulate the operator action and unblock-condition this cycle. Pure
copy-paste of last cycle's text is forbidden.
```

### Observations section schema (agent-internal)

```
## Cycle observations

Agent-internal working notes. NOT forwarded to operator-facing surfaces
(ap2 status, Mattermost cron post). Use this for observations that
informed THIS cycle's assessment but don't fit a structured section.

Triage discipline (when writing this section):
1. Read the prior cycle's `## Cycle observations` bullets FIRST.
2. For each prior bullet, decide:
   - Situation changed? → drop (mention change in Mission alignment if
     cross-cutting)
   - Now fits a structured section? → promote there (Mission alignment,
     Current focus > Gaps, Considered & deferred); do NOT also keep here
   - Stale / no longer informs current reasoning? → drop
   - Still actively informing reasoning AND no better home? → carry,
     with a one-sentence re-justification of why this cycle still needs it
3. Default disposition: DROP. Only carry if explicitly re-justified.

Hard cap: 10 bullets max. If you can't triage to 10, the discipline is
slipping.

Hard prohibitions:
- NEVER an item the operator should act on (use `## Decisions needed
  from operator`)
- NEVER pure status reporting (events.jsonl covers that)
- NEVER recurring "no X events" / "no operator activity" type bullets
```

### Why rename instead of just tightening the description

The current section name "Open questions for operator" is misleading — operators read it expecting questions but find observations. Tightening the description without renaming would still leave the title-vs-content mismatch. The rename is what makes the title load-bearing as a constraint: if it says "Decisions needed," the agent has explicit prompt-side incentive to stop dumping observations.

### Why a hard cap on Cycle observations

Without a cap, the section accumulates over cycles even with triage discipline — agents are bad at being ruthless about what's relevant. A hard ceiling forces the agent to drop or promote rather than carry. 10 bullets is the rough threshold where a section is still scannable; beyond that the agent IS narrating, not observing.

### Why the parser rename

`parse_open_questions` becomes `parse_operator_decisions`. Internal-API rename; same shape (returns `list[str]` of bullet text). Three call sites update; no behavioral change for them. The parser ignoring `## Cycle observations` is structural (heading-match), but a defensive guard + test prevents regressions if a future schema change moves the section adjacent.

### Backwards compatibility

- Pre-this-task `ideation_state.md` files have the old section name and old content shape. The next ideation cycle reads its prior cycle as input; if it sees the old "Open questions for operator" heading, the agent should map it to the new "Decisions needed from operator" structure on rewrite — and triage out content that doesn't fit the new schema. Effectively a one-cycle migration; no backfill needed.
- Operators reading `ap2 status` get the new heading text ("decisions needed") in the surfacing line. Visible change but not a behavioral break.
- The Mattermost cron post's bullet wording changes accordingly. Visible to chat readers but expected.

## Verification

- `uv run pytest -q ap2/tests/` — full regression gate passes.
- `grep -nE "Open questions for operator" ap2/ideation.default.md` — should return ZERO matches (rename complete).
- `grep -nE "Decisions needed from operator" ap2/ideation.default.md` — at least one match (the new section heading + schema instructions).
- `grep -nE "Cycle observations" ap2/ideation.default.md` — at least one match (the new agent-internal section).
- `grep -nE "parse_operator_decisions" ap2/ideation.py ap2/cli.py ap2/web.py ap2/status_report.py` — function defined in ideation.py AND imported by all three caller surfaces.
- `grep -nE "parse_open_questions" ap2/` — should return ZERO matches outside of test files validating the rename happened cleanly.
- prose: a test in `test_ideation_defaults.py` loads `ap2/ideation.default.md` and asserts the `## Decisions needed from operator` section's body contains all of: a "?-terminated OR prefix" requirement, an "articulate the specific operator action" requirement, a list of prohibited content kinds (status observations, pattern-tracking, behavioral commentary, metric updates), and the (carried)-discipline language.
- prose: a test pins the new `## Cycle observations` section's body — contains the triage-decision-tree language, the 10-bullet hard cap, the "default disposition is DROP" instruction, and the hard prohibitions against operator-actionable content + pure status reporting + recurring negative-observation bullets.
- prose: a test in `test_ideation.py` synthesizes a fixture `ideation_state.md` containing both `## Decisions needed from operator` (with two valid bullets) AND `## Cycle observations` (with three observation-shaped bullets); calls `parse_operator_decisions(path)`; asserts the returned list contains exactly the two decisions bullets and NONE of the observations content.
- prose: a test in `test_status_report.py` (or the equivalent module) exercises the cron-post forwarding flow against the same fixture; asserts the rendered status post's bullets contain content from the decisions section AND do NOT contain any line from the cycle-observations section.
- prose: a test pins `ap2 status` rendering — fixture with both sections; CLI text output contains a "decisions needed" line citing the decisions bullets, and contains NO line referencing cycle-observations content.
- prose: a defensive test pins the parser's heading-match strictness — synthesize a malformed fixture where Cycle observations comes BEFORE Decisions needed; assert `parse_operator_decisions` still returns only decisions bullets, no leakage.

## Out of scope

- Restructuring the OTHER ideation_state.md sections (Mission alignment, Current focus assessment, Considered & deferred). They keep their current schemas; this task only changes the two operator-facing/observation-related sections.
- Changing the ideation prompt's read-order or Step 0 schema-write flow more broadly. The agent still writes the file via the `ideation_state_write` MCP tool; the schema instructions just have new content for two sections.
- Adding events.jsonl event types for Cycle observations (e.g., emitting an event when bullets are carried vs dropped). Pure prompt + parser change; no new event surface.
- Backfilling existing ideation_state.md files. The next ideation cycle rewrites with the new schema; no migration step.
- Surfacing Cycle observations in any operator-facing surface. The whole point is they DON'T leak; if operator visibility into agent observations becomes useful later, it's a separate TB.
- Changing TB-173's surfacing logic structurally. The fix relies on the existing heading-match parser rejecting non-Decisions content; no new surfacing path.
- Renaming `ap2/ideation.default.md` itself or restructuring how `load_prompt(cfg)` resolves it.
- Updating documentation (README, skills/SKILL.md) to reflect the rename. The headings change in the prompt body; downstream docs that reference "open questions" by name can update opportunistically when other doc work happens.

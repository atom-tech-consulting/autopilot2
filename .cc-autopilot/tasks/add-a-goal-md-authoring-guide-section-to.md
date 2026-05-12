# Add a goal.md authoring guide section to `ap2/howto.md`

## Goal

This task is anchored in goal.md's `## Done when` criterion: "An operator can point ap2 at a fresh project, paste a `goal.md` (with Mission + `## Done when`), and walk away for a week without intervention." The walk-away promise depends on operators being able to author a goal.md that ideation can read effectively — but today there's no dedicated documentation explaining what each section is for, how ideation consumes it, or what makes a section work vs. fall flat.

Today's documentation surface for goal.md is fragmented:
- `ap2/howto.md:35` — one-liner in the file-layout list ("operator-curated mission (read by ideation)")
- `ap2/howto.md:282` — `ap2 check` table mentions "missing goal.md" as a warning
- `ap2/README.md:9` — ideation reads goal.md
- `ap2/init.py:63-91` — internal comment explaining which headings the TB-161 anchor validator matches against

None of these is a guide. An operator onboarding a new project has no central place to learn what makes Mission / Done-when / Current focus / Non-goals / Constraints individually work, how ideation reads each, or how the TB-161/TB-164 validators key off them.

This task adds a dedicated `## Authoring goal.md` section to `ap2/howto.md` — one page, five sub-headings (one per goal.md section), each explaining (a) what content belongs there, (b) how ideation consumes it, (c) how it interacts with the validators (TB-161 anchor, TB-164 Why-now), and (d) a fully-worked example. This codebase's own goal.md serves as the worked example so operators have a concrete reference.

Why now: the goal-template fix (sibling TB) ships the canonical five-section structure to fresh projects, but a template alone doesn't teach the operator what makes each section useful — the placeholders signal shape, not substance. Together the template fix and this doc close the onboarding-friction gap that the goal-draft.md → goal.md promotion exposed in this project. Filing now means new ap2-managed projects start with both the right scaffolding AND the guidance to fill it in.

## Scope

- `ap2/howto.md` — new `## Authoring goal.md` section. Suggested placement: after the file-layout overview (current line ~35) and before the operator-workflow sections, OR near the existing `ap2 check` mention of missing-goal.md at line ~282. Five subsections (`### Mission`, `### Done when`, `### Current focus`, `### Non-goals`, `### Constraints`), each ≤200 words, with content described below.
- No code changes. Documentation-only TB.
- Optional: a one-paragraph "Why each section matters to ideation" lead-in before the five subsections, explaining the ideation read-order from `ap2/ideation.default.md` and which sections key into the TB-161 anchor validator.

## Design

### Per-section content

**`### Mission`** — one-sentence "what is this project FOR." Frames every proposal; ideation reads it but doesn't quote-match against it. Bad: "improve developer experience" (unmeasurable). Good: "a Slack bot that ingests trade alerts and posts daily P&L summaries" (concrete subject + scope).

**`### Done when`** — bulleted list of measurable completion criteria. Load-bearing for ideation Step 0: criteria all-met → `exhausted-needs-operator` status → ideation stops proposing. Bad: "the project is solid" (unmeasurable). Good: "walks 1000 strategies through backtest at <10s/strategy on the prod box" (measurable, testable). Each bullet should pass the delete-test: removing it should genuinely change the project's done-signal.

**`### Current focus`** — narrative paragraphs naming the active theme(s). Ideation's Step 0 schema asks for a per-focus-item assessment (Progress / Gaps / Status / Reasoning). The narrative is what the TB-161 anchor validator matches against — briefings' `## Goal` body must cite verbatim text from the focus heading or paragraph. So the focus text doubles as both human guidance AND machine-checkable anchor surface; write it so meaningful proposals can cite it naturally.

**`### Non-goals`** — bulleted list of explicit non-goals. Ideation's Step 0 includes a "non-goal risk check" — proposals straying into non-goal areas get flagged. Frame each as "we are NOT trying to X because Y" to make ideation's drift-detection unambiguous.

**`### Constraints`** — bulleted list of hard constraints (tech stack, deadlines, dependencies, blast-radius limits). Ideation respects these when ranking proposals (e.g., "no API-key features" if OAuth-only is a constraint). Not anchor surface for TB-161 today (the validator only matches Current focus / Done when), so constraint-specific TBs need to thread their goal cite through one of those sections.

### Worked example

Reference this repo's own goal.md as the canonical example. Quote 2-3 lines from each section with a one-line annotation explaining the choice. Avoids duplicating prose; lets readers see real goal.md content rather than a synthetic one. Include an explicit "if you don't have goal-draft.md staged, use this repo's goal.md as a starting model" pointer.

### Validator-interaction summary

A short callout box (or bulleted list) summarizing the two validators that key off goal.md:

- **TB-161 anchor validator** — briefings' `## Goal` body must cite (substring match) text from `## Current focus` or `## Done when` headings/bullets. Reword these sections so meaningful citations are possible.
- **TB-164 Why-now validator** — independent of goal.md content; checks the briefing itself has a `Why now:` line. Goal.md doesn't need a Why-now section.

## Verification

- `grep -nE "^## Authoring goal\.md|^## Authoring \`goal\.md\`" ap2/howto.md` — the new section heading exists.
- `grep -qE "^### Done when|^### Current focus|^### Non-goals|^### Constraints|^### Mission" ap2/howto.md` — at least one subsection heading per section is present (≥5 matches).
- `grep -qE "TB-161|TB-164" ap2/howto.md` — the new section references the two validators by TB-N so readers can grep back to the implementing code.
- prose: a test (or a manual `ap2 check`-style sanity check in CI) confirms the section's worked-example block quotes verbatim text from THIS repo's `goal.md` — so when this repo's goal.md changes, the docs don't drift silently. Implementation hint: a `test_docs.py` test reads the worked-example block out of `ap2/howto.md` and asserts every quoted line still appears verbatim in `goal.md`. Future-proofs against silent drift.
- prose: a reader unfamiliar with ap2 can, after reading just the new section, author a goal.md whose `## Current focus` and `## Done when` sections satisfy the TB-161 anchor validator when cited by a briefing's `## Goal` body. The verification bullet's check: the worked example MUST itself satisfy this — i.e., a synthetic briefing citing the worked example's Current-focus text passes `_validate_briefing_structure` in a test.

## Out of scope

- A separate stand-alone `goal-authoring.md` file. The howto.md section is the right home today; spawn a separate file only when the section grows past ~500 lines.
- Migrating `ap2/README.md` or `ap2/architecture.md` to mirror the new content. The howto is the operator-facing doc; the README and architecture docs serve different audiences.
- Adding a `ap2 doctor goal.md` lint checking each section is non-placeholder. Separate concern; today's `ap2 check` already warns on missing goal.md, which is enough mechanical guard.
- Internationalization or translation. English-only for v1.
- A video walkthrough or screencast. Text-only for v1.
- Promoting the worked example into a `templates/goal-example.md` file shipped alongside `ap2 init`. The template (`GOAL_TEMPLATE` in `ap2/init.py`) is the shipped scaffold; the doc references this repo's filled-in goal.md as the model for substance. Keep template and example separate to avoid drift.
## Attempts

### 2026-05-12 — verification_failed
(no summary)
- **kind:** per_task
- **failed_criteria:** [fail] `grep -nE "^## Authoring goal\.md|^## Authoring \`goal.md`" ap2/howto.md` — the new section heading exists.
- **Debug dumps:** `prompt: .cc-autopilot/debug/20260512T002401Z-TB-200.prompt.md`, `stream: .cc-autopilot/debug/20260512T002401Z-TB-200.stream.jsonl`, `messages: .cc-autopilot/debug/20260512T002401Z-TB-200.messages.jsonl`

# Teach `Prose:` prefix convention in briefing-authoring prompts (`ap2/ideation.default.md` + `skills/ap2-task/SKILL.md`)

Tags: #autopilot #docs #code-quality #verifier #prompts #briefing

## Goal

Close the docs-axis follow-up to TB-219 — Current focus: code quality (cleanness axis, goal.md L80-87): TB-219 added the `Prose:` prefix as a hard-override signal in `verify.py`'s prose-vs-shell classifier and documented the convention in `ap2/howto.md`, but the two prompts that actually drive briefing authorship (`ap2/ideation.default.md` L399-422 and `skills/ap2-task/SKILL.md` L68-80) still teach only "prose bullets are allowed" without naming the prefix. The classifier-trap shape TB-219 caught at n=5 (TB-204/TB-206/TB-207/TB-209/TB-217) recurs because authors don't write the override.

Why now: TB-219 landed at 4814b97 today (2026-05-14T07:38Z). The hard-override path is in place but opt-in at author time; until the two author-facing prompts teach the prefix, future ideation-authored and task-authored briefings keep landing on the heuristic-fallback path and the n=6 incident is one operator-batch away. Closing this gap now compounds TB-219's value — without the prompt-side teaching, TB-219 mostly serves as a safety-net for past briefings, not a forward-looking convention.

## Scope

Update three lines of teaching, no behavior changes:

1. **`ap2/ideation.default.md`**: in the "## Briefing requirements" block (around L396-423), the "Three valid shapes" enumeration's shape 3 ("Prose claim a judge can confirm against the diff or working tree") gets an explicit "lead with `Prose:` token" instruction, mirroring `ap2/howto.md` L360-425. Add a short sentence naming the convention as the canonical signal so ideation-authored briefings adopt it.

2. **`skills/ap2-task/SKILL.md`**: the "## Verification" guidance block (around L68-83) gets a parallel one-paragraph note on the `Prose:` prefix convention — pointers to `ap2/howto.md` for full coverage, but enough on-page so task-agent authors don't have to navigate out.

3. No changes to `verify.py`, `howto.md`, or test files — TB-219 already landed those.

## Design

- Source the canonical phrasing from `ap2/howto.md` L360-425 (TB-219's reference text). Keep both updates short — these are prompt files; verbosity costs tokens on every control / task run.
- Both updates name the prefix as the canonical signal: leading-codespan still works (and is the default classification), but `Prose:` is the unambiguous override an author should reach for first when the bullet starts with a codespan that the classifier might mistake for a shell command.
- No changes to `_validate_briefing_structure` — the prefix is optional; this is a teaching-only change.
- Reusability note: the same paragraph lands in two prompt files, but they serve different audiences (control vs task agent) and have different surrounding context, so a single canonical block isn't natural here. Inline the brief teaching at each site, both referencing `ap2/howto.md` as the long-form source.

## Verification

- `grep -nE "Prose:" ap2/ideation.default.md` — exits 0 with at least one match inside the "## Briefing requirements" / "Three valid shapes" block; previously zero matches.
- `grep -nE "Prose:" skills/ap2-task/SKILL.md` — exits 0 with at least one match; previously zero matches.
- `grep -cE "Prose:" ap2/ideation.default.md skills/ap2-task/SKILL.md` — combined count is at least 2 (one teaching mention per file minimum).
- `ap2/ideation.default.md` Prose: the new teaching paragraph names the `Prose:` token explicitly, points authors to `ap2/howto.md` for full coverage, and lives inside the existing "## Briefing requirements" section (not a new top-level section). Judge confirms via Read.
- `skills/ap2-task/SKILL.md` Prose: the new teaching paragraph mirrors the ideation file's intent, lives inside the existing `## Verification` guidance block, and references `ap2/howto.md` for the full convention. Judge confirms via Read.
- `uv run pytest -q ap2/tests/` — full suite green (exit 0); no regression in docs-drift or coverage-drift tests, since this change touches only prompt files.

## Out of scope

- Backfilling existing briefings with the `Prose:` prefix — TB-219's hard-override is forward-looking by design; old briefings already landed.
- Re-running verifier on past briefings — TB-219 already covers the heuristic-fallback path for legacy authors.
- Mechanical drift gate that asserts the prefix is mentioned in author-facing prompts — single-paragraph teaching changes don't warrant a regression-pin test; this is documentation, not behavior.
- Any verifier changes — TB-219 is complete; this task only updates teaching prompts.

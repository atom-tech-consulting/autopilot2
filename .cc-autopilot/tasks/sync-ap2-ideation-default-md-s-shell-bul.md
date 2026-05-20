## Goal

Sync `ap2/ideation.default.md`'s `## Shell-bullet pitfalls to AVOID
(TB-76 — observed in prod)` section (currently three pitfalls: bare
`python`, bare-path-as-command, multi-line bullets) with the
authoritative four-pitfall list in `ap2/howto.md` L462-505
(literal-backtick, absence-check `!` prefix, directory-walking `-r`,
`Prose:` prefix). The howto already carries the canonical text and
a worked example combining all four; the ideation prompt — which
is what the ideation agent reads when AUTHORING briefings each
cycle — has not been updated in lockstep, so ideation-authored
briefings keep tripping over pitfalls the howto already warns about.
This proposal addresses Current focus: end-to-end automation
(goal.md L38-151), specifically the axis-1 manual-approval
bottleneck: every preventable retry-storm on an ideation-authored
briefing is operator-toil that the walk-away promise (`## Done
when` bullet "an operator can point ap2 at a fresh project ... and
walk away for a week without intervention") forfeits.

Why now: TB-270 retry-storm on 2026-05-20T04:54→05:59Z was caused
by an ideation-authored bullet missing the `!` exit-inversion
prefix on an absence-check (`grep "<absent string>" ...` exits 1
when absent, which the verifier reads as FAIL); 3 retries
exhausted, operator-manual unfreeze required. The howto already
documents the fix (pitfall #2 at L479-484); the ideation prompt
that authored the briefing did not. Closing the prompt-vs-howto
divergence is a one-file edit that compounds against every future
ideation-authored briefing — exactly the kind of preventive,
upstream-of-validator move the axis-1 trust upgrade depends on.

## Scope

1. Edit `ap2/ideation.default.md`'s `## Shell-bullet pitfalls to
   AVOID (TB-76 — observed in prod)` section (currently
   L471-486). Replace the three existing pitfall bullets with the
   four authoritative pitfalls verbatim-aligned to `ap2/howto.md`
   L462-505:
   - **No literal backticks in the command body** (TB-207 history).
   - **Absence-check shell bullets must use the `!` exit-inversion
     prefix** (TB-270 history; the bullet whose miss caused the
     2026-05-20 retry storm).
   - **Directory-walking grep must use `-r`** (TB-204 history).
   - **`Prose:` prefix for judge bullets** when a bullet's
     grammatical subject is a backtick-fenced filename/symbol
     (TB-219 classifier complement).
   Preserve the existing intro sentence about `/bin/sh -c` exec
   semantics and the trailing "Prefer running concrete project
   commands" line; the four pitfalls replace ONLY the bullet body
   in between.

2. Add a one-line worked-example pointer at the end of the
   section: `See \`ap2/howto.md\` L462-505 for a worked example
   combining all four.` Keep the example itself in `ap2/howto.md`
   (single source of truth) — the ideation prompt should reference,
   not duplicate, the worked example to avoid future drift.

3. Add a regression-pin module
   `ap2/tests/test_tb273_ideation_pitfalls_sync.py` with assertions
   that:
   - `ap2/ideation.default.md`'s pitfalls section contains all four
     pitfall-identifying substrings: `"literal backtick"`,
     `"! grep"` (or `"exit-inversion"`), `"grep -r"`, `"Prose:"`.
   - The section contains the cross-reference to
     `ap2/howto.md` L462-505.
   - The legacy three-pitfall-only shape is gone (assert that
     `"## Shell-bullet pitfalls"` heading still exists AND the new
     `"! grep"` + `"Prose:"` strings are now present in the same
     section — their absence would be the regression).

## Design

- **Single source of truth pattern**: `ap2/howto.md` is the
  agent-facing runtime reference (the task agent reads it when
  authoring its own work); `ap2/ideation.default.md` is the
  ideation-agent prompt (read when authoring NEW briefings). Both
  agents need the pitfall vocabulary, but the worked example
  should live in exactly one place to avoid drift. Sync the
  prompt's bullet headings to match the howto verbatim, and
  reference the howto for the worked example rather than
  duplicating it. Future pitfalls land in howto first (observed in
  prod), then sync here in a similar follow-up.
- **No new authoring conventions**: this is a pull, not a push.
  The four pitfalls already exist in howto; the ideation prompt
  is the one out of date. Choosing "sync to howto" (not the other
  way round) honors howto's role as the authoritative reference.
- **Regression-pin shape mirrors TB-269/270**: a dedicated
  `test_tb273_*.py` module with assertions named after each
  scope-3 bullet. The judge can map bullet→test directly and the
  module is greppable for future regression debugging. No new
  runtime code paths are exercised; the assertions are pure
  Read-the-file pattern matches.
- **Out-of-scope guardrails**: explicitly do NOT add new
  pitfalls, modify howto, or wire any briefing-validator gate
  for absence-check shape. Each of those is a separate decision
  (whack-a-mole risk per TB-172/TB-240 rejection patterns) that
  the operator owns; this proposal stays narrow.

## Verification

- `uv run pytest -q ap2/tests/test_tb273_ideation_pitfalls_sync.py` — new regression-pin module passes (all scope-3 assertions covered).
- `uv run pytest -q` — full suite passes (no incidental breakage in callers that parse `ap2/ideation.default.md`).
- `grep -F "literal backtick" ap2/ideation.default.md` — TB-207 pitfall #1 present.
- `grep -F "! grep" ap2/ideation.default.md` — TB-270 absence-`!` pitfall present in the synced section.
- `grep -F "grep -r" ap2/ideation.default.md` — TB-204 directory-walk pitfall present.
- `grep -F "Prose:" ap2/ideation.default.md` — TB-219 prose-prefix pitfall present.
- `grep -F "ap2/howto.md" ap2/ideation.default.md` — the cross-reference to the howto worked example is present in the prompt.
- `ap2/ideation.default.md` Prose: the `## Shell-bullet pitfalls to AVOID (TB-76 — observed in prod)` section now lists exactly four pitfalls (literal-backtick, absence-`!`, directory-`-r`, `Prose:` prefix) aligned verbatim with `ap2/howto.md`'s four-pitfall convention; judge confirms via Read.

## Out of scope

- Adding new pitfalls beyond the four already in `ap2/howto.md`
  (this is a sync, not an expansion — new pitfalls land in howto
  first based on observed prod failures, then sync here).
- Modifying `ap2/howto.md`'s pitfall list (it's already the
  authoritative source).
- Adding any auto_unfreeze fix-shape or briefing-validator gate
  for the `!`-miss class (whack-a-mole risk per TB-172 / TB-240
  rejection patterns; preventive sync is the durable form).
- Touching `skills/ap2-task/SKILL.md` or other agent-facing docs
  (those are task-agent-facing; this proposal targets the
  ideation agent's prompt template specifically).

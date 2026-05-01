# Briefing prompts must require auto-verifiable Verification bullets only

## Goal

Update the briefing-authoring prompts in `ap2/ideation.default.md` and `skills/ap2-task/SKILL.md` (and any other place that instructs an author — human or LLM — how to write `## Verification` sections) so every bullet is auto-verifiable: a backticked shell command, a unit / e2e test name, or a prose claim a judge can confirm against the diff or the working tree. No `Manual:` bullets. No "operator runs X live and observes Y" steps that the per-task verifier cannot evaluate.

## Why

TB-122 hit the failure mode on 2026-05-01: 5/6 verification bullets passed under the new cumulative-diff verifier, but a single `Manual: kick a long-running task on stoch, mention @claude-bot status → handler replies in <30s` bullet kept failing because the verifier (correctly) cannot observe a live operator action. Result: retry_exhausted and the task was re-frozen despite the implementation being complete and all auto-bullets green.

The right fix is at the briefing-authoring layer: don't write manual bullets in the first place. Anything worth verifying belongs in a test the daemon can run unattended, OR a prose claim that names a file/symbol the judge can Grep for. If a behavior genuinely cannot be auto-verified (rare), it shouldn't be in the gating Verification section — it belongs in `## Out of scope` or a manual `## Operator checklist` section the verifier ignores.

## Scope

- `ap2/ideation.default.md`: update the task-emit instructions so ideation never proposes Manual bullets. Add an explicit rule: "Every `## Verification` bullet must be auto-checkable. Convert manual procedures to e2e tests with stubbed dependencies; if you cannot, it is out of scope."
- `skills/ap2-task/SKILL.md`: update the operator-facing skill doc with the same rule plus an example of converting a manual bullet to a stubbed e2e test (mirror TB-122's fix as the canonical example).
- `skills/migrate-to-ap2/SKILL.md`: same rule for migration-time briefings.
- `ap2/init.py:BRIEFING_TEMPLATE` (or wherever the empty briefing template lives): the verification-section preamble text should explicitly say "auto-verifiable bullets only — no Manual: steps."
- Optional: a lightweight lint in `ap2 check` that warns when a Verification section contains a bullet starting with `Manual:` or `[manual]`. Non-fatal — surfaced as a warning so the operator can fix it before dispatch.

## Verification

- `uv run pytest -q ap2/tests/` — full regression gate passes (gating)
- `! grep -qE "^\\s*[-*]\\s*Manual:" ap2/ideation.default.md` — ideation prompt no longer suggests Manual bullets
- `grep -qE "auto.?verifiable" ap2/ideation.default.md` — ideation prompt explicitly mentions the auto-verifiable rule
- `grep -qE "auto.?verifiable" skills/ap2-task/SKILL.md` — operator skill doc carries the same rule
- `grep -qE "auto.?verifiable" ap2/init.py` — empty-briefing template's verification preamble carries the rule
- New unit test in `test_ideation_defaults.py` (or wherever the prompt is pinned): the rendered ideation prompt contains a string asserting Verification bullets must be auto-checkable. Pins the prompt instruction so future edits don't silently regress.
- If the `ap2 check` lint is included: new unit test in `test_check.py` verifying that a briefing whose `## Verification` contains `- Manual: ...` produces a warning-level Issue, while one with only auto-verifiable bullets produces nothing.

## Out of scope

- Changing the per-task verifier to soft-pass `Manual:` bullets — addressed at briefing-author layer instead.
- Migrating existing in-flight briefings (TB-122 was hand-edited as part of this issue's discovery; remaining briefings can be cleaned up opportunistically as they're touched).
- A separate `## Manual checklist` section the verifier ignores — keeps the briefing surface simple; if you can't auto-verify it, it's out of scope.

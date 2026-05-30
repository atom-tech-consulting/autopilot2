# Purge stale hardcoded-3 proposal caps and rotation-era references from the ideation prompt

Tags: #autopilot #ideation #prompt #refactor #docs-drift #cleanup

## Goal

`ap2/ideation.default.md` carries two layers of drift that the
dynamic-slot rework (TB-160/TB-183) and the multi-focus collapse
(TB-342) + ideation-halt merge (TB-345) left behind.

1. **Self-contradictory proposal count.** L29-31 already states the
   correct rule — "Propose at most N new tasks this cycle, where N is
   the `proposal slots this cycle` value" — but four older lines still
   hardcode **3**, and the agent reads both. The concrete number wins,
   which is why raising `AP2_IDEATION_TRIGGER_TASK_COUNT` to a higher
   value still yields ~3 proposals (the slot computation in
   `ap2/ideation.py` is correct; the prompt text is the limiter):
   - L155-158 `## Proposals this cycle`: "List the 3 task TB-Ns…
     (or fewer if Backlog already has ≥3 workable items…)"
   - L242-243 follow-up discovery: "Only fall back to greenfield ideas
     if you're short of 3 candidates afterwards."
   - L329 failure review: "compete with greenfield against the same
     Backlog<3 budget"
   - L343 Ranking: "Propose the top 3 via board_edit"

2. **Rotation-era references** that no longer match the code after
   TB-342/TB-345:
   - L180 cites `ap2/focus_advance.py::_ideation_empty_against_focus`
     — that module path no longer exists (now `ap2/ideation_halt.py`)
     and the symbol was renamed to `_consecutive_empty_ideation_cycles`.
   - L183 cites `AP2_FOCUS_ADVANCE_EMPTY_CYCLES` "auto-advance
     threshold" — the knob is now `AP2_IDEATION_HALT_EMPTY_CYCLES` and
     there is no "advance" anymore, just a single halt.
   - L176-177 examples ("focus-2 marked exhausted-needs-operator",
     "TB-298 + TB-299 against focus-2") bake in per-focus indexing that
     the single-halt model removed.
   - L66/L71-73 status enum + exhaustion language is framed per-focus;
     post-collapse, exhaustion is whole-goal.
   - L148 lists "focus-rotation decisions" as a decisions-needed
     example — rotation is gone.

Make L29-31's dynamic-N rule the single source of truth for proposal
count, and align every module/knob/example reference with the
post-TB-345 reality.

Why now: the hardcoded-3 leftovers are an active correctness bug —
they silently cap autonomous ideation at ~3 proposals regardless of
the operator's configured trigger threshold, so the board drains and
ideation under-fills it; and the broken `focus_advance.py` citation
will mislead any future reader (including the agent itself) right
before the OSS cut. Operator-directed 2026-05-29; meta-infra prompt
cleanup with no active focus, so `--skip-goal-alignment`. Builds on
TB-345's `ideation_halt` rename, so `@blocked:TB-345`.

## Scope

Edits are confined to `ap2/ideation.default.md` plus any test that
pins the old wording.

- **Count references → N.** Rewrite L155-158, L242-243, L329, L343 so
  each refers to the injected `proposal slots this cycle` value (N)
  rather than a literal 3. Keep the intent of each line (the
  "Backlog already populated; no proposals this cycle" exit at
  L156-158 becomes "Backlog already has ≥N workable items"; the
  failure-remediation budget at L329 references the configured trigger
  threshold / slot budget, not "Backlog<3"). Do NOT touch unrelated
  numerics that are legitimately fixed: "3-5 most recent Completes"
  (L56), "2-3 narrower Backlog tasks" (L291), "Scan up to 5" (L246),
  "Hard cap: 10 bullets" (L105) — these are not per-cycle proposal
  caps.
- **Module/knob/symbol citations.** L180: `ap2/focus_advance.py::_ideation_empty_against_focus`
  → `ap2/ideation_halt.py::_consecutive_empty_ideation_cycles`.
  L183: `AP2_FOCUS_ADVANCE_EMPTY_CYCLES` → `AP2_IDEATION_HALT_EMPTY_CYCLES`,
  and drop "auto-advance" framing in favor of the single-halt
  description (the threshold counts consecutive dry cycles and emits
  the halt directly).
- **Rotation examples / framing.** L176-177: reword the example
  summaries to be focus-neutral (e.g. "TB-298 + TB-299 against the
  current focus" → "TB-298 + TB-299"; "focus-2 marked
  exhausted-needs-operator" → "goal marked exhausted; all Progress
  signals addressed"). L66/L71-73: keep the per-heading
  progress/gaps/status assessment (multi-focus headings remain
  operator prose/priority hints per TB-342), but reframe the
  `exhausted-needs-operator` status and the exhaustion sentence as a
  whole-goal judgment, not a per-focus rotation step. L148: replace
  the "focus-rotation decisions" example with a still-valid
  decisions-needed example (e.g. "residual-risk acceptances awaiting
  sign-off, escalations" already present — drop the rotation one).
- **Tests.** If any test in `ap2/tests/` pins the removed wording
  (e.g. asserts the literal "top 3" / "Backlog<3" /
  `focus_advance.py` / `AP2_FOCUS_ADVANCE_EMPTY_CYCLES` appears in the
  prompt), update its expectation to the new wording. Do NOT weaken a
  test to pass — re-point it at the corrected string.

## Design

- **One source of truth.** L29-31 is the canonical rule; every other
  count mention becomes a pointer to "N / the `proposal slots this
  cycle` value." This removes the contradiction without changing the
  slot math (`_compute_slots` in `ap2/ideation.py` already yields
  `max(0, threshold - queued)` and is correct — do not touch it).
- **Citations follow code, not the reverse.** The prompt is
  documentation of live behavior; after TB-345 the live module is
  `ap2/ideation_halt.py` and the live knob is
  `AP2_IDEATION_HALT_EMPTY_CYCLES`. The prompt must match.
- **Multi-focus headings stay expressive.** Per TB-342, the operator
  may still list several `## Current focus:` headings as prose
  priority hints; the agent reads them all and the goal-anchor
  validator accepts any. The per-heading assessment loop stays — only
  the rotation/sequencing framing is removed.
- **Insulation.** The prompt is read fresh each ideation cycle via
  `load_prompt`; the change takes effect on the next cycle with no
  restart. The pytest gate catches a broken docs-drift pin before
  commit.

## Verification

- `uv run --extra dev pytest -q ap2/tests/` — full suite passes
  (canonical `AP2_VERIFY_CMD`, scoped to `ap2/tests/`).
- `! grep -nE "top 3|Backlog<3|the 3 task TB-Ns|short of 3 candidates" ap2/ideation.default.md` — the hardcoded-3 proposal caps are gone.
- `! grep -nE "focus_advance\.py|AP2_FOCUS_ADVANCE_EMPTY_CYCLES|_ideation_empty_against_focus" ap2/ideation.default.md` — the stale module path, old knob name, and renamed symbol are gone.
- `grep -nE "ap2/ideation_halt\.py|AP2_IDEATION_HALT_EMPTY_CYCLES|_consecutive_empty_ideation_cycles" ap2/ideation.default.md` — the corrected module, knob, and symbol names are present.
- `grep -nE "proposal slots this cycle" ap2/ideation.default.md` — the dynamic-N anchor phrase is still present and is the count reference the other sections point at.
- `ap2/ideation.default.md` Prose: every per-cycle proposal-count reference (the `## Proposals this cycle` schema line, the follow-up-discovery fallback, the failure-remediation budget line, and the Ranking "propose the top …" line) refers to the injected `proposal slots this cycle` value (N) rather than a hardcoded 3; the rotation example wording ("focus-2", "against focus-2") and the per-focus `exhausted-needs-operator` framing are reworded to the single whole-goal halt model. Judge confirms via Read.

## Out of scope

- The five stale `focus_advance` source-comment references in
  `ap2/events.py`, `ap2/goal.py`, `ap2/ideation.py`, `ap2/tools.py` —
  separate cleanup task (filed alongside this one).
- The slot-computation code in `ap2/ideation.py` (`_compute_slots`,
  `_trigger_task_count`) — verified correct; not touched here.
- Editing `goal.md` (operator-owned, fenced).
- Any change to the shell-bullet-pitfalls section (L496-531) — it
  stays verbatim-aligned with `ap2/howto.md` and is not part of this
  drift.

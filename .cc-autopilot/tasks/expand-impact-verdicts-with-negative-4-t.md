# Expand `IMPACT_VERDICTS` with `negative` (4 total) — gradient bucket for "actively regressed" outcomes distinct from "no impact"

Tags: `#autopilot` `#operator-surface` `#cli` `#classify` `#regression-pin`

## Goal

Advance goal.md's **Current focus: end-to-end automation** focus's (1) **Manual-approval bottleneck** axis by adding one bucket to the operator's retrospective classification vocabulary. Today `IMPACT_VERDICTS = ("advanced-goal", "pro-forma", "unclear")` at `ap2/tools.py:586` operates on a single dimension — the delete-test ("if we delete this and the goal still ships, was it useful?"). Three answers: yes (`advanced-goal`), no (`pro-forma`), can't-tell (`unclear`). What's missing is a bucket for the **stronger negative**: not just "didn't add value" but "the codebase is now WORSE because this shipped" — a regression introduced, a test inadvertently weakened, a refactor that landed but increased complexity beyond what the briefing intended. Under `AP2_AUTO_APPROVE=1` (per TB-223) the operator's classify signal is THE primary judgment surface for ideation prompt-tuning; collapsing "neutral-but-low-value" and "actively-harmful" into one bucket loses the signal that lets ideation strongly avoid harmful shapes. The comment at tools.py:584 names this as the intentional extension path: *"Adding values is a one-line tuple edit; expanding via the operator's CLI is the briefing's intentional follow-up."* This TB executes that follow-up for the actively-harmful gap only — confidence-grounded scope; broader expansion deferred until operator engagement shows real need.

Why now: TB-248 (`ap2 audit` verb, in review at filing time) ships an interactive walkthrough whose `[c]lassify` sub-prompt lists `IMPACT_VERDICTS` to the operator. Expanding the enum BEFORE TB-248 lands means the walkthrough offers the 4-verdict set from day one — operators don't form muscle memory around 3 verdicts that then expand. Operator (2026-05-16) tested-and-rejected adding `wasteful` as a fourth bucket: under the existing delete-test framing, `wasteful` is semantically equivalent to `pro-forma` (both = "no, not useful") and would add a fabricated distinction (operator regret) that the existing semantic dimension doesn't track. Adding only `negative` closes the gap with a real semantic distinction (codebase-WORSE, not just codebase-neutral) without fabricating a new dimension.

## Scope

(1) **Extend `IMPACT_VERDICTS`** at `ap2/tools.py:586` to 4 entries:

```python
IMPACT_VERDICTS: tuple[str, ...] = (
    "advanced-goal",     # substantively advanced the goal (positive)
    "pro-forma",         # goal-shaped but didn't advance — compliance signal (no harm; just no impact)
    "negative",          # actively regressed something OR made the codebase worse — failed the stronger delete-test (would deletion make things BETTER, not just neutral?)
    "unclear",           # impact not yet legible (uncertain — defer)
)
```

The 4 buckets form a gradient: substantive-positive → compliance-neutral → actively-harmful, with uncertain as the explicit "can't tell yet" bucket. Order in the tuple matches operator's mental sort (best → worst, uncertain last).

(2) **Update `cmd_classify` help text** in `ap2/cli.py` (line ~1986) to list the expanded set + brief one-line semantic gloss per verdict. Operator running `ap2 classify --help` sees what each bucket means and when to pick each.

(3) **Update `classifications_last_30d_by_verdict` renderer** at `ap2/cli.py` (lines 174, 263, 383 per the existing 3-bucket render path) to handle all 4 verdicts in the compact display. Missing-bucket fallback ("0" for any verdict with no observations) preserves the existing-bucket display for projects that haven't classified any `negative` shapes yet.

(4) **Update `ap2/howto.md`'s classify section** to document all 4 verdicts + the semantic distinction between `pro-forma` and `negative` (the load-bearing new distinction): use `pro-forma` when the task didn't advance the goal but didn't regress anything; use `negative` when the task actively made something worse — a regression slipped through, test coverage was inadvertently weakened, a refactor increased complexity beyond the briefing's intent, or similar codebase-WORSE outcomes. The howto should make clear: "pro-forma" is no-impact + no-harm; "negative" is no-impact + harm.

(5) **Tests** (extend existing `ap2/tests/test_classify.py` or `test_cli.py`):
  - `test_classify_accepts_each_impact_verdict`: parameterized over all 4; each invocation succeeds (queue op generated, no validation error).
  - `test_classify_rejects_invalid_verdict`: pass `--impact bogus`; assert CLI exits non-zero with the expected error message naming the 4 valid choices.
  - `test_classifications_last_30d_renders_all_4_verdicts`: seed events for each verdict; assert render output lists all 4 with correct counts.
  - `test_impact_verdicts_tuple_length`: regression-pin `len(IMPACT_VERDICTS) == 4` (catches accidental removal in a future refactor).

(6) **Don't add a backfill mechanism** for re-classifying historical `pro-forma` records as `negative` — historical operator decisions stand. Future classifications use the richer vocabulary.

(7) **Don't change `automation_status.py`'s renderers** unless they enumerate verdicts (they don't today — the `automation_status` collector tracks event-type counts, not verdict counts). If TB-248's audit verb adds a verdict-count summary, it'll iterate `IMPACT_VERDICTS` automatically.

## Design

**Why 4 (not 3 or 5)**: the existing 3 collapse two semantically-distinct negative outcomes ("no impact, no harm" vs "no impact, plus harm") into `pro-forma`. The collapse loses the signal ideation needs to strongly avoid harmful shapes vs merely de-prioritize compliance shapes. Adding `negative` restores the distinction with a SECOND delete-test that's already operationalizable: "if we deleted this work, would the codebase be BETTER, not just neutral?" — a stronger test than the base "would the goal still ship?" Yes → `negative`; no → `pro-forma`.

Adding a 5th bucket (e.g. `wasteful` for "operator regrets approving but no codebase harm") was considered and rejected — under the existing single-dimension framing it's equivalent to `pro-forma`. Distinguishing them would require introducing a NEW dimension (operator foreseeability at proposal time) that the current enum doesn't track. The fabrication-trap concern (per TB-240's reject reasoning): adding semantics that look useful but aren't grounded in observed need. If operator engagement post-TB-248 shows you frequently want to express "low-value AND foreseeable-at-proposal-time" distinct from `pro-forma`, file a follow-up TB with that empirical evidence.

**Why `negative` as the verdict name** (not `harmful` / `regressed` / `worse`): `negative` is the cleanest single-word adjective in the namespace. `harmful` overlaps with "security-harmful" connotations; `regressed` is ambiguous with regression tests; `worse` is comparative but lacks a referent. `negative` matches the existing `pro-forma`'s grammatical shape (adjectival) closely enough.

**Goal-anchor**: the Done-when bullet "ideation reliably proposes goal-aligned next steps that substantively advance the goal (not just goal-shaped pro-forma compliance), without drifting into ap2-meta polish or scope creep" depends on ideation having clear signal about what counts as goal-aligned vs pro-forma vs actively harmful. Today's 3-bucket vocabulary collapses the last two into one.

## Verification

- `uv run pytest -q ap2/tests/` — full suite green (exit 0).
- `grep -nE '"advanced-goal"' ap2/tools.py` — exit 0; the existing constant value is present (sanity check that we didn't accidentally rename existing values).
- `grep -nE '"negative"' ap2/tools.py` — exit 0; the new value is present in the constant.
- `[ "$(grep -cE '\"(advanced-goal|pro-forma|unclear|negative)\"' ap2/tools.py)" -ge 4 ]` — at least 4 string-literal occurrences of the verdict names in tools.py (one per tuple entry, plus possibly callers).
- `grep -nE "negative" ap2/howto.md` — exit 0; the new verdict is documented in the operator howto.
- `grep -nE "negative" ap2/cli.py` — exit 0; the new verdict appears in the CLI (help text + render path).
- Prose: each of the 4 verdicts has a one-line semantic description in `cmd_classify`'s help text or `--impact` argument's `choices=` documentation, making it unambiguous when an operator types `ap2 classify --help` which bucket to pick for a given task outcome. Judge confirms via `Read` of the argparse setup.
- Prose: the `pro-forma` vs `negative` distinction is named explicitly in `ap2/howto.md`'s classify section (not just listed as 4 verbatim values) — operator can read the howto and decide which verdict to pick for a borderline case. The distinction should frame as: `pro-forma` = no-impact + no-harm; `negative` = no-impact + actively-harmful. Judge confirms via `Read` of the howto section.

## Out of scope

- Adding `wasteful` (or any other 5th verdict) — see Design "Why 4 not 5"; defer until operator engagement shows real need.
- Backfilling historical `pro-forma` records as `negative` — operator's prior decisions stand.
- Renaming the existing 3 verdicts for grammatical consistency (e.g. `advanced-goal` → `substantive`, `pro-forma` → `compliance-shaped`) — orthogonal cleanup; would break backward compat with all prior classifications.
- Auto-classifying via LLM — operator judgment is the signal source; same principle as TB-248's "no LLM auto-classification" non-goal.
- Per-verdict ideation prompt-header customization (e.g. "show last 5 `negative` tasks to ideation as 'avoid these shapes'") — separate follow-up; this TB just adds vocabulary.
- Mattermost surface for classification activity — separate observability surface.
- A `--impact-prompt` interactive flag on `ap2 classify` that helps operators choose — TB-248's audit walkthrough already provides the interactive surface; bundling here would be duplicate.

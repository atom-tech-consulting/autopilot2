## Goal

Close the second axis-1 lever named in TB-257's investigation
artifact, complementary to TB-269's timeout bump on the same
**Current focus: end-to-end automation** roadmap. TB-257
(`.cc-autopilot/insights/validator-judge-timeout-2026-05-18.md`)
categorized `prompt-too-heavy` as the secondary factor: even the
smallest measured briefing (4621 B) took ~22s avg, and typical
operator queue-append briefings are ≥6 KB. The full briefing
markdown — Design, Verification, Out-of-scope — is currently
shoved into the user payload at `ap2/validator_judge.py:378`
(`"briefing_markdown": briefing_text`), but hard-predecessor
detection only needs the briefing's intent surface
(Goal + Scope). Slicing the payload to those two sections
shrinks the input token count and therefore the wall-clock,
independently of any timeout calibration — and structurally
relieves the same pressure TB-269's deadline bump masks.

Why now: TB-269 bumps the deadline so the gate stops timing out,
but the underlying call still spends ~30s+ per queue-append on
content the judge doesn't use. Slimming the payload is the
structural fix that lowers operator-perceived latency on every
`ap2 add` / `ap2 update_goal` and reduces token spend on a
high-frequency call site. Both axes 1 and 3 (cost guards) of the
end-to-end-automation focus benefit. The TB-257 artifact's
secondary-factor designation makes this the principled next move
once calibration lands.

## Scope

1. Extract a new helper `_slice_briefing_for_dep_judge(briefing_
   text: str) -> str` in `ap2/validator_judge.py` that returns
   the substring covering `## Goal` and `## Scope` sections only
   (terminating at the next `## ` heading or EOF). Empty / missing-
   section fallback: return the full `briefing_text` unchanged
   (defensive — don't make the judge blind on briefings that
   skip the canonical heading shape). Both sections returned
   together preserve order from the source so `## Goal` always
   precedes `## Scope` in the slice.

2. Use the helper to populate `user_payload["briefing_markdown"]`
   in `_judge_dep_coherence_default` (`ap2/validator_judge.py:
   378`). NO other call-site changes — the system prompt and
   `task_description` / `blocked_codespan_tokens` fields stay as
   today.

3. Re-measurement protocol documented in the TB-257 artifact:
   append a `## Re-measurement after TB-270` section (do NOT
   mutate the existing measurement sections) noting the date the
   slice landed and the expected wall-clock reduction direction
   (typical input size shrinks from ~6KB → ~1-2KB; SDK latency
   should drop accordingly). The artifact's `updated`/
   `updated_by` YAML fields update to TB-270.

4. Update `ap2/howto.md`: cross-reference TB-270 in the existing
   validator-judge section so future ideation cycles can find the
   slicing rationale + helper without grepping the artifact.

5. Regression-pin module `ap2/tests/test_tb270_validator_judge_
   payload_slice.py` covering: (a) helper returns Goal+Scope-only
   substring on a canonical-shaped briefing; (b) helper returns
   the full briefing on a briefing missing one or both headings
   (defensive fallback); (c) the slicing preserves Goal-then-Scope
   ordering when source has them in that order; (d) integration
   pin asserting `_judge_dep_coherence_default`'s user_payload
   `briefing_markdown` field equals `_slice_briefing_for_dep_
   judge(briefing_text)` by inspecting the SDK-call prompt
   (mock the sdk.query call, capture the prompt arg, parse the
   JSON block, assert).

## Design

The judge's job is hard-predecessor detection: given a briefing
and a set of `@blocked` tokens, does the briefing's intent
genuinely depend on another TB-N's work being on disk? That
question is answered from the briefing's narrative-intent
sections (Goal: why; Scope: what), not from Verification (which
checks shape, not dependencies) or Out-of-scope (which negatives
don't shift the dep graph) or Design (which is internal to the
TB, not a cross-task dep claim). The slice is a faithful
narrowing of input, not a lossy compression — what's removed is
material the judge wouldn't have used to change its verdict.

Goal-and-Scope-only also coincides with what
`_validate_briefing_structure` already requires to be non-empty
(TB-161 anchor check; TB-164 Why-now check is inside Goal), so
the slice's expected size is bounded by the same gate
operator-curated briefings already pass. Briefings that pass
queue-append validation are guaranteed to have a non-empty slice
output; only legacy or hand-edited skip-the-validator briefings
hit the defensive fallback branch.

Combined with TB-269's deadline bump, the expected post-
deployment shape is: nominal calls finish ~15-25s (below the new
60s default), giving the dep-coherence judge generous headroom
even on edge-case heavy briefings. Operator-visible: `ap2 status`
validator-judge timeout count should drop to ≤1/week from the
current ~1/operator-queue-append.

## Verification

- `uv run pytest -q ap2/tests/test_tb270_validator_judge_payload_slice.py` — new regression-pin module passes (all 4 scope bullets covered).
- `uv run pytest -q ap2/tests/` — full suite passes (no regressions in existing validator-judge / dep-coherence tests).
- `grep -nE "def _slice_briefing_for_dep_judge" ap2/validator_judge.py` — exits 0 (helper present).
- `grep -nE "_slice_briefing_for_dep_judge\(" ap2/validator_judge.py` — prints ≥2 lines (definition + call site in `_judge_dep_coherence_default`).
- `grep -nE "briefing_markdown\"[[:space:]]*:[[:space:]]*briefing_text" ap2/validator_judge.py` — exits NON-ZERO (the raw full-briefing assignment has been replaced; if the grep finds the literal pattern, the slice wasn't wired).
- `grep -nE "Re-measurement after TB-270" .cc-autopilot/insights/validator-judge-timeout-2026-05-18.md` — exits 0 (artifact append-only update present).
- `grep -nE "TB-270" ap2/howto.md` — exits 0 (howto cross-reference present).
- Prose: the new `_slice_briefing_for_dep_judge` in `ap2/validator_judge.py` returns Goal+Scope substring on canonical briefings and falls back to the full `briefing_text` when either heading is missing — judge confirms by Read of the helper body and the canonical-vs-fallback branch shape.

## Out of scope

- Token-budget caps on `briefing_markdown` (truncating mid-section
  at N chars) — the slice already produces a bounded result; an
  explicit cap layered on top is premature without post-deploy
  measurement evidence.
- Removing `task_description` / `blocked_codespan_tokens` from the
  payload — these are tiny (single line + small list) and the
  judge needs both to ground its verdict. Slice scope is briefing
  prose only.
- Generalizing the slice helper for other judges (e.g. prose-
  verification judge) — the prose judge has different inputs
  (diff + bullet, not briefing markdown); refactor waits for a
  second caller.
- Caching parsed slices across queue-append batches — operator
  queue is low-volume; caching adds complexity without measured
  payoff.
## Attempts

### 2026-05-20 — verification_failed
(no summary)
- **kind:** per_task
- **failed_criteria:** [fail] `grep -nE "briefing_markdown\"[[:space:]]*:[[:space:]]*briefing_text" ap2/validator_judge.py` — exits NON-ZERO (the raw 
- **Debug dumps:** `prompt: .cc-autopilot/debug/20260520T044106Z-TB-270.prompt.md`, `stream: .cc-autopilot/debug/20260520T044106Z-TB-270.stream.jsonl`, `messages: .cc-autopilot/debug/20260520T044106Z-TB-270.messages.jsonl`

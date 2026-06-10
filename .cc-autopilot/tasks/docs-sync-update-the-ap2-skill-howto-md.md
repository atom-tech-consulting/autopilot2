# Docs sync: update the ap2 skill + howto.md to the TB-385 event vocabulary (task_solve / task_verify)

Tags: #autopilot #docs #skill #howto #events #sync

## Goal

TB-385 renamed the per-task lifecycle events — `task_start` → `task_solve`, and
folded the mid-stream `verify_passed` + per-bullet prose `judge_call` into one
terminal `task_verify` — and updated howto.md's `## Event schema` section, the
web pages, and the consumers. But two doc surfaces were missed and now describe
the old vocabulary:

- **`skills/ap2/SKILL.md`** — the interesting-event-types list (L53) and the
  worked example (L111) still use `task_start` and omit `task_verify`, so an
  agent operating ap2 through the skill greps for an event the daemon no longer
  emits for new runs.
- **`ap2/howto.md`** — the `### Prose-judge diagnostics` section (≈L397–450)
  still describes the prose judge as emitting a per-call `judge_call` event,
  and the stuck-detector reference (≈L1266) keys on `task_start`; both predate
  the `task_verify` fold.

Bring both in sync with the current vocabulary. Doc-surface split (do not
violate): `skills/ap2/SKILL.md` = operator/agent quick-reference; `ap2/howto.md`
= operation manual (CLI / knobs / events). No component-model design prose in
either (that lives in `architecture.md`). Meta-infra docs, no focus anchor.

## Scope

- **skills/ap2/SKILL.md** — in the interesting-event-types list (L53) replace
  `task_start` with `task_solve` and add `task_verify`; update the worked
  example (L111) to show `task_solve` instead of `task_start`. Add a brief
  parenthetical that pre-TB-385 history still carries `task_start` /
  `verify_passed` / `judge_call`, which readers accept alongside the new names
  (so historical `ap2 events` output is not misread as malformed).
- **ap2/howto.md** — reconcile the residual pre-TB-385 references with the
  already-updated `## Event schema` section:
  - `### Prose-judge diagnostics` (≈L397–450): make clear that per-prose-bullet
    results are now carried in the terminal `task_verify` event (the prose
    judge's per-bullet `judge_call` was folded in by TB-385); note that the
    `judge_call` diagnostics that remain apply to the still-streaming judge
    kinds (validator / janitor / ideation-scrub), not prose verification
    bullets. Keep the diagnostic techniques; only fix the attribution.
  - Stuck-detector reference (≈L1266): the detector keys on `task_solve` (with
    legacy `task_start` fallback), not `task_start` alone.
  - Light wording: where the verifier/validator/janitor/scrub calls are called
    "component calls" (≈L2281), they are adapter-routed agent-kind calls — the
    judges are not components post-TB-386. Adjust the phrasing without adding
    component-model design prose.
- Do NOT touch the `## Event schema` section's `task_solve` / `task_verify`
  entries (already correct, TB-385) except to keep cross-references consistent.
- Documentation only — no code changes.

## Design

- Source of truth for the vocabulary: `ap2/events.py` + `ap2/verify.py`
  (`task_verify` emission) and the existing howto.md `## Event schema` entries
  for `task_solve` / `task_verify` (TB-385). Match those; do not reintroduce
  `task_start` / `verify_passed` as current emissions.
- The skill and howto must agree on the three per-task lifecycle verbs
  (`task_solve` → `task_verify` → `task_complete`) and on the legacy-name
  tolerance for old history.

## Verification

- `grep -qE 'task_solve' skills/ap2/SKILL.md` — the skill lists `task_solve`.
- `grep -qE 'task_verify' skills/ap2/SKILL.md` — the skill lists `task_verify`.
- `uv run --extra dev pytest -q ap2/tests/ --ignore=ap2/tests/smoke` — the suite (incl. any docs-drift checks) passes.
- `skills/ap2/SKILL.md` Prose: the interesting-event-types list uses `task_solve` and `task_verify` as the current per-task lifecycle verbs and the example reflects `task_solve`, with a note that pre-TB-385 `task_start` / `verify_passed` / `judge_call` remain accepted in old history. Judge confirms via Read.
- `ap2/howto.md` Prose: the prose-judge diagnostics section attributes per-prose-bullet results to the terminal `task_verify` event (not a per-bullet `judge_call`), the stuck-detector reference keys on `task_solve` with legacy `task_start` fallback, and the judges are not described as "components." Judge confirms via Read.

## Out of scope

- The `## Event schema` `task_solve` / `task_verify` entries themselves (already correct — TB-385).
- Component-model design prose (architecture.md, separate) and the agent-backend section (TB-393).
- The `ap2-task` / `migrate-to-ap2` skills (no event-vocab staleness found).
- Any code change; documentation only.

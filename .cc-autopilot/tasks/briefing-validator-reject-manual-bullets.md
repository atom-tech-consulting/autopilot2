# Briefing validator: reject `Manual:` bullets in `## Verification` at queue-append time

## Goal

Current focus: ideation quality. TB-138 pinned the "auto-verifiable bullets only — no `Manual:` bullets" rule into `ap2/ideation.default.md` + the briefing template + `skills/ap2-task/SKILL.md`, and added `_check_briefings_manual_bullets` (`ap2/check.py:152`) as an operator-facing warning. But `_validate_briefing_structure` (`ap2/tools.py:345`) — the queue-append-time gate that fires before TB-N is allocated — does NOT reject `Manual:` bullets today. A briefing whose `## Verification` carries `- Manual: operator runs X and observes Y` still passes the gate, gets a TB-N, dispatches, and re-creates exactly the TB-122 failure mode (3 retries × 1 manual bullet → retry_exhausted → re-frozen despite implementation complete). This task closes that mechanical gap by mirroring the existing TB-138 lint into the validator, in the same spot TB-161 (goal-anchor) and TB-164 (Why-now) extended it.

Why now: closes the last documented "auto-verifiable bullets only" enforcement gap — the rule lives in three author-side surfaces (prompt, template, skill doc) plus a non-fatal lint, but the queue-append gate is the only place that mechanically blocks a malformed briefing before it costs a TB-N + a task-agent run. One ideation hallucination today is one tick away from a re-run of TB-122's retry_exhausted on a manual bullet.

## Scope

- `ap2/tools.py` — extend `_validate_briefing_structure` with a `## Verification`-body scan that rejects any line matching `_MANUAL_BULLET_RE` (importable from `ap2/check.py`, or duplicate the regex with a comment cross-referencing `check.py:144` so the two stay in sync).
- `ap2/init.py` — keep `BRIEFING_TEMPLATE`'s existing TB-138 prose unchanged; no template churn needed.
- `ap2/check.py` — `_check_briefings_manual_bullets` stays as the operator-facing warning for already-on-disk briefings (the validator only fires at queue-append).
- `ap2/tests/test_tools.py` — new tests: (a) reject add_backlog whose briefing has `- Manual: operator runs X` in Verification, (b) accept the same briefing with that bullet in `## Out of scope`, (c) case-insensitive match (`- manual: ...`, `- [Manual] ...`), (d) update-op via `do_operator_queue_append` rejects the same way (mirrors TB-154's update-op coverage).
- `ap2/ideation.default.md` — add a one-liner under the existing TB-138 paragraph noting "the validator now also rejects this at queue-append time (TB-171)" so the prompt + gate stay cross-referenced.

## Design

Single-spot extension of `_validate_briefing_structure` after the existing TB-164 Why-now check, before the final `return None`:

1. Locate the `## Verification` body (already extracted upstream — reuse `parse_verification_section`'s slice or re-call `_briefing_section_body(briefing_text, "Verification")`).
2. Scan the body line-by-line with `_MANUAL_BULLET_RE` (case-insensitive, anchored on bullet marker).
3. On match, return a structured error message naming the offending line + the TB-138 / TB-122 rationale + the fix (move to `## Out of scope`, or convert to a stubbed e2e test per TB-122's pattern).

Error message must be specific enough that the operator/agent can fix without re-reading source — same standard as the TB-161 / TB-164 messages already in this validator.

Failure mode: shared regex source vs duplicated. Importing `_MANUAL_BULLET_RE` from `ap2/check.py` introduces a tools→check coupling that doesn't exist today; duplicating with a `# keep in sync with check.py:144` comment is uglier but cheaper. Pick duplication unless tools.py already imports check.py somewhere — verify before deciding.

## Verification

- `uv run pytest -q ap2/tests/` — full suite passes
- `uv run pytest -q ap2/tests/test_tools.py -k manual_bullet` — new test set passes
- `grep -rnE "Manual" ap2/tools.py` — at least one match in `_validate_briefing_structure` (or in a sibling helper it calls), proving the new check landed
- New unit test in `ap2/tests/test_tools.py` named `test_validate_briefing_structure_rejects_manual_bullet_in_verification` (or similar) drives `_validate_briefing_structure` directly with a briefing whose `## Verification` has `- Manual: operator runs X` and asserts a non-None error string mentioning "Manual" or "auto-verifiable"
- New unit test asserts the same briefing with the manual bullet moved to `## Out of scope` (and a real shell bullet in `## Verification`) passes the validator
- New unit test in `test_tools.py` exercises `do_operator_queue_append` with `op="update"` + a briefing containing a Manual bullet and asserts the call returns `_err(...)` (mirrors TB-154's update-op coverage at `test_tb154_validate_briefing_structure_fires_for_update_op`)
- `grep -rnE "TB-171" ap2/ideation.default.md` — confirms the prompt cross-reference landed

## Out of scope

- Changing the existing `_check_briefings_manual_bullets` lint behavior (stays a warning for already-on-disk briefings; validator covers the queue-append path).
- Migrating any existing briefing with a Manual bullet (none on disk per `grep -rnE "^\s*[-*]\s*Manual:" .cc-autopilot/tasks/`); this task is forward-looking only.
- Auto-converting Manual bullets to stubbed e2e tests — the validator rejects, the author fixes; no automation.

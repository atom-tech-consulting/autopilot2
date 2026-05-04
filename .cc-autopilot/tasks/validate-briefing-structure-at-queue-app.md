# Validate briefing structure at queue-append time

## Goal

Reject briefings whose section structure doesn't match the canonical Goal / Scope / Design / Verification / Out-of-scope shape, at the `do_operator_queue_append` boundary, so the queue never accepts a briefing the per-task verifier can't parse. Today TB-135 enforces "briefing must be non-empty" but says nothing about structure — TB-153 (authored by the MM handler in chat) shipped with `## Acceptance` instead of `## Verification`, custom subsections, and a `## Files to touch` block at the wrong level. Result: `parse_verification_section` would have returned None, and the per-task verifier would have skipped the task entirely.

## Scope

Files to touch:

- `ap2/tools.py` — extend `do_operator_queue_append` (and any direct `add_*` paths) with a `_validate_briefing_structure(briefing_text)` call before allocating the TB-N. Return `_err(...)` with a clear message naming the missing/misnamed section.
- `ap2/init.py` — extract the canonical section list (`["Goal", "Scope", "Design", "Verification", "Out of scope"]`) into a shared constant `BRIEFING_REQUIRED_SECTIONS` so the validator and the empty-briefing template share one source of truth.
- `ap2/prompts.py` — update the MM handler prompt to include the canonical template inline, with a one-paragraph rule: "When asked to add a task, the briefing you pass to `operator_queue_append` MUST use exactly these section names (case-sensitive): `## Goal`, `## Scope`, `## Design`, `## Verification`, `## Out of scope`. The queue-append validator will reject any other section names."
- `ap2/tools.py` — update the `operator_queue_append` MCP tool docstring with the same canonical template + rejection note, so the agent reads the requirement before calling.
- `ap2/check.py` — extend the existing `_check_briefing_links` pass with a `_check_briefing_structure` that warns (not errors) on already-on-disk briefings whose section structure is non-canonical, so the operator can fix legacy entries opportunistically without breaking dispatch.
- Tests in `ap2/tests/test_tools.py` and `ap2/tests/test_check.py`.

## Design

### Validation rule

A briefing is valid when its rendered markdown contains, at the `##` level, at least: `Goal`, `Scope`, `Design`, `Verification`, `Out of scope` (case-sensitive, in any order). Extra `##`-level sections (e.g. `## Decision log`, `## Why`) are allowed — extension is fine, omission/rename is not. The `## Verification` section must additionally:

- Be parseable by `verify.parse_verification_section` (returns a non-None list).
- Contain at least one bullet.

Two reject paths matter:

(1) **Renamed Verification** (the TB-153 case): briefing has `## Acceptance` instead of `## Verification`. `parse_verification_section` returns None silently. Validator catches this by checking the literal section presence, not by relying on the verifier's tolerant parser.

(2) **Empty Verification** (covered today by the auto-fill skeleton TB-135 retired): briefing has `## Verification` but no bullets, or only a placeholder bullet like `(additional shell or prose bullets)`. Validator rejects on the empty-bullet-list case; the placeholder-bullet case is TB-138's territory (auto-verifiable rule) and stays out of scope here.

### Where validation runs

In `do_operator_queue_append` for `add_*` ops, BEFORE `_allocate_id` bumps `CLAUDE.md`'s next_task_id. Same placement as TB-134's single-line validation and TB-135's empty-briefing check — fail-fast, so a rejected add doesn't leak a TB-N.

The `update` op (TB-153 itself) uses the same validator when the update touches the briefing — same boundary.

### Error message shape

Match the existing `_err(...)` format from neighboring validations:

```
briefing structure invalid: missing section `## Verification`. The briefing
must contain `## Goal`, `## Scope`, `## Design`, `## Verification`, and
`## Out of scope` headings (case-sensitive). See ap2/init.py
BRIEFING_TEMPLATE for the canonical shape, or copy from any in-flight
briefing in `.cc-autopilot/tasks/`.
```

Specific enough that the calling agent / operator knows what to fix; references the template so they can self-serve.

### MM handler prompt + tool docstring updates

Both surfaces tell the agent the same thing as the validator's error message — "use these exact section names" — so the agent's first attempt usually passes. The docstring update is what the agent reads when picking the tool; the prompt update is what shapes its briefing-authoring behavior. Both pinned via `test_prompts.py` so future edits don't silently weaken the contract.

### `ap2 check` lint (warning, not error)

For briefings already on disk (legacy or operator-edited): walk `.cc-autopilot/tasks/*.md`, run the same `_validate_briefing_structure`, surface non-canonical ones as warnings in `ap2 check` output. Non-fatal — operator decides whether to fix or accept the legacy entry. Mirrors `_check_briefing_links`'s warning shape.

## Verification

- `uv run pytest -q ap2/tests/` — full regression gate passes (gating)
- `python3 -c "from ap2.init import BRIEFING_REQUIRED_SECTIONS; assert {'Goal','Scope','Design','Verification','Out of scope'} <= set(BRIEFING_REQUIRED_SECTIONS)"` — canonical section list extracted to a shared constant.
- `grep -qE "_validate_briefing_structure" ap2/tools.py` — validator function exists and is wired into the queue-append boundary.
- New unit test in `test_tools.py`: `do_operator_queue_append({"op":"add_backlog", "briefing":"<no Verification section>"})` returns `isError=True` with a message naming the missing section. Queue file unchanged; CLAUDE.md next_task_id unchanged (no leaked TB-N).
- New unit test in `test_tools.py`: a briefing with `## Acceptance` instead of `## Verification` is rejected the same way (TB-153's exact failure mode).
- New unit test in `test_tools.py`: a briefing with `## Verification` but no bullets is rejected.
- New unit test in `test_tools.py`: a canonical briefing (all five sections + at least one Verification bullet) is accepted; queue file gets the record.
- New unit test in `test_tools.py`: same checks fire on the `update` op when the update payload includes a `briefing` field.
- New unit test in `test_check.py`: `ap2 check` emits a warning-level Issue for an on-disk briefing missing a canonical section; emits nothing for a canonical briefing.
- New unit test in `test_prompts.py`: the MM handler prompt includes the literal section list (`## Goal`, `## Scope`, `## Design`, `## Verification`, `## Out of scope`) as part of the briefing-authoring instruction.
- New unit test in `test_tools.py`: the `operator_queue_append` MCP tool docstring includes the same literal section list.

## Out of scope

- Validating the QUALITY of Verification bullets (auto-verifiable vs manual, shell-bullet syntax). That's TB-138's territory — this task is structural-only.
- Allowing operator-defined section sets (e.g. a project that wants `## Risks` instead of `## Out of scope`). Single canonical shape simplifies the verifier and the briefing-authoring guidance; if a future use case demands flexibility, file separately.
- Auto-fixing renamed sections (`## Acceptance` → `## Verification`) at queue-append. Reject and force the author to fix; auto-fix hides authorship mistakes from the operator and complicates the validator's failure mode taxonomy.
- Migrating existing on-disk briefings whose structure doesn't match. The `ap2 check` warning lets the operator opportunistically fix; bulk migration is out of scope.

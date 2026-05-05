# Surface ideation_state.md "Open questions for operator" in `ap2 status` and web home

## Goal

Current focus: ideation quality. The ideation prompt's Step 0 schema mandates an `## Open questions for operator` section in `.cc-autopilot/ideation_state.md` whenever a focus item is `exhausted-needs-operator`, when goal.md appears to need updating, OR when the ideator notices a gap outside any current focus item. Today the ideator dutifully populates this section every cycle (see the current `ideation_state.md` for examples), but no operator-facing surface renders it — `ap2 status`, the web home page, and the cron status-report all skip the file entirely. Result: ideator-surfaced questions sit unread until the operator manually reads `.cc-autopilot/ideation_state.md`. This task closes that loop by reading the section out of the file and rendering it in the two highest-traffic operator surfaces (CLI status + web home).

Why now: the ideation prompt explicitly relies on this section for operator escalation ("Surfaced when a focus item is `exhausted-needs-operator`, when goal.md appears to need updating, OR when you noticed a gap outside any current focus item"), and TB-161/TB-163/TB-164/TB-168/TB-169 have moved the focus item close to `exhausted-needs-operator` — meaning future ideation cycles will increasingly rely on this section to ask the operator to update goal.md. Without rendering, the escalation path is silent.

## Scope

- New helper `parse_open_questions(path: Path) -> list[str]` in `ap2/ideation.py` (or a new `ap2/ideation_state.py` if cleanest) — reads `.cc-autopilot/ideation_state.md`, finds the `## Open questions for operator` section, returns its bullets as a list of single-line strings (newlines collapsed). Returns `[]` when the file or section is absent.
- `ap2/cli.py::cmd_status` — render a "Open questions for operator (N): ..." block when the list is non-empty (truncate to top 5 + `(+M more)` if >5; mirrors TB-151's `_format_pending_review_line` pattern).
- `ap2/cli.py::cmd_status` JSON output — add an `open_questions: [str, ...]` field (full list, untruncated).
- `ap2/web.py` — add a `_render_open_questions(cfg)` helper, mount above `_render_pending_queue` on the home page; matching CSS class for visual parity.
- `ap2/status_report.py` — render the same block in the cron status-report's snapshot via `state_extras` (mirrors TB-151's plumbing).
- Tests:
  - `ap2/tests/test_ideation_trigger.py` (or new `test_ideation_state.py`) — unit tests for `parse_open_questions`: file missing, section missing, section empty, section with 1 / 3 / 7 bullets, multi-line bullet collapsed.
  - `ap2/tests/test_cli.py` — `cmd_status` text + JSON include the open-questions block when non-empty; omit when empty.
  - `ap2/tests/test_web.py` — home page renders the block when non-empty; omits when empty.
  - `ap2/tests/test_status_report_skip.py` — cron status-report's `state_extras` carries the block.

## Design

`parse_open_questions` reuses the same section-slicing pattern as `ap2/check.py:_check_briefings_manual_bullets` (`_VERIFICATION_HEADER_RE` + `_NEXT_SECTION_RE`). Match the heading case-sensitively as `## Open questions for operator`. Slice from heading-end to the next `## ` (or EOF). Walk the slice line-by-line; each non-empty line starting with `- ` (or `* `) is a bullet. Collapse multi-line bullets (continuation lines indented under a bullet) by joining with a single space.

`cmd_status` rendering matches TB-151's pending-review-IDs line shape:

```
open questions for operator (3): <first bullet, ≤80 chars>; <second>; <third>
```

Truncate per-bullet to ~80 chars with ellipsis. Above 5 bullets, append `(+M more)` and stop.

Web rendering: card-style block above `_render_pending_queue`'s output. Each bullet a `<li>` so the operator can scan visually. Hide the card entirely when the list is empty.

Failure mode: ideator may write a `## Open questions for operator` section with prose paragraphs instead of bullets. Defense: `parse_open_questions` falls back to "split paragraphs by blank line; each paragraph is one entry". Cap at 7 entries to bound rendering cost.

## Verification

- `uv run pytest -q ap2/tests/` — full suite passes
- `uv run pytest -q ap2/tests/ -k "open_questions or ideation_state"` — new tests pass
- `grep -rnE "parse_open_questions|open_questions" ap2/cli.py ap2/web.py ap2/status_report.py` — wiring landed in all three surfaces
- New unit test `test_parse_open_questions_handles_missing_file_returns_empty_list` asserts empty list when `.cc-autopilot/ideation_state.md` is absent
- New unit test `test_parse_open_questions_handles_missing_section_returns_empty_list` asserts empty list when the file lacks the section
- New unit test `test_parse_open_questions_returns_bullets` builds a fixture file with three `## Open questions for operator` bullets and asserts the helper returns the three strings
- New unit test `test_parse_open_questions_caps_at_seven` asserts >7 bullets get truncated with a trailing "(+M more)" entry or similar
- New unit test in `test_cli.py` named `test_cmd_status_renders_open_questions_when_present` asserts the text-mode `ap2 status` output includes a line beginning with "open questions for operator" when the file has bullets
- New unit test in `test_cli.py` asserts the JSON-mode `ap2 status --json` output includes an `open_questions` key with the full bullet list
- New unit test in `test_cli.py` asserts the line is omitted entirely when `parse_open_questions` returns `[]`
- New unit test in `test_web.py` asserts `_render_home` includes the open-questions card HTML when bullets exist; absent when empty
- New unit test in `test_status_report_skip.py` asserts the cron status-report's snapshot block carries the rendered open-questions text

## Out of scope

- Editing `.cc-autopilot/ideation_state.md` from the CLI/web (the file is ideator-owned; this task is read-only surfacing).
- Notifying Mattermost when new questions appear (could be a follow-up task; for now the surfaces are passive).
- Acknowledging / dismissing questions via the operator queue — operator can edit `ideation_state.md` directly to clear them; no new op needed.
- Cross-cycle aging of questions (which deferred items have appeared in N consecutive cycles) — handled in a separate proposal if a recurring-question signal materializes.

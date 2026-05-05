# TB-174 — Parse focus statuses from ideation_state.md; auto-skip ideation cron when all focus items are `exhausted-needs-operator`

Tags: `#autopilot` `#ideation` `#cron` `#cost` `#observability`

## Goal

Add a `parse_focus_statuses(path: Path) -> dict[str, str]` helper to
`ap2/ideation.py` (sibling to TB-173's `parse_open_questions`) and
gate `_maybe_ideate` on it: when the last-written ideation_state.md's
`## Current focus assessment` reports ALL focus items with
`Status: exhausted-needs-operator`, the natural ideation cron skips
with a new `ideation_skipped reason=focus_exhausted` event and does
NOT call the SDK. This closes the goal-anchored gap from goal.md's
"Current focus: ideation quality" — specifically the Done-when bullet
"ideation reliably proposes goal-aligned next steps without drifting
into ap2-meta polish or scope creep, and stops proposing when the
target project's `## Done when` criteria are all met." Today
"stop proposing" only fires implicitly when goal-met; this extends it
to "ideator self-reports no actionable gaps remain." The forced path
(`ap2 ideate`, TB-159) keeps bypassing the new gate so the operator
can override after refreshing goal.md.

Why now: each natural ideation tick currently spends roughly
$0.10–$1.00 on SDK cost even after the prior cycle's assessment
explicitly self-reported `Status: exhausted-needs-operator` — that's
wasted spend on increasingly thin proposals while the operator's only
stop-signal is `AP2_IDEATION_DISABLED=1` (manual env knob). The
status field is already authored by the ideator at every cycle;
nothing reads it. TB-173 just landed the parallel
`parse_open_questions` parser + surfacing pipe; this finishes the
ideation_state.md → daemon-action loop on the focus-status field.

## Scope

- `ap2/ideation.py`: add `parse_focus_statuses(path: Path) -> dict[str, str]`
  near `parse_open_questions`. Walks the `## Current focus assessment`
  section, picks each top-level `**<focus item>**` bullet, extracts
  the nested `Status: <value>` sub-bullet for that item, returns
  `{focus_title: status}` (status normalized lowercase, one of
  `in-progress` / `exhausted-needs-operator` / `deferred`, or
  `unknown` for malformed). Returns `{}` when file/section is
  missing or no focus items parse.
- `ap2/ideation.py::_maybe_ideate`: insert a new gate AFTER the
  cooldown check and BEFORE `_run_ideation`. If the parsed map is
  non-empty AND every value is `exhausted-needs-operator`,
  `events.append(cfg.events_file, "ideation_skipped",
  reason="focus_exhausted", focus_count=N)`, call `mark_run(...)`
  to advance the cooldown clock (so a 30s tick loop doesn't
  retrigger), and return without invoking the SDK.
- `ap2/ideation.py::force_ideate`: leaves the new gate alone — the
  forced path already bypasses cooldown / disable / queue-depth and
  must continue to bypass the focus-exhausted gate too. Add a
  one-line docstring note.
- `ap2/tests/test_ideation_state.py`: add unit tests for
  `parse_focus_statuses` (single in-progress, single
  exhausted-needs-operator, multi-mixed, missing file, missing
  section, malformed status value, empty section).
- `ap2/tests/test_ideation_trigger.py`: add tests for the
  `_maybe_ideate` gate path and the `force_ideate` bypass.

## Design

`parse_focus_statuses` mirrors `parse_open_questions`'s shape: a
small section walker over the markdown source. Top-level focus-item
bullet shape is documented in `ap2/ideation.default.md` lines 53-66
(`- **<focus item verbatim from goal.md>**` followed by a nested
`- Status: <value>` bullet). Walker scans the section's lines, picks
out lines whose first non-whitespace token is `- **`, captures the
title between `**…**`, and reads forward to the next nested
`- Status:` line before the next top-level bullet.

Gate placement in `_maybe_ideate`: existing order is opt-out → Active
→ queue-depth → cooldown → SDK. The new gate slots AFTER cooldown so
the existing event ordering on the active path is preserved on the
"focus still in-progress" branch. When the gate fires it replaces
the SDK call entirely; `mark_run` still bumps so a daemon tick every
30s doesn't keep re-evaluating the gate (cost: one parse + one event
emit per cooldown window, negligible).

`ideation_skipped` is a new event name. `events.append` is
schemaless so no schema migration is needed; document the new event
in `ap2/README.md`'s event vocabulary list and add to
`IDEATION_RELEVANT_EVENT_TYPES` (TB-169 allowlist) so the next
cycle's ideator sees the skip in its events block.

## Verification

- `uv run pytest -q ap2/tests/` — full suite passes.
- `grep -nE "^def parse_focus_statuses" ap2/ideation.py` — parser
  helper exists at module scope.
- `grep -rnE "ideation_skipped" ap2/ideation.py` — new event name
  appears in `_maybe_ideate`.
- `grep -rnE "focus_exhausted" ap2/ideation.py` — reason string
  appears at the gate site.
- `grep -nE "ideation_skipped" ap2/ideation.py ap2/prompts.py` —
  event name added to `IDEATION_RELEVANT_EVENT_TYPES` (or
  whichever module hosts the allowlist) so future ideation prompts
  see the skip.
- New test `test_parse_focus_statuses_returns_status_per_focus_item`
  in `ap2/tests/test_ideation_state.py` exists and pins the parser's
  primary multi-item return shape.
- New test `test_parse_focus_statuses_returns_empty_when_section_missing`
  in `ap2/tests/test_ideation_state.py` covers the missing-section
  fallback.
- New test `test_maybe_ideate_skips_when_all_focus_exhausted`
  in `ap2/tests/test_ideation_trigger.py` writes a fixture
  ideation_state.md whose only focus item is
  `Status: exhausted-needs-operator`, drives `_maybe_ideate`, and
  asserts no SDK call is made AND an `ideation_skipped` event with
  `reason=focus_exhausted` was emitted.
- New test `test_maybe_ideate_runs_when_any_focus_in_progress`
  in `ap2/tests/test_ideation_trigger.py` asserts a mixed map
  (one `in-progress`, one `exhausted-needs-operator`) does NOT
  trip the gate.
- New test `test_force_ideate_bypasses_focus_exhausted_gate`
  in `ap2/tests/test_ideation_trigger.py` asserts `force_ideate`
  still invokes the SDK even when every focus item is
  `exhausted-needs-operator`.

## Out of scope

- Surfacing focus statuses in `ap2 status` text/JSON or web home
  (deferred per this cycle's `Considered & deferred` — TB-173
  already shows open questions; per-focus surfacing is incremental).
- Auto-rotating goal.md `## Current focus` (Non-goal: operator owns
  goal definition).
- New `ap2 focus` CLI subcommand to display the parsed map (the
  helper is queryable via Python directly; surface only if a real
  use case lands).
- Changing the cooldown duration on the skip path (the existing
  `AP2_IDEATION_COOLDOWN_S` env knob already governs).

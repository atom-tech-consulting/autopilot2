# Reject titles containing literal asterisk at queue-append time (TB-214-shape dead-letter prevention)

## Goal

Goal-anchored to goal.md's `Current focus: code quality` (line 38) —
specifically axis 1 ("Testing coverage": "every shipped CLI verb, MCP
tool, control-agent path, and env-knob-flagged behavior has automated
tests pinning the happy path AND at least one error path"). Extend
`_validate_single_line` in `ap2/tools.py` (L126-139) so it also rejects
titles containing the literal character `*`, in addition to the existing
TB-134 newline / carriage-return check. Mirror TB-134's loud-reject
pattern: any title that would break `ap2/board.py:TASK_LINE_RE`'s
`\*\*(?P<title>[^*]+)\*\*` group must fail at the write-time gate,
not silently produce a dead-letter task line that operator-queue verbs
can no longer address.

Why now: TB-214 (ideation-authored at 2026-05-13T10:35:58Z; title
`Pin 4 sandbox install-* CLI verbs (...)`) is currently dead-letter on
disk. The literal `*` in `install-*` collides with TASK_LINE_RE's title
group; `Board.find('TB-214')` returns `None`; `ap2 approve TB-214`,
`ap2 update TB-214`, and `ap2 delete TB-214` all raise `KeyError`; the
task is only visible via a `board_malformed_line` event in
`events.jsonl` that operators have to grep for. `ap2 status --json`
silently reports `pending_review: 4` with `pending_review_ids: ["TB-211",
"TB-212", "TB-213", "TB-215"]` against 5 Backlog rows — TB-214 disappears
from the operator-facing surface entirely. Without this gate, the trap
recurs every cycle an ideation proposal (or `ap2 add`) names a glob,
wildcard, or footnote-marker literally in the title. The fix is a
single field-specific extension to one existing helper; the failure
mode is concrete, reproduced in production, and would otherwise need
hand-edit recovery every recurrence.

## Scope

1. `ap2/tools.py`:
   - Add a sibling constant near `SINGLE_LINE_ERR` (L86):
     `TITLE_NO_ASTERISK_ERR = "title must not contain '*' — TASKS.md's
     bold-fence parser (board.py TASK_LINE_RE) collapses on embedded
     asterisks; rename or describe the wildcard in the briefing prose
     instead"`.
   - Extend `_validate_single_line(field, value)`: after the existing
     newline check, if `field == "title"` and `"*" in value`, return
     `TITLE_NO_ASTERISK_ERR`. Keep the newline check unchanged and
     return early as today. Field-specific guard so description / tag /
     blocked values with `*` continue to round-trip (the parser
     doesn't choke on those fields).

2. `ap2/tests/`:
   - Add `test_validate_single_line_rejects_asterisk_in_title` in
     `ap2/tests/test_tools.py` (or wherever the existing
     `_validate_single_line` newline tests live — `grep -rn
     "_validate_single_line\|SINGLE_LINE_ERR" ap2/tests/` to locate).
     Assert: `_validate_single_line("title", "foo*bar")` returns a
     non-None error containing `"*"`; `_validate_single_line("title",
     "foo bar")` returns None; `_validate_single_line("description",
     "foo*bar")` returns None (field-specific).
   - Add three entry-point tests pinning end-to-end rejection (one per
     call site of `_validate_single_line("title", ...)`):
     - `do_board_edit({"action": "add_backlog", "title": "has *
       asterisk", ...})` returns `isError=True` with the new message
       and writes nothing to `TASKS.md`.
     - `do_operator_queue_append({"op": "add_backlog", "title": "has *
       asterisk", ...})` returns `isError=True` with the new message
       and appends nothing to `operator_queue.jsonl`.
     - `cli.cmd_add` invoked with `--title "has * asterisk"` exits
       non-zero, prints the new error to stderr, and writes nothing to
       `TASKS.md` or `operator_queue.jsonl`. Use the existing CLI test
       harness (`subprocess` / `Runner` — whichever pattern
       `test_cli.py` already uses for TB-134's newline test).
   - Add `test_task_line_re_malformed_on_asterisk_title` in
     `ap2/tests/test_board.py` (or wherever `TASK_LINE_RE` /
     `Board.load` tests live): build a Board with a hand-crafted line
     `- [ ] **TB-999** **foo *bar** \`#x\` —`, parse, assert that line
     appears in `b.malformed_lines` AND `Board.find("TB-999")` returns
     None. Documents the parser limitation that motivates the gate.

3. No template / docs change required. The new error message is
   self-documenting; skill files (`skills/ap2-task/SKILL.md`) don't
   enumerate title char rules today and don't need to start now.

## Design

- Field-specific (`if field == "title"`) extension of the existing helper
  keeps the change local, matches TB-134's shape, and avoids retroactive
  rejection of existing description / tag / blocked values that contain
  `*` for legitimate reasons.
- Single shared validator covers all three write paths (cmd_add,
  do_board_edit, do_operator_queue_append) without per-path
  duplication — the helper's existing call-site fan-out (see
  `Grep _validate_single_line ap2/` at the validator's docstring L131-134)
  already routes every TASKS.md-bound input through it.
- No parser refactor: TASK_LINE_RE staying line-anchored and
  `[^*]+`-bounded is correct for current shape; the cost of widening
  the title group to balanced-`**` matching is bigger than the
  cost of forbidding `*` in titles. TB-119 (Frozen) tracks the
  mistune-AST direction that would subsume this whole class; reopen
  there if a second char class needs similar tolerance.
- No auto-sanitize: TB-134's docstring (L82-85) explicitly explains
  why silent newline-to-space rewriting was rejected — same reasoning
  applies to `*`. Loud reject + actionable hint forces the caller to
  pick the right semantic split (move wildcard mention into briefing
  prose, or rephrase the title).
- TB-214 itself stays out of scope: that on-disk row needs an operator
  hand-edit (no queue verb can address an un-findable task id). The
  unblock recipe is surfaced in `ideation_state.md`'s "Decisions needed
  from operator" section this cycle.

## Verification

- `grep -q 'TITLE_NO_ASTERISK_ERR' ap2/tools.py` — new error constant lands.
- `grep -nE 'field == .title.' ap2/tools.py` — at least one hit inside
  `_validate_single_line` (the field-specific guard).
- `uv run pytest -q ap2/tests/test_tools.py -k asterisk` — new title-
  asterisk validator unit tests pass.
- `uv run pytest -q ap2/tests/test_board.py -k asterisk` — new parser-
  side malformed-line regression test passes.
- `uv run pytest -q ap2/tests -k asterisk` — entry-point tests across
  cli / board_edit / operator_queue all pass (covers wherever they
  land if test files differ from the above guess).
- `uv run pytest -q` — full suite passes; no regression in TB-134
  newline checks or any downstream caller.
- `uv run python -c "from ap2.tools import _validate_single_line; assert _validate_single_line('title', 'install-*') is not None; assert _validate_single_line('title', 'install verbs') is None; assert _validate_single_line('description', 'foo*bar') is None; assert _validate_single_line('tag', '#foo*bar') is None"` — exit 0; pins field-specific gating end-to-end.

## Out of scope

- Fixing TB-214's on-disk task line (operator hand-edit; surfaced as a
  separate Decision-needed item in `ideation_state.md`).
- Parser-level tolerance of `*` in titles (TB-119 Frozen tracker for
  the mistune-AST migration; defer until a second char class needs
  similar tolerance).
- Rejecting `*` in `description` / `tag` / `blocked` fields (parser
  doesn't choke on those; would be pro-forma).
- Auto-sanitizing operator inputs (silently rewrites intent; TB-134
  rejected the auto-fix shape for the same reason).
- Surfacing `malformed_line` counts in `ap2 status` / web view as
  secondary hardening — worth a separate proposal once the gate ships
  and operators start asking "what slipped past?", but not now.

# TB-134 — Reject ap2 add when description contains newlines

## Goal

Today operator board ops accept multi-line descriptions silently — they round-trip through Task.render() into TASKS.md as actual newline bytes, splitting the rendered task line across multiple physical lines. The first line still parses (TASK_LINE_RE is per-line) but the trailing [→ brief](...) link gets stranded on a different line and is lost from the parsed Task; subsequent lines are flagged as board_malformed_line events but stay on disk. Hit on TB-132 and TB-133 on 2026-04-30 (had to manually re-collapse). Defensive auto-collapse (replacing newlines with spaces) is too lenient: it silently rewrites the operator's intent, and a 400-char wall-of-text crammed into one line is rarely what they actually meant. Better: reject the input loudly and make the caller decide how to simplify. Scope: (1) cli.cmd_add: validate args.description before passing to do_board_edit; if it contains any \n or \r byte, exit non-zero with a clear message ('description must be a single line — break long content into briefing.md instead, or summarize to one line'). (2) Same validation in do_board_edit (tools.py) so MCP-driven callers (ideation, MM handler, future operator-queue ops) hit the same gate. Returns _err with the same message; the calling agent retries. (3) Apply to title and tags too while we're there — they have the same single-line constraint. (4) Tests: unit test that ap2 add -d 'first\nsecond' exits non-zero and writes nothing. Test that do_board_edit({description: 'a\nb'}) returns isError. (5) Document in skills/ap2-task/SKILL.md ('description: single line; for richer prose, edit the briefing after the add'). Why reject not auto-fix: forces operators to choose between summarizing inline or moving detail into the briefing — the right semantic split. Auto-collapse hides the choice and produces ugly run-on sentences.

## Scope

- (file / module to change)

## Design

(filled in by /tb prep or by the ideation agent)

## Verification

- `uv run pytest -q ap2/tests/` — full regression gate passes (gating)
- New unit test in `test_cli.py`: `ap2 add "title" -d $'first\nsecond"` (real newline) exits non-zero, prints a clear error message mentioning "single line" and pointing at the briefing, AND TASKS.md is unchanged on disk.
- New unit test in `test_cli.py`: same with a `\r` byte → also rejected with the same error.
- New unit test in `test_cli.py`: `ap2 add "title with\nnewline" --briefing-file ...` (newline in title) is rejected.
- New unit test in `test_cli.py`: a tag containing a newline is rejected.
- New unit test in `test_cli.py`: regression — `ap2 add "title" -d "single-line description" ...` continues to succeed unchanged.
- New unit test in `test_tools.py`: `do_board_edit({"action":"add_backlog","title":"t","description":"a\nb",...})` returns `isError=True` with a message matching the CLI's; nothing is written to TASKS.md or the briefings dir.
- New unit test in `test_tools.py`: same for `add_ready` and `add_frozen`.
- The error message text guides the operator to two correct paths: (a) summarize to one line, (b) put the rich content in the briefing file. Avoid suggesting auto-collapse.
- The diff updates `skills/ap2-task/SKILL.md` documenting the single-line constraint for `description`, `title`, and tags.

## Out of scope

- Any auto-collapse / auto-fix behavior — the whole point is to reject loudly.
- Length limits on description (separate concern).
- Validating tag character classes beyond newline rejection.

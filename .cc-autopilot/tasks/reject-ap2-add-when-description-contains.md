# TB-134 — Reject ap2 add when description contains newlines

## Goal

Today operator board ops accept multi-line descriptions silently — they round-trip through Task.render() into TASKS.md as actual newline bytes, splitting the rendered task line across multiple physical lines. The first line still parses (TASK_LINE_RE is per-line) but the trailing [→ brief](...) link gets stranded on a different line and is lost from the parsed Task; subsequent lines are flagged as board_malformed_line events but stay on disk. Hit on TB-132 and TB-133 on 2026-04-30 (had to manually re-collapse). Defensive auto-collapse (replacing newlines with spaces) is too lenient: it silently rewrites the operator's intent, and a 400-char wall-of-text crammed into one line is rarely what they actually meant. Better: reject the input loudly and make the caller decide how to simplify. Scope: (1) cli.cmd_add: validate args.description before passing to do_board_edit; if it contains any \n or \r byte, exit non-zero with a clear message ('description must be a single line — break long content into briefing.md instead, or summarize to one line'). (2) Same validation in do_board_edit (tools.py) so MCP-driven callers (ideation, MM handler, future operator-queue ops) hit the same gate. Returns _err with the same message; the calling agent retries. (3) Apply to title and tags too while we're there — they have the same single-line constraint. (4) Tests: unit test that ap2 add -d 'first\nsecond' exits non-zero and writes nothing. Test that do_board_edit({description: 'a\nb'}) returns isError. (5) Document in skills/ap2-task/SKILL.md ('description: single line; for richer prose, edit the briefing after the add'). Why reject not auto-fix: forces operators to choose between summarizing inline or moving detail into the briefing — the right semantic split. Auto-collapse hides the choice and produces ugly run-on sentences.

## Scope

- (file / module to change)

## Design

(filled in by /tb prep or by the ideation agent)

## Verification

Concrete acceptance criteria the daemon's per-task verifier (TB-69)
runs after the agent's commit. Shell-command bullets (backtick-fenced
at the start of the bullet) are run automatically; prose bullets are
judged by an SDK call against the diff.

- `uv run pytest -q` — full suite passes
- (additional shell or prose bullets)

## Out of scope

- (filled in)

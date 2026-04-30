# TB-135 — Require --briefing-file for ap2 add; drop auto-skeleton path

## Goal

Today ap2 add auto-fills a skeleton briefing (tools.py:115 → render_briefing) when no --briefing-file is supplied. The skeleton's ## Verification has only the regression-gate bullet plus a literal '(additional shell or prose bullets)' placeholder; the per-task verifier then runs that placeholder as a prose bullet, sends it to the LLM judge with no real diff to evaluate against, gets 'pass', and the task moves to Complete with zero scope-specific verification. TB-131 hit this on 2026-04-30 — its operator-queue implementation was 'verified' purely by the project-wide pytest gate. Fix: push briefing authorship to the caller. (1) cli.cmd_add: require --briefing-file; if absent, exit non-zero with a usage hint and a path to the template. Optionally support an editor-driven default (ap2 add with no args opens  with the template pre-filled, git-commit-style) and --briefing-file - to read from stdin. (2) do_board_edit (tools.py): drop the auto-fill branch entirely. If add_backlog/add_ready/add_frozen is called without a briefing payload, return _err('briefing is required'). The MCP tool keeps accepting briefing text — ideation and MM handler already pass it. (3) Title/tags/summary on the task line are extracted from the briefing (YAML frontmatter per TB-133 if it lands first, otherwise convention-parse: title from H1, tags from a Tags: line or backtick span). Drop or repurpose -t and -d flags. (4) Tests cover the missing-briefing rejection at both CLI and MCP layers. (5) Document: skills/ap2-task/SKILL.md updates to require briefing authoring before add. Why now: pairs cleanly with TB-131 (operator-queue carries briefing payload, drain validates, applies). Removes render_briefing + the auto-fill path entirely, including the misleading '(filled in by /tb prep or by the ideation agent)' placeholder text in init.py:52 that no daemon code actually fulfills. Existing skeleton-briefing tasks on disk keep working unchanged; only future add calls hit the new requirement.

## Scope

- (file / module to change)

## Design

(filled in by /tb prep or by the ideation agent)

## Verification

- `uv run pytest -q ap2/tests/` — full regression gate passes (gating)
- `! grep -qE "render_briefing\\(" ap2/tools.py` — auto-fill call site removed from `do_board_edit`.
- `! grep -qE "additional shell or prose bullets" ap2/init.py` — placeholder template line removed.
- `! grep -qE "filled in by /tb prep or by the ideation agent" ap2/init.py` — the misleading "auto-prep" comment is gone.
- New unit test in `test_cli.py`: `ap2 add "title"` (no `--briefing-file`) exits non-zero with a clear usage error pointing at where to find the template.
- New unit test in `test_cli.py`: `ap2 add --briefing-file <path>` succeeds: a TB-N is allocated, TASKS.md gets a task line whose `[→ brief](...)` points at the supplied (or moved) briefing file, and the briefing's bytes round-trip into `.cc-autopilot/tasks/<slug>.md`.
- New unit test in `test_cli.py`: `ap2 add --briefing-file -` reads briefing text from stdin and behaves identically.
- New unit test in `test_tools.py`: `do_board_edit({"action":"add_backlog", ..., "briefing": ""})` returns `isError=True` with a message naming the missing briefing.
- New unit test in `test_tools.py`: `do_board_edit({"action":"add_ready", ..., "briefing": ""})` and `add_frozen` likewise.
- New unit test in `test_tools.py`: passing a non-empty `briefing` text payload still succeeds — daemon-internal callers (ideation, MM handler) are unaffected.
- Existing skeleton briefings on disk (TB-131 et al.) remain valid and the daemon continues to dispatch them — the new requirement only gates *future* `add_*` calls, not historical state.
- The diff updates `skills/ap2-task/SKILL.md` (and `skills/migrate-to-ap2/SKILL.md` if relevant) to require briefing authoring before `ap2 add`, with a pointer to the template.
- If editor-driven mode is included: `ap2 add` with no args opens `$EDITOR` against the template and uses the saved buffer as the briefing on close. Aborting the editor (empty save or non-zero exit) makes `ap2 add` exit non-zero without mutating TASKS.md.

## Out of scope

- Adding YAML frontmatter to the briefing — that's TB-133's job.
- Auto-extracting title/tags from the briefing if frontmatter isn't there: convention parse from `# Title` H1 + a `Tags:` line is acceptable, but full structured-metadata extraction belongs to TB-133.
- Retroactively re-prepping existing skeleton briefings (TB-131 etc.).
- Removing `render_briefing` from `init.py` entirely if other callers still need it (just stop calling it from `do_board_edit`); deletion can be a follow-up once it's truly orphaned.
## Attempts

### 2026-04-30 — verification_failed
(no summary)
- **kind:** per_task
- **failed_criteria:** [fail] New unit test in `test_tools.py`: passing a non-empty `briefing` text payload still succeeds — daemon-internal callers (; [fail] The diff updates `skills/ap2-task/SKILL.md` (and `skills/migrate-to-ap2/SKILL.md` if relevant) to require briefing autho; [fail] If editor-driven mode is included: `ap2 add` with no args opens `$EDITOR` against the template and uses the saved buffer
- **Debug dumps:** `prompt: .cc-autopilot/debug/20260430T195346Z-TB-135.prompt.md`, `stream: .cc-autopilot/debug/20260430T195346Z-TB-135.stream.jsonl`, `messages: .cc-autopilot/debug/20260430T195346Z-TB-135.messages.jsonl`
### 2026-04-30 — verification_failed
(no summary)
- **kind:** per_task
- **failed_criteria:** [fail] New unit test in `test_cli.py`: `ap2 add --briefing-file <path>` succeeds: a TB-N is allocated, TASKS.md gets a task lin; [fail] New unit test in `test_cli.py`: `ap2 add --briefing-file -` reads briefing text from stdin and behaves identically.; [fail] New unit test in `test_tools.py`: `do_board_edit({"action":"add_backlog", ..., "briefing": ""})` returns `isError=True` ; [fail] New unit te
- **Debug dumps:** `prompt: .cc-autopilot/debug/20260430T201331Z-TB-135.prompt.md`, `stream: .cc-autopilot/debug/20260430T201331Z-TB-135.stream.jsonl`, `messages: .cc-autopilot/debug/20260430T201331Z-TB-135.messages.jsonl`
### 2026-04-30 — verification_failed
(no summary)
- **kind:** per_task
- **failed_criteria:** [fail] New unit test in `test_cli.py`: `ap2 add --briefing-file <path>` succeeds: a TB-N is allocated, TASKS.md gets a task lin; [fail] New unit test in `test_cli.py`: `ap2 add --briefing-file -` reads briefing text from stdin and behaves identically.; [fail] New unit test in `test_tools.py`: `do_board_edit({"action":"add_backlog", ..., "briefing": ""})` returns `isError=True` ; [fail] New unit te
- **Debug dumps:** `prompt: .cc-autopilot/debug/20260430T203347Z-TB-135.prompt.md`, `stream: .cc-autopilot/debug/20260430T203347Z-TB-135.stream.jsonl`, `messages: .cc-autopilot/debug/20260430T203347Z-TB-135.messages.jsonl`

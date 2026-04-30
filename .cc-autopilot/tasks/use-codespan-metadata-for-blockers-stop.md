# TB-132 — Use codespan metadata for blockers; stop regex-on-prose

## Goal

Today _BLOCKED_CLAUSE_RE (board.py:40) regexes a task's free-text description for the literal substring '(blocked on: ...)'. This collides with prose: TB-121's description literally contains the phrase '(blocked on: review)' as part of explaining the proposed feature, and the parser auto-blocks the task on a non-existent token 'review' — TB-121 sits in Backlog forever, never auto-promoted. Same fragility class as TB-91 (verification heading regex bit by parentheticals) but cheaper to fix than the full mistune-AST migration TB-119 covers.

Fix shape: move structured blocker data out of the description blob into a dedicated codespan, mirroring the existing tag pattern. Today task lines look like:

  - [ ] **TB-121** **Title** `#tag1` `#tag2` — free prose where (blocked on: review) lives [→ brief](...)

Proposed:

  - [ ] **TB-121** **Title** `#tag1` `@blocked:review,TB-5` — free prose [→ brief](...)

The `@blocked:...` codespan is parsed off the same backtick-span list TASK_LINE_RE already captures for tags; no regex against the description blob. Multiple blocker tokens stay comma-separated inside the codespan. Backward compatible — existing tasks without the codespan parse to 'no blockers', identical behavior to today.

Scope:
(1) Extend TASK_LINE_RE (or split tag-vs-meta capture) to recognize `@<key>:<value>` codespans alongside `#<tag>` ones. Single regex rule: any backtick span starting with @ is metadata, any starting with # is a tag.
(2) Add Task.meta: dict field. Populate during parse_task_line; Task.blocked_on reads from self.meta.get('blocked', '').split(',') instead of regexing description.
(3) Update Task.render() to emit `@<key>:<value>` codespans after tags, before the em-dash.
(4) ap2 add --blocked TB-5 (or similar flag) writes the codespan instead of injecting (blocked on: ...) into description. Ideation and other writers (the gating mechanism TB-121 itself describes) emit the codespan too.
(5) Migrate the few extant (blocked on: ...) clauses by hand. Leave the regex behavior in place during transition: if meta['blocked'] is empty, fall back to the legacy regex; once everything's migrated, drop the regex entirely.
(6) Tests: TB-121's exact prose (containing '(blocked on: review)' as descriptive text) must NOT cause auto-promote to skip the task. A task with `@blocked:TB-5` codespan AND TB-5 not Complete must skip. A task with `@blocked:TB-5` codespan AND TB-5 Complete must dispatch.

Why not full AST: TB-119 (mistune AST for the Board parser) is the bigger-but-correct lift; this task is the minimal version that absorbs the specific failure mode (TB-121 self-reference) without rewriting the parsing surface. The tag/meta codespan format extends naturally to other structured fields (priority, owner, due_date) later if we want, without ever expanding the regex to look inside the prose blob.

## Scope

- (file / module to change)

## Design

(filled in by /tb prep or by the ideation agent)

## Verification

- `uv run pytest -q ap2/tests/` — full regression gate passes (gating)
- `grep -qE "@<key>:|@blocked" ap2/board.py` — new codespan parser added
- New unit test in `test_board.py`: `parse_task_line` populates a `meta` dict from `@<key>:<value>` codespans alongside the existing `tags` from `#<tag>` codespans; tags and meta are kept distinct.
- New unit test in `test_board.py`: `Task.blocked_on` returns `["TB-5"]` for a task with `` `@blocked:TB-5` `` codespan and ignores any `(blocked on: ...)` substring in the description.
- New unit test in `test_board.py`: `Task.render()` emits `` `@blocked:...` `` codespans after `#tags`, before the em-dash; round-trip parse → render is byte-identical.
- New unit test in `test_board.py`: a task with only legacy `(blocked on: TB-5)` in description (no codespan) keeps parsing as blocked under the transition fallback, so existing tasks aren't broken.
- New unit test in `test_board.py`: TB-121's exact prose (description containing `(blocked on: review)` as descriptive text, no `@blocked` codespan) parses with `blocked_on == []` once the legacy fallback is dropped — the original failure mode no longer happens.
- New unit test: `ap2 add --blocked TB-5,review "title" --briefing-file ...` writes the codespan in the rendered task line and not into the description.
- The diff updates `skills/ap2-task/SKILL.md` (or the equivalent doc) to mention the new `@<key>:<value>` codespan convention so future callers don't reach for `(blocked on: ...)` again.

## Out of scope

- TB-119's full mistune-AST migration of the Board parser.
- TB-133's briefing-frontmatter approach (broader, lands later).
- Migrating any pre-existing `(blocked on: ...)` clauses in production data — this repo has none right now.
## Attempts

### 2026-04-30 — verification_failed
(no summary)
- **kind:** per_task
- **failed_criteria:** [fail] New unit test in `test_board.py`: `Task.blocked_on` returns `["TB-5"]` for a task with ``@blocked:TB-5`` codespan and ig; [fail] New unit test in `test_board.py`: `Task.render()` emits ``@blocked:...`` codespans after `#tags`, before the em-dash; ro; [fail] New unit test in `test_board.py`: a task with only legacy `(blocked on: TB-5)` in description (no codespan) keeps parsin; [fail] New un
- **Debug dumps:** `prompt: .cc-autopilot/debug/20260430T211503Z-TB-132.prompt.md`, `stream: .cc-autopilot/debug/20260430T211503Z-TB-132.stream.jsonl`, `messages: .cc-autopilot/debug/20260430T211503Z-TB-132.messages.jsonl`

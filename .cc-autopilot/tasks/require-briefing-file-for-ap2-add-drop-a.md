# TB-135 — Require --briefing-file for ap2 add; drop auto-skeleton path

## Goal

Today ap2 add auto-fills a skeleton briefing (tools.py:115 → render_briefing) when no --briefing-file is supplied. The skeleton's ## Verification has only the regression-gate bullet plus a literal '(additional shell or prose bullets)' placeholder; the per-task verifier then runs that placeholder as a prose bullet, sends it to the LLM judge with no real diff to evaluate against, gets 'pass', and the task moves to Complete with zero scope-specific verification. TB-131 hit this on 2026-04-30 — its operator-queue implementation was 'verified' purely by the project-wide pytest gate. Fix: push briefing authorship to the caller. (1) cli.cmd_add: require --briefing-file; if absent, exit non-zero with a usage hint and a path to the template. Optionally support an editor-driven default (ap2 add with no args opens  with the template pre-filled, git-commit-style) and --briefing-file - to read from stdin. (2) do_board_edit (tools.py): drop the auto-fill branch entirely. If add_backlog/add_ready/add_frozen is called without a briefing payload, return _err('briefing is required'). The MCP tool keeps accepting briefing text — ideation and MM handler already pass it. (3) Title/tags/summary on the task line are extracted from the briefing (YAML frontmatter per TB-133 if it lands first, otherwise convention-parse: title from H1, tags from a Tags: line or backtick span). Drop or repurpose -t and -d flags. (4) Tests cover the missing-briefing rejection at both CLI and MCP layers. (5) Document: skills/ap2-task/SKILL.md updates to require briefing authoring before add. Why now: pairs cleanly with TB-131 (operator-queue carries briefing payload, drain validates, applies). Removes render_briefing + the auto-fill path entirely, including the misleading '(filled in by /tb prep or by the ideation agent)' placeholder text in init.py:52 that no daemon code actually fulfills. Existing skeleton-briefing tasks on disk keep working unchanged; only future add calls hit the new requirement.

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

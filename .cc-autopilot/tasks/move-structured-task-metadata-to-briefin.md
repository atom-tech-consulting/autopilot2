# TB-133 — Move structured task metadata to briefing YAML frontmatter

## Goal

Longer-term structural answer to the 'regex on description prose' fragility class (TB-91, TB-121). Generalizes beyond the immediate blocker-codespan fix in TB-132.

Idea: TASKS.md task line becomes pure display — id + title + tags + briefing link + free-prose description summary. Anything *structured* (blockers, priority, owner, due_date, retry-policy overrides, …) lives in the briefing's YAML frontmatter, where it's a natural fit alongside the briefing's verification section, design notes, and attempt history. The Board parser stops trying to extract structure from prose entirely.

Today task line:

```
- [ ] **TB-121** **Title** `#tag` — '...prose containing (blocked on: review)...' [→ brief](...)
```

Proposed task line (display only):

```
- [ ] **TB-121** **Title** `#tag` — short prose summary [→ brief](...)
```

Briefing front matter:

```yaml
---
blocked_on: [review]
priority: high
owner: lzhang
---
# TB-121 — Title
## Goal
...
```

## Scope

- (file / module to change)

## Design

(1) Briefing reader: parse YAML frontmatter (PyYAML already a dep) on each briefing load. Add to existing briefing-parse path used by verifier. Cache by briefing-mtime so dispatchability checks aren't I/O-bound.

(2) `Board.next_dispatchable`: when checking a task's blockers, resolve briefing path → load frontmatter → read `blocked_on`. Falls back to today's regex-on-description path for tasks without frontmatter (transition-friendly).

(3) Helpers: `ap2 add --blocked TB-5` writes `blocked_on` into the new briefing's frontmatter instead of mutating description. `ap2 approve TB-N` (the gating mechanism TB-121 describes) strips the blocker by editing frontmatter.

(4) Migration: optional `ap2 sandbox migrate-metadata` script that walks existing tasks, extracts `(blocked on: ...)` clauses, removes them from descriptions, and writes them as frontmatter `blocked_on:` arrays in each briefing. Idempotent.

(5) Tests: `blocked_on` discovery from frontmatter; cache invalidation on briefing-mtime; fallback to regex-on-description when frontmatter absent.

## Verification

Concrete acceptance criteria the daemon's per-task verifier (TB-69)
runs after the agent's commit. Shell-command bullets (backtick-fenced
at the start of the bullet) are run automatically; prose bullets are
judged by an SDK call against the diff.

- `uv run pytest -q` — full suite passes
- New unit tests cover frontmatter parsing, mtime caching, and regex-fallback paths
- `Board.next_dispatchable` correctly skips a task whose briefing front matter has unmet `blocked_on` and dispatches one whose blockers are all satisfied
- The `ap2 sandbox migrate-metadata` script (if implemented) is idempotent and produces no diff on second run

## Out of scope

- TB-132 (codespan blockers) — that's the cheaper interim fix
- TB-119 (full mistune-AST migration of Board parser)
- Adding a fixed schema for frontmatter fields beyond `blocked_on` (kept open-ended)

## Why Frozen

TB-132 (codespan approach) is the cheaper minimal fix that absorbs TB-121's specific failure mode without changing storage shape. Thaw this when:

- (a) a second structured field is needed (priority, owner, due_date) — at which point repeating the codespan trick gets ugly;
- (b) briefing-as-source-of-truth is otherwise needed (e.g. richer `ap2 web` display); or
- (c) TB-119's AST migration lands and we want to clean up the metadata story at the same time.

# Trim `_current_state_block` for ideation: drop board counts + recent commits, keep `now:`

## Goal

Sharpen ideation's prompt signal density by removing two pieces of `_current_state_block` content that don't pay rent for ideation specifically:

1. **Board counts** (`0A / 1R / 4B / 0P / 40C / 3F`) — the ideation agent reads `TASKS.md` directly per its read-order (Step 4 of `ap2/ideation.default.md`), where it gets the same information with full task titles and per-section detail. The pre-flight count is redundant.

2. **Recent 10 commits** (`git log --oneline -n 10`) — empirically ~60% are `state:` daemon meta-commits (`state: drained 1 operator op(s)`, `state: TB-N → Complete`, `state: ideation`) carrying no signal ideation can act on. The remaining ~40% (`TB-N: <feature>` lines) are subsumed by `progress.md` — the canonical "what shipped + why" surface — which the agent reads at Step 5 with richer context.

Keep `now:` — it's load-bearing: control agents have no Bash and no other clock; the prompt-injected timestamp is the agent's only source for the `_Last updated:` line in the `ideation_state.md` schema (line 41 of `ap2/ideation.default.md`).

Why now: the goal.md "Current focus: ideation quality" emphasizes prompt-shape work, and signal-density audits (this conversation, ~2026-05-04) identified `_current_state_block` as one of three sources of ideation prompt bloat alongside the events block and CLAUDE.md read instruction. Trimming each is independently mergeable; this is the cheapest one and clears the path for the other two without touching ideation's behavioral instructions.

This change is **opt-in via kwargs** so status-report cron (which DOES use the board counts in its posted report and the recent commits to summarize activity) continues to behave as before.

## Scope

- `ap2/prompts.py::_current_state_block` — add `include_board: bool = True` and `include_commits: bool = True` kwargs. When False, omit the corresponding sub-block from the rendered string (header line + content). `now:` and `extras` (TB-151) are unaffected.
- `ap2/prompts.py::build_control_prompt` — accept the same two kwargs and forward to `_current_state_block`. Defaults stay True for backwards compatibility with status-report cron and any future callers.
- `ap2/ideation.py::_maybe_ideate` — when calling `build_control_prompt`, pass `include_board=False, include_commits=False`.
- New tests in `ap2/tests/test_prompts.py`.

## Design

### Kwarg shape, not separate function

Adding boolean kwargs keeps a single source of truth for the `_current_state_block` rendering. The alternative (separate `_ideation_state_block`) duplicates the `now:` rendering and creates two places to maintain when the snapshot's content evolves.

### What ideation's snapshot looks like after this change

```
## Current state (rendered just before this prompt was sent)
- now: 2026-05-04T22:45:30Z
```

That's it. ~1KB → ~150 bytes. The agent gets its clock and proceeds to read TASKS.md, progress.md, etc., per the body's read-order.

### What status-report's snapshot continues to look like

Unchanged — the cron path doesn't pass the new kwargs, so defaults apply:

```
## Current state (rendered just before this prompt was sent)
- now: 2026-05-04T22:45:30Z
- board: 0A / 1R / 4B / 0P / 40C / 3F (Active/Ready/Backlog/Pipeline-Pending/Complete/Frozen)
- recent commits (HEAD~10):
  7131e71 TB-166: persist control-agent token usage + stream/messages dumps
  ...
```

### Backwards compatibility

- `_current_state_block` and `build_control_prompt` signatures take new keyword args with default-True values. Existing callers (`daemon.run_cron`, `status_report.run_status_report`) call by positional/old-kwarg shape, get the unchanged rendering.
- Ideation is the only caller that opts out.

### Interaction with `state_extras` (TB-151)

`state_extras` continues to render below the (possibly-suppressed) board + commits sub-blocks. When all three (board, commits, extras) are suppressed, only `now:` remains under the header. When all are present, ordering is unchanged from today.

## Verification

- `uv run pytest -q ap2/tests/` — full regression gate passes.
- `grep -nE "include_board|include_commits" ap2/prompts.py` — kwargs are wired in `_current_state_block` AND `build_control_prompt`.
- `grep -nE "include_board=False" ap2/ideation.py` — ideation opts out of the board sub-block.
- `grep -nE "include_commits=False" ap2/ideation.py` — ideation opts out of the commits sub-block.
- prose: a test in `test_prompts.py` calls `_current_state_block(cfg, include_board=False, include_commits=False)` (against a fixture project with at least one task and at least two git commits) and asserts the returned string (a) contains the `now:` line, (b) does NOT contain the literal `board:` substring, (c) does NOT contain any `recent commits` heading or commit short-sha pattern, (d) does NOT contain whitespace-only orphan blocks where the suppressed sections would have been.
- prose: a test in `test_prompts.py` calls `_current_state_block(cfg)` (default kwargs) against the same fixture and asserts the rendering is byte-identical to the pre-change behavior — pin the unchanged shape for status-report.
- prose: a test in `test_prompts.py` exercises `build_control_prompt(cfg, "ideation", load_prompt(cfg), include_board=False, include_commits=False)` and asserts the assembled prompt's `## Current state` block contains `now:` but neither `board:` nor `recent commits`. The rest of the prompt (`_CONTROL_HEADER`, body, `## Guidance`, `_events_block`) is unchanged.
- prose: an integration-flavored test in `test_ideation*.py` invokes `_maybe_ideate` with a stubbed SDK that captures the prompt sent — assert the captured prompt matches the trimmed shape (no `board:` line, no commit short-shas).

## Out of scope

- Trimming the events block (`_events_block` filtering by event-type for ideation specifically). Separate concern, separate TB if pursued.
- Removing the CLAUDE.md read instruction from ideation's read-order. Separate concern.
- Changing `progress.md`'s shape to better serve as the "what shipped" surface ideation reads (e.g. richer per-task summary). Out of scope; the existing format is already sufficient.
- Adding more granular kwargs (e.g. suppress only `state:` commits vs all commits). Boolean is enough; a future task can add filtering if signal density on the commits sub-block becomes a concern for status-report.
- Removing `_current_state_block` entirely from ideation (i.e. dropping `now:` too). The agent has no other clock; this would force timestamp hallucination or empty-string in `ideation_state.md`'s `_Last updated:` field.
- Per-event-kind extras for the snapshot block (e.g. inject "last 3 ideation cycles' verdicts" inline). The state_extras mechanism already supports this if needed; no change required here.

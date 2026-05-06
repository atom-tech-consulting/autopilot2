# Fix mixed-blocker pending-review surfacing — `@blocked:review,TB-N` tasks are invisible

## Goal

This task is anchored in goal.md's `## Done when` criterion: "An operator can point ap2 at a fresh project, paste a goal.md (with Mission + `## Done when`), and walk away for a week without intervention." Pending-review surfacing is the operator's primary "what needs my approval" signal — both `ap2 status`'s `review:` line and the web's "pending review" pill exist precisely so an operator returning after time away can scan and see what awaits their decision. A bug that hides a task from both surfaces while the task is genuinely awaiting operator approval directly breaks the walk-away promise: the task sits forever in Backlog, the operator never notices, the daemon never auto-dispatches because the review token remains unsatisfied.

The bug shape: a task with mixed blockers (e.g., `@blocked:review,TB-N`) is excluded from the pending-review view by the strict `all(b.lower() == "review" for b in t.blocked_on)` filter at three call sites — `ap2/cli.py:138`, `ap2/status_report.py:92`, and `ap2/web.py:925`. The `all()` quantifier requires every blocker to be the literal string "review"; a single non-review blocker (TB-N or any other scheme) makes the filter return False even though `review` IS one of the blockers. Net effect: the task needs operator approval to clear `review`, but the operator never sees that it does because no surfacing path classifies it as pending-review.

The intent of the original `all()` was preserve-strict-semantics — the docstring at `ap2/web.py:916-921` notes "`review` token MIXED with other blockers (those are gated on the [other blockers])." But the surfacing question is orthogonal to the dispatch question: the task IS pending operator review (regardless of what else gates dispatch), and if the operator approves, the existing `_approve_review_token` helper strips just the `review` token, leaving any other blockers to gate auto-promotion naturally afterward. The OPERATOR-FACING SURFACE should therefore use `any(... == "review")` semantics: "this task wants my approval among other things."

Why now: bug just observed in this session. The fix is a one-character per call site change (`all` → `any`) plus regression tests. While the bug doesn't bite often today (most ideation-proposed tasks land with `@blocked:review` alone, no other blocker), the moment ideation proposes a task that depends on another in-flight TB AND needs review, the operator loses visibility. The fix is cheap, the failure mode is a strict break in the walk-away promise, and the existing single-blocker case is unchanged.

## Scope

- `ap2/web.py::_is_pending_review` (line 913-925) — change the return-line quantifier from `all` to `any`. Update the docstring to reflect the new semantics: "True iff `review` is AMONG `t`'s structural blockers — covers both the typical pure-`@blocked:review` case AND mixed-blocker cases where the operator's approval still needs to be captured even if other dependencies remain."
- `ap2/cli.py::cmd_status` (line 138) — change the equivalent `all(b.lower() == "review" for b in t.blocked_on)` filter to `any(...)`. The list comprehension building `pending_review_ids` now includes mixed-blocker tasks.
- `ap2/status_report.py::_pending_review_ids` (line 92) — same change. The cron-driven status post's pending-review surfacing now matches the CLI.
- `ap2/tests/test_cli.py`, `ap2/tests/test_web.py`, `ap2/tests/test_status_report.py` — extend each existing pending-review test fixture with a mixed-blocker case (`@blocked:review,TB-X`); assert the task is now counted/surfaced. Add a regression-pin for the pure-`@blocked:review` and pure-`@blocked:TB-X` cases (unchanged behavior — both still classify correctly: pure-review = pending-review, pure-TB-X = NOT pending-review).

## Design

### What "pending review" means after the fix

**A task is pending review iff `review` appears among its `@blocked:` codespan tokens.** Whether other blockers also exist is irrelevant to the surfacing question — the operator's approve action is still meaningful (strips the `review` token, leaving other blockers to gate auto-dispatch).

| Blockers | Pre-fix `_is_pending_review` | Post-fix `_is_pending_review` |
|---|---|---|
| `[]` (no blockers) | False | False (unchanged) |
| `["review"]` | True | True (unchanged) |
| `["TB-5"]` | False | False (unchanged) |
| `["review", "TB-5"]` | **False (BUG)** | True (FIXED) |
| `["TB-5", "TB-7"]` | False | False (unchanged) |
| `["review", "review"]` (degenerate) | True | True (unchanged) |

### Dispatch behavior is unchanged

The fix only touches surfacing — the strict `_is_dispatchable` check in `ap2/board.py:362-364` continues to require ALL blockers satisfied for auto-promotion. A `@blocked:review,TB-5` task post-fix:
1. Operator runs `ap2 approve TB-N` → strips `review` token (existing behavior via `_approve_review_token`)
2. Task's blockers now `["TB-5"]` — still not dispatchable until TB-5 completes
3. When TB-5 completes, `_is_blocker_satisfied("TB-5", completed)` returns True → task auto-promotes naturally

The fix bridges the surfacing gap without touching dispatch semantics.

### Why three call sites, not one shared helper

Each site has slightly different surrounding context: cli.py builds an `ids` list for both text rendering and JSON output; status_report.py builds the same list for the prompt-side `state_extras` injection; web.py uses it as both a filter predicate AND a per-task pill-render predicate. They could be unified to a single helper, but the boilerplate-vs-readability tradeoff isn't a clear win, and the bug's surface area is small enough that three targeted swaps stay clearer than a refactor. If a future TB needs to extend the predicate further (e.g., "include `review` and `unblock-pending` schemes both"), unification can land then.

### Backwards compatibility

- Pure-review tasks: classified identically pre/post-fix.
- Pure-non-review tasks: classified identically pre/post-fix.
- Mixed-blocker tasks: previously hidden; now surfaced.
- No change to `_approve_review_token`, no change to `_is_dispatchable`, no change to event vocabulary, no change to operator_log.md audit lines.

## Verification

- `uv run pytest -q ap2/tests/` — full regression gate passes.
- `grep -nE "all\(.*lower\(\).*review.*for.*blocked_on" ap2/cli.py ap2/status_report.py ap2/web.py` — should return ZERO matches (validates the buggy pattern is gone from all three sites).
- `grep -nE "any\(.*lower\(\).*review.*for.*blocked_on" ap2/cli.py ap2/status_report.py ap2/web.py` — at least 3 matches (the fix is applied at all three sites).
- prose: a test in `test_web.py` synthesizes a fixture board with three Backlog tasks: TB-A `@blocked:review`, TB-B `@blocked:review,TB-X`, TB-C `@blocked:TB-X`. Calls `_is_pending_review` for each. Asserts (a) TB-A returns True, (b) TB-B returns True (the new behavior — was False pre-fix), (c) TB-C returns False.
- prose: a test in `test_cli.py` exercises `cmd_status` against the same fixture and asserts the rendered text output's `review:` line lists BOTH TB-A and TB-B (in source order or whatever the existing sort is) — pre-fix it would only list TB-A.
- prose: a test in `test_status_report.py` (or the equivalent module) exercises `_pending_review_ids` against the same fixture; asserts the returned list contains both TB-A and TB-B.
- prose: a test pins the dispatch-gate independence — synthesize a board with TB-B `@blocked:review,TB-X` (where TB-X is in Backlog, not Complete). Run the auto-promote sweep. Assert TB-B is NOT auto-promoted (still gated on TB-X) even though `_is_pending_review` returns True for it. The fix is surfacing-only; dispatch semantics are unchanged.

## Out of scope

- Changing the rendering of other blocker types (e.g., showing a "blocked on TB-X" pill on the web). The `@blocked:<csv>` codespan is already visible on the rendered task line; an explicit per-blocker pill can come later if useful.
- Refactoring the three pending-review call sites into a shared helper. v1 is targeted swaps; refactor when a fourth call site or a more complex predicate justifies it.
- Renaming the "pending review" pill or the `review` blocker token.
- Changing `_approve_review_token` to handle mixed-blocker cases differently. It already strips just the `review` token; that behavior is correct.
- Auto-displaying the OTHER blockers on the pending-review surface (e.g., "TB-B (also gated on TB-X)"). The codespan is visible on the task line; surfacing the pending-review status is the load-bearing fix.
- A new event type to mark mixed-blocker tasks. The `@blocked:` codespan IS the source of truth; no new event needed.
- Web-side filtering controls that toggle between strict and loose pending-review semantics. The loose semantic is the right default; toggle-UI is unnecessary complexity.
## Attempts

### 2026-05-06 — verification_failed
(no summary)
- **kind:** per_task
- **failed_criteria:** [fail] `grep -nE "all\(.*lower\(\).*review.*for.*blocked_on" ap2/cli.py ap2/status_report.py ap2/web.py` — should return ZERO m
- **Debug dumps:** `prompt: .cc-autopilot/debug/20260506T181654Z-TB-187.prompt.md`, `stream: .cc-autopilot/debug/20260506T181654Z-TB-187.stream.jsonl`, `messages: .cc-autopilot/debug/20260506T181654Z-TB-187.messages.jsonl`

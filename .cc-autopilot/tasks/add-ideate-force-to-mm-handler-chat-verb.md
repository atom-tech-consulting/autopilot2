# Add `ideate [force]` to MM handler chat-verb list (parity with `ap2 ideate` CLI)

## Goal

This task is anchored in goal.md's `## Done when` criterion: "An operator can point ap2 at a fresh project, paste a goal.md (with Mission + `## Done when`), and walk away for a week without intervention." The walk-away promise depends on the chat surface (Mattermost, mobile-friendly) carrying parity with the CLI surface for operator-driven verbs — without that, an operator away from a terminal loses control of routine board / lifecycle operations.

TB-159 added `ap2 ideate [--force]` (CLI) — manual ideation trigger that bypasses the cooldown / `AP2_IDEATION_DISABLED` / non-empty-Ready-or-Backlog gates. The queue infrastructure (`OPERATOR_QUEUE_OPS` in `ap2/tools.py:1259`, `operator_queue_append` MCP tool in `MM_HANDLER_TOOLS`) is already in place to route chat-triggered ideate ops — but the MM handler's prompt verb list (`ap2/prompts.py:464-477`) enumerates only `add_*` / `move_to_backlog` / `unfreeze` / `delete` / `approve` / `reject`. `ideate` isn't there, so the handler doesn't know it can route an "@claude-bot ideate" mention through `operator_queue_append({"op": "ideate", ...})`.

This task closes the parity gap by adding `ideate [force]` to the MM handler's documented verb list. No daemon changes — the queue-drain already handles `op="ideate"`. Pure prompt edit.

Why now: friction observed mid-session — operator wanted to trigger ideation from chat, no path exists. TB-159's Out-of-scope explicitly deferred the MM handler addition with "Defer until friction observed; CLI is enough surface for v1." That deferral has now resolved into observed friction; closing the gap completes TB-159's coverage.

## Scope

- `ap2/prompts.py` — extend the MM handler's queue-ops enumeration in `MM_HANDLER_PROMPT` to include `ideate [force]`. Match the existing inline-shape used for `reject TB-N [reason: ...]` (TB-152): name the verb, link to TB-159, give the chat-trigger phrasing the agent should accept ("@claude-bot ideate" / "@claude-bot ideate force"), describe what it does (manual trigger, bypasses cooldown / disabled / queue-depth gates, refuses if a task is Active unless `force`), and route via `operator_queue_append({"op": "ideate", "force": <bool>})`.
- `ap2/tests/test_prompts.py` — pin that the MM handler prompt body contains the `ideate` verb description.
- `ap2/tests/test_mattermost*.py` (or wherever the handler test harness lives) — pin that an "@claude-bot ideate" mention routes through to a queued `ideate` op (with `force=False` by default; `force=True` when the operator says "ideate force" / "ideate --force" / similar).

## Design

### Verb shape in chat

Mirror the existing shape used by other MM verbs. The handler accepts:

- `@claude-bot ideate` → `operator_queue_append({"op": "ideate", "force": false})`
- `@claude-bot ideate force` → `operator_queue_append({"op": "ideate", "force": true})`

The `force` flag bypasses the Active-task refusal (same semantics as the CLI's `--force`). Default false matches the CLI default.

### Why no separate MCP tool

The queue path is the canonical board / lifecycle mutation channel for chat (TB-142 dropped `board_edit` from `MM_HANDLER_TOOLS`; ops route through the queue). `ideate` is already a registered op (`OPERATOR_QUEUE_OPS` in `ap2/tools.py:1259`) and the drain handler already knows what to do with it. No new tool surface needed.

### Refusal path

When a task is currently Active (board has 1+ tasks in `Active`), the daemon's drain handler refuses the `ideate` op without `force=true` — same semantic as the CLI. The MM handler should pre-flight check the board state if possible, OR rely on the drain's refusal (which the handler can see via the `operator_queue_append` return value or the next-tick `operator_queue_drained` event). Either is fine; the prompt should NOT teach the handler to lie about "I'll trigger ideation now" when the gate would refuse — explicit error feedback to the operator is better.

### Backwards compatibility

Adding a new verb is purely additive. Existing chat verbs unchanged. CLI's `ap2 ideate` unchanged. Queue drain unchanged.

## Verification

- `uv run pytest -q ap2/tests/` — full regression gate passes.
- `grep -nE "ideate" ap2/prompts.py` — verb appears in MM handler prompt body (currently 0 occurrences; should become ≥1 after this task).
- `python3 -c "from ap2.tools import OPERATOR_QUEUE_OPS; assert 'ideate' in OPERATOR_QUEUE_OPS"` — queue op registration unchanged (regression check).
- prose: a test in `test_prompts.py` (or `test_mattermost*.py`) loads the MM handler prompt via `build_mm_handler_prompt` (or whichever public entry point assembles it) and asserts the rendered string contains `ideate` AND the literal phrase or close paraphrase "manual ideation trigger" (or similar verb description; pin one specific marker so the test catches accidental removal).
- prose: a test pins the chat-routing — synthesize an "@claude-bot ideate" mention through the MM handler test harness with a stubbed SDK that captures the `operator_queue_append` calls; assert one call lands with `op="ideate"` AND `force=False`. A second test exercises "@claude-bot ideate force" (or the syntactic variant the prompt teaches) and asserts `force=True`.

## Out of scope

- Adding `ideate` semantics to any other agent kind (ideation, cron). Operator-routed only — same as the CLI.
- Adding a `--reason` flag to `ideate` (chat or CLI). The verb is operator-on-demand; the rationale is implicit ("operator wanted ideation now") and the next ideation cycle's run-usage event captures the trigger context.
- Pre-flight refusing when Active is non-empty inside the MM handler itself. The drain's refusal is sufficient; teaching the handler to second-guess board state risks staleness.
- Web UI button for manual ideation trigger. Out of band; chat + CLI is enough surface.
- Renaming the `ideate` op or changing the underlying daemon path. Pure prompt extension.

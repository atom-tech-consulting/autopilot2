# Fix stale `ideation_state.md` references in cron status report (drop "Tasks awaiting review" redundancy + teach the agent to validate forwarded references)

## Goal

This task is anchored in goal.md's `## Done when` criterion: "An operator can point ap2 at a fresh project, paste a goal.md (with Mission + `## Done when`), and walk away for a week without intervention." The 6-hourly Mattermost status post is one of the operator's primary at-a-distance walk-away surfaces — it's the message that lands in the operator's chat client while they're not looking at the terminal, and it's supposed to give them an accurate "is anything off?" signal. Today's post forwards the `## Open questions for operator` section from `ideation_state.md` verbatim, which means up to 2 hours of staleness (the gap between ideation cycles) bleeds into a surface the operator trusts as current.

Two distinct staleness shapes are visible in real posts today:

1. **Redundant "Tasks awaiting review" line** — ideation writes `**Tasks awaiting review**: TB-X, TB-Y` into its open-questions section. The status-report routine ALSO surfaces a separate `Pending operator review (N): TB-X, TB-Y` line that's freshly re-derived from current board state per status-report run (TB-151). If anything got approved or rejected in the gap between ideation cycles, the two lines actively contradict each other in the same post.
2. **Stale per-TB-N references by mid-flight status** — entries like `**TB-181 retry watch (n=1 prose-bullet over-specification)**` are the ideator's snapshot of TB-181's retry state at the last ideation cycle; if TB-181 has since landed, hit retry-exhausted, been edited, or been deleted, the reference is wrong. The agent assembling the status post has events.jsonl + TASKS.md in its context but currently forwards the line verbatim without cross-checking.

This task closes both gaps with two coordinated prompt edits — no code-side mechanical-refresh logic, no schema changes, no event-shape changes. Pure prompt-iteration work, in line with goal.md's "Current focus: ideation quality."

Why now: the redundancy + per-TB staleness was just observed firsthand inspecting today's session's `ap2 status` output (which surfaces the same lines via the same TB-173 path). Filing now lands the fix before the next batch of long-running tasks lands and amplifies the staleness — and matches the operator's repeated preference for prompt-iteration trims as the load-bearing ideation-quality work.

## Scope

- `ap2/ideation.default.md` — update the `## Open questions for operator` schema fragment (around line 73) to instruct ideation explicitly: do NOT include any "Tasks awaiting review" / "TB-N awaiting approval" bullets. That information is mechanically surfaced separately by `ap2 status` and the cron status-report (per TB-151 / TB-173); duplicating it in the open-questions list creates contradiction risk when the gap between ideation cycles diverges from current board state. Open-questions content stays focused on items that REQUIRE narrative judgment — focus-rotation candidates, retry-watch interpretations, residual-risk acceptances, escalations.
- `ap2/status_report.py` (or `ap2/prompts.py` if the status-report prompt body lives there) — extend `STATUS_REPORT_PROMPT` (or the equivalent prompt-body constant) with a validation instruction: before including any TB-N reference forwarded from `ideation_state.md`'s open-questions list into the Mattermost post, the agent MUST check the recent events tail for a `task_complete`, `task_deleted`, `task_updated`, or `verification_failed` event for that TB-N landing AFTER the `ideation_state_updated` event's timestamp. If found, the agent either (a) skips the bullet entirely (preferred when the bullet is now obsolete) OR (b) rewords it to reflect the current state with a parenthetical noting the bullet was based on stale ideation_state.md content. The agent already has both events.jsonl and the file in context; this is purely a reasoning-step prompt addition.
- `ap2/tests/test_ideation_defaults.py` (or similar) — regression test pinning that `ap2/ideation.default.md` does NOT contain instructions to write "Tasks awaiting review" into the open-questions section; positive test pinning the new instruction.
- `ap2/tests/test_status_report.py` (or where status-report prompt is tested) — regression test pinning that the status-report prompt body contains the validation instruction (greppable phrase like "validate against events" / "check for task_complete since ideation_state_updated").

## Design

### Why prompt-only, not code-side mechanical refresh

Option C from the design discussion (read-time mechanical refresh in `status_report.py` to substitute fresh data into stale ideation_state.md content) was deliberately rejected. Reasons:

- The status-report agent already reads events.jsonl + ideation_state.md in the same invocation. Cross-referencing is free reasoning work; pre-computing it in Python duplicates the agent's context-walking.
- Mechanical refresh requires hardcoding patterns to recognize ("if the bullet contains `TB-N awaiting`, do X"). Brittle; misses novel staleness shapes the LLM can catch via narrative reasoning.
- Prompt-only changes are smaller diffs, easier to roll back if they regress, and don't add a new code surface that needs to keep up with future ideation_state.md schema changes.

### What stays in `## Open questions for operator` after this task

Useful narrative bullets that ideation is uniquely positioned to surface:

- Focus-rotation candidates ("after TB-174/TB-175 land + approve, consider rotating focus to X")
- Retry-watch interpretations ("TB-181 retry watch (n=1 prose-bullet over-specification): the failure shape suggests Y")
- Residual-risk acceptances ("Shell-bullet residual-risk acceptance: TB-172 reject implies Z")
- Cross-TB pattern observations ("Three of the last five rejections cite scope-creep — consider tightening the briefing-template's scope guidance")
- Goal-relevance escalations ("`.cc-autopilot/insights/_index.md` still empty; first insight bootstrap is overdue")

What gets DROPPED (and is now surfaced by the mechanical pending-review line instead):

- "**Tasks awaiting review**: TB-X, TB-Y" — duplicates `Pending operator review (N): TB-X, TB-Y` line
- "TB-Z waiting for `ap2 approve` since <date>" — same redundancy

### The validation rule, phrased for the prompt

Concrete prompt-side language to add (final wording is the implementer's call; this is the intent):

> When forwarding bullets from `ideation_state.md`'s `## Open questions for operator` section into the Mattermost post:
> 1. Note the `ts` of the most recent `ideation_state_updated` event in `events.jsonl`. This is when the open-questions content was last refreshed.
> 2. For every TB-N reference in a bullet, scan events.jsonl for any `task_complete`, `task_deleted`, `task_updated`, or `verification_failed` event for that TB-N with `ts` AFTER the `ideation_state_updated` ts.
> 3. If found, the bullet is stale: either skip it (when the bullet's premise no longer holds — e.g., a "TB-N retry watch" bullet for a TB-N that has now landed Complete) OR rewrite it with a parenthetical noting the staleness ("(per stale ideation_state.md; TB-N landed Complete at <ts>)").
> 4. If not found, the bullet's TB-N references are still current; forward as-is.

### Costs

This adds ~10-30 tokens of prompt body + a small additional reasoning step per status-report run. Status-report cycles cost ~$0.05-0.20 today (per the recent control_run_usage events); validation reasoning adds maybe $0.01-0.03 per run. Negligible.

The redundant-line drop SAVES tokens (smaller ideation_state.md → smaller prompt for both ideation and status-report; smaller MM post).

### Backwards compatibility

Existing `ideation_state.md` files written by the pre-this-task ideation prompt may contain "Tasks awaiting review" bullets. The status-report agent's new validation rule will cross-check these against events; obsolete bullets will be skipped. Within ~2-4 hours of this task landing, the next ideation cycle will rewrite ideation_state.md without the redundant content. No backfill needed.

## Verification

- `uv run pytest -q ap2/tests/` — full regression gate passes.
- `grep -nE "tasks awaiting review|TB-N awaiting" ap2/ideation.default.md` — should return ZERO matches inside the `## Open questions for operator` schema fragment (validates the redundancy is dropped).
- `grep -nE "validate.*events|task_complete.*since|ideation_state_updated" ap2/status_report.py ap2/prompts.py` — at least one match in the status-report prompt body confirming the validation instruction is wired.
- prose: a regression test in `test_ideation_defaults.py` (or similar) loads `ap2/ideation.default.md` and asserts the section between `## Open questions for operator` and the next `##` heading does NOT contain the literal substring "Tasks awaiting review" (case-insensitive). Pin both the absence-of-old-instruction AND the presence of the new prohibition (e.g., a phrase like "do NOT include tasks-awaiting-review bullets").
- prose: a test in `test_status_report.py` (or wherever the status-report prompt is asserted) loads the status-report prompt body and asserts it contains the validation phrase — concrete grep target like "task_complete" + "ideation_state_updated" or "validate against events.jsonl". Pin one specific marker so the test catches accidental removal.
- prose: a smoke test exercises the status-report agent against a fixture: synthetic `ideation_state.md` containing one open-questions bullet referencing `TB-X retry watch` AND a synthetic events.jsonl containing a `task_complete TB-X status=complete` event AFTER the `ideation_state_updated` ts. Stub the SDK to capture the prompt sent; assert the agent receives both fixtures AND the validation instruction. (Cannot easily pin the agent's actual reasoning without an integration test; the prompt-content + event-presence checks are the load-bearing pin.)
- prose: a test pins the no-staleness-detected case — fixture with one open-questions bullet referencing `TB-Y` AND no `task_complete TB-Y` after the `ideation_state_updated` ts; assert the prompt is structured such that the agent is expected to forward the bullet unchanged (validation instruction's "if not found, forward as-is" branch).

## Out of scope

- Read-time mechanical refresh in `status_report.py` (option C from the design discussion). Prompt-only is sufficient; code-side substitution adds complexity without proportional benefit.
- Restructuring `ideation_state.md`'s schema beyond the one-line redundancy drop. The other sections (`## Mission alignment`, `## Current focus assessment`, etc.) keep their current shape.
- Backfilling existing `ideation_state.md` files. Next ideation cycle rewrites with the new prompt; no migration step needed.
- Validating non-TB-N references (file paths, free-form prose claims) in open-questions bullets. The TB-N case is the high-value common one; novel reference-shape validation is future work if observed.
- Surfacing the agent's "I skipped this bullet because it was stale" decision back into events.jsonl for audit. The status-report post itself shows what got included; the absence of a bullet is its own audit signal.
- Updating the `ap2 status` CLI to apply the same validation logic. The CLI's open-questions surface (TB-173) reads from `ideation_state.md` directly without an LLM step; if CLI freshness becomes a complaint, that's a separate TB (read-time mechanical refresh in cli.py — option C scoped to the CLI surface only).
- Web home page's open-questions surface (also TB-173). Same scope split as the CLI — separate concern.
- Adding a new event type for "ideation bullet superseded." The skip-during-forward path is sufficient.

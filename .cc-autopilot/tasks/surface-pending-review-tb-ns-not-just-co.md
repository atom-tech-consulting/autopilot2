# TB-151 — Surface pending-review TB-Ns in `ap2 status` and the cron status-report

## Why
TB-121 added the review gate: ideation proposals land with
`@blocked:review` and the operator runs `ap2 approve TB-N` to
dispatch. `ap2 status` (cli.py:186-194) reports a count
("review: 3 ideation proposals pending") but does not list the
TB-Ns — operators have to open TASKS.md and scan for
`@blocked:review` codespans to find which IDs to approve.

The cron `status-report` routine (`ap2/status_report.py`) does not
mention pending-review at all (`grep "review" ap2/status_report.py`
→ 0 hits at the time of authoring). Mattermost reports therefore
omit the most actionable signal: "operator has N proposals waiting
on a verb."

This is the natural follow-up to TB-121 + TB-144 — both surfaces
already know the count; both should also list the IDs.

## Scope
1. `ap2/cli.py` `cmd_status`:
   - Collect the list of pending-review TB-Ns (not just the count)
     by extending the comprehension at cli.py:133-136 to keep
     `t.id` instead of summing.
   - When `pending_review` is non-empty, render a single line in
     the text branch:
       `review:   3 pending — TB-150, TB-151, TB-152
        (`ap2 approve TB-N`)`
     Truncate to first 5 IDs with "(+N more)" if there are more
     than 5. Keep the count-only behavior in `--json` but add a
     parallel `pending_review_ids: ["TB-150", ...]` field for
     machine consumers.
2. `ap2/status_report.py`:
   - Inject pending-review TB-Ns into the snapshot block the
     routine builds before the SDK call. Add a one-line section
     "Pending operator review (N): TB-150, TB-151, ..." (truncated
     to 5 + "(+M more)" the same way as the CLI). Skip the line
     entirely when N=0.
   - Update the routine's prompt-string to instruct the agent to
     pass the line through verbatim into the posted Mattermost
     report when present.
3. Tests:
   - `ap2/tests/test_cli.py`: new `test_status_lists_pending_review_ids`
     constructs a Board with 3 review-gated tasks, runs `cmd_status`
     with stdout capture, asserts each TB-N appears in the printed
     line; pin the truncation behavior with a 6-task variant
     ("(+1 more)"). Pin the JSON branch carries the same IDs.
   - `ap2/tests/test_status_report.py` (or wherever
     `status_report.py` is tested): new test that the snapshot
     block includes a "Pending operator review" line with the
     IDs listed when N>0, and is absent when N=0.
4. Both rendering paths share a single helper
   (`_format_pending_review_line(ids)`) so the truncation rule
   stays consistent across CLI and cron-report.

## Verification
- `uv run pytest -q ap2/tests/test_cli.py` — passes (existing +
  new tests).
- `uv run pytest -q ap2/tests/` — full regression gate passes.
- `grep -q "_format_pending_review_line" ap2/cli.py` — shared
  helper exists.
- `grep -q "_format_pending_review_line" ap2/status_report.py` —
  helper reused on the cron side.
- `grep -nE "Pending operator review" ap2/status_report.py` — the
  string the routine injects into its snapshot block is present.
- prose: `cmd_status` in `ap2/cli.py` lists the actual TB-Ns
  (not just the count) on the `review:` line of its text output,
  truncating after 5 IDs with a "(+N more)" suffix; the JSON
  branch carries a parallel `pending_review_ids` list.
- prose: `ap2/status_report.py`'s snapshot block contains a
  "Pending operator review (N): TB-..." line whenever N>0, and
  the routine's prompt instructs the agent to forward that line
  to the posted Mattermost report verbatim.

## Out of scope
- Adding an `approve --all-pending-review` bulk verb (separate
  ergonomics task; defer until friction observed).
- Web UI changes (web.py already has the pending-review filter
  per TB-121).
- Auto-approving anything — the gate stays human-in-the-loop.

# Status-report cron digest block — auto-approve / auto-unfreeze loop activity since last report

## Goal

The current focus is `Current focus: end-to-end automation`. The
status-report cron (TB-128 fresh snapshot + TB-144 shared routine)
is the operator's scheduled return surface: it lands in Mattermost
on its schedule and tells the operator "since the last time you
looked, here's what happened." TB-223 / TB-224 / TB-225 just shipped
three axes of opt-in automation — but the status-report contract
doesn't mention auto-approve / auto-unfreeze loop activity at all
today. An operator who walks away for a day, returns to the
Mattermost status post, and sees board counts + recent completes
has no idea how many tasks the auto-approve loop dispatched
unattended, whether the loop is currently paused, or whether the
loop triggered any halts in the digest window.

Why now: walk away for a week without intervention (goal.md L28-29
Done-when bullet 1) is the mission's headline promise, and the
status-report cron is the surface that proves whether the promise
held while the operator was gone. Without an auto-approve digest in
that post, returning operators have to alt-tab to `ap2 status` or
`ap2 logs` to learn whether the loop ran cleanly — the
status-report is the WALK-AWAY summary, so it must summarize
exactly the surfaces the operator gave up touch on.

## Scope

(1) New status-report contract block in
`ap2/status_report.py` (or whichever module currently houses the
TB-144 shared routine): after the existing board-counts + recent-
completes summary, add an `## Automation loop activity` section
when at least one of these is true:
  - `AP2_AUTO_APPROVE=1` in the daemon env, OR
  - any `auto_approved` / `auto_unfreeze_applied` /
    `auto_unfreeze_skipped` / `auto_approve_paused` event fired
    since the previous `cron_complete name=status-report` event.

(2) Section content (rendered as Markdown for the Mattermost post):
  - One-line headline: `auto-approve: <healthy|PAUSED reason=X>;
    auto-unfreeze: <healthy|cooldown>`.
  - Bullet list of counts since-last-report: `- N tasks auto-approved
    (M succeeded, K froze)`, `- L tasks auto-unfrozen (P succeeded,
    Q re-froze)`, `- R briefing-fix shapes auto-applied`,
    `- S auto-unfreeze attempts skipped (reason breakdown)`.
  - When paused, list the most recent halt event with its
    timestamp + reason + the ack verb the operator needs to run.
  - When all counts are zero AND knob is unset, render NOTHING
    (omit the section entirely — no zero-noise on pre-opt-in
    projects).

(3) Reuse TB-227's `collect_auto_approve_state` helper for
state (paused, reason, threshold) so the two surfaces don't drift.
Counts in this section are scoped to the inter-report window
(`since_idx` = idx of the previous `cron_complete name=status-report`
event); helper extends to take a `since_event_idx` kwarg, or a
sibling `collect_window_loop_activity(cfg, since_event_idx)` lives
in the same module — implementation choice belongs to the agent.

(4) `_status_report_should_skip` (per TB-128): the new automation
loop activity counts as "interesting" — emit a non-zero
`auto_approve_paused` / `auto_unfreeze_applied` event in the
window means the status-report cron MUST NOT skip even if board
counts are unchanged. Extend the should-skip gate's
"interesting-event" allowlist accordingly.

(5) Tests in new
`ap2/tests/test_tb228_status_report_automation_digest.py`:
  - Section absent when knob off + all counters zero.
  - Section present when knob on + counters zero (renders "healthy,
    0 since last report").
  - Section present when paused — renders pause reason + ack
    verb.
  - Section present when counters non-zero (knob may be on or off
    historically — handles operator toggling).
  - `_status_report_should_skip` returns False when an
    `auto_approve_paused` event landed in the window, even if
    nothing else interesting happened.

(6) Update the status-report cron prompt's contract (`ap2/cron.default.yaml` + `ap2/prompts.py`
`_STATUS_REPORT_CONTRACT`) to TEACH the cron agent to include the
section verbatim from the daemon-injected snapshot (don't ask the
agent to re-derive — the snapshot block is authoritative per
TB-128's stale-text fix).

## Design

- The Markdown formatting + count aggregation lives in
  `ap2/status_report.py` so the snapshot block injected into the
  cron prompt carries the rendered section verbatim. The agent's
  job is "post the snapshot as-is" (per TB-128 contract), not
  "compute counts" — keeps the post deterministic and avoids the
  TB-128 stale-text regression class.
- Window scoping via `since_idx` is cheaper than wall-clock; the
  `events.jsonl` tail scan already powers TB-227's helper and
  TB-144's shared routine.
- Paused-reason rendering reuses TB-227's `pause_reason`
  derivation — same source of truth, two surfaces.
- The shared toolkit between TB-227 and TB-228 (same helper, same
  reason mapping) is a feature, not duplication; the
  `collect_*` family lives in a single module that both surfaces
  import.

## Verification

- `uv run pytest -q ap2/tests/test_tb228_status_report_automation_digest.py` — new test module exists and all behavioral cases pass.
- `uv run pytest -q ap2/tests/` — full suite green vs current 1421 baseline.
- `test -f ap2/tests/test_tb228_status_report_automation_digest.py` — test module present.
- `grep -nE "Automation loop activity" ap2/status_report.py` — section heading constant present.
- `grep -nE "auto_approve_paused" ap2/status_report.py` — pause-event handling wired.
- `grep -nE "auto_approve_paused|auto_unfreeze_applied" ap2/daemon.py ap2/status_report.py` — should-skip gate's interesting-event allowlist references at least one of the new event types.
- Prose: the status-report cron prompt contract (`_STATUS_REPORT_CONTRACT` in `ap2/prompts.py` and the matching prompt body in `ap2/cron.default.yaml`) teaches the agent to render the daemon-injected snapshot verbatim including the Automation loop activity section; judge confirms via Read of both files.
- Prose: the section is omitted entirely when `AP2_AUTO_APPROVE` is unset AND all four event-type counters in the window are zero; judge confirms by reading the matching test in `test_tb228_status_report_automation_digest.py`.

## Out of scope

- `ap2 status` text/JSON surface — TB-227 owns that.
- A standalone digest cron separate from the status-report cron.
  This task piggybacks on the existing scheduled post; no new
  cron job.
- Mattermost-side rich formatting (attachments, color blocks).
  Keep the post pure Markdown so a sandbox without
  Mattermost-attachment APIs still renders cleanly.
- Operator-tunable digest cadence. The existing status-report
  cron schedule is the cadence.

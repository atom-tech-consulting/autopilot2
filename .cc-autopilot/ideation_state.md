# Ideation State

_Last updated: 2026-05-02T06:44:00Z by ideation cron_

## Mission alignment
Inferred from CLAUDE.md + progress.md (goal.md is still placeholders): ap2
is an autonomous-coding autopilot — Claude SDK daemon proposes work,
operator-gates dispatch, per-task agents implement, verifier judges, all
surfaced through CLI/web/Mattermost. Recent 5 completes all serve this
mission: TB-149 (Mattermost thread context for the chat handler),
TB-121 (review gate seeded e2e — completes the ideation→approval path),
TB-148 (web tints distinguish verification_failed/retry_exhausted from
clean), TB-147 (verifier runs bullets via /bin/bash for `[[ ]]` /
process-substitution), TB-144 (status-report hoisted to a shared
routine + MCP tool). No drift detected.

## Current focus assessment
goal.md has no explicit "Current focus" — inferring from the dominant
themes in the last ~25 completes:

- **Review gate + dispatch safety**
  - Progress so far: TB-121 lands the gate (every ideation add carries
    `@blocked:review`, `ap2 approve` strips it); TB-138 forces every
    Verification bullet to be auto-verifiable (no `Manual:`); TB-142
    routes MM-handler approves through the operator queue; TB-145
    collapses MM_HANDLER_TOOLS to a single restricted set; TB-146
    removes `cron_edit` from every agent toolset.
  - Gaps: `ap2 status` shows a count of pending-review tasks but not
    their TB-Ns (cli.py:186-194); the cron status_report routine
    doesn't surface pending-review at all (status_report.py grep for
    "review" → 0 hits) — operators have to grep TASKS.md to find
    which IDs to approve. There's no `reject` verb: an operator who
    decides against an ideation proposal must `ap2 delete TB-N`,
    which doesn't log intent into operator_log.md, so future
    ideation cycles can re-propose the same idea.
  - Status: `in-progress`
  - Reasoning: gate is shipped and tested; the missing pieces are
    surface ergonomics around the queue it produced.

- **Verifier robustness**
  - Progress so far: TB-127 (resolve task→commit→diff), TB-136
    (cumulative diff across retries + Read/Glob/Grep for the prose
    judge), TB-137 (max_turns 8→20), TB-147 (run bullets via
    /bin/bash).
  - Gaps: briefings still ship with TB-76-class shell pitfalls (bare
    `python`, bare path-as-command, multi-line bullets). `ap2 check`
    lints `Manual:` (check.py:140-178, TB-138) but not these shell
    shapes — the verifier discovers them at runtime via `exit 127`,
    forcing edit-briefing retries.
  - Status: `in-progress`
  - Reasoning: judge-side hardening is solid; author-side lint to
    catch known footguns pre-approval is the natural next step.

- **Observability / operator UX**
  - Progress so far: TB-128 (status-report freshness contract),
    TB-129 (live /task-run/<id> page), TB-130 (web auto-starts with
    daemon), TB-148 (status-tinted task_complete rows), TB-144
    (status-report routine + MCP tool), TB-149 (MM thread-read tool).
  - Gaps: pending-review surfacing inside the cron status post and
    inside `ap2 status` (overlap with the gate gap above — same
    underlying signal).
  - Status: `in-progress`
  - Reasoning: web + cron coverage are mature; the unresolved gap is
    the review-queue's discoverability outside TASKS.md grep.

## Non-goal risk check
None. goal.md has no Non-goals declared and nothing in flight is
straying from the inferred mission.

## Considered & deferred this cycle
- **Web `/pending-review` view**: already covered — web.py has
  `_is_pending_review` (line 519) plus a tag-pill renderer + section
  filter (line 678 "pending review (N)"). No additional work needed.
- **Bulk `approve TB-N TB-M ...` from chat**: nice ergonomics but not
  a missing capability — operator can repeat the verb. Defer until
  someone hits the friction.
- **Insight-file bootstrap**: `_index.md` is empty. Per ideation
  prompt rules, only propose `#evaluation` tasks reactively when a
  ranking gap genuinely needs grounding. None of this cycle's
  proposals do.
- **Daemon-downtime watchdog upgrade**: 2026-05-01 had a ~10h gap
  (daemon_stop@19:23Z → daemon_start@05:33Z); auto_diagnose fired
  twice (12:07Z 3h, 18:07Z 5h) into Mattermost. The watchdog is
  already doing its job — no software gap surfaced.

## Open questions for operator
- goal.md is still all placeholders — once the project mission is
  written down, ideation can rank against it instead of inferring
  from progress.md every cycle.
- After this cycle lands TB-150/TB-151/TB-152 to Backlog, all three
  will sit `@blocked:review`; review with `ap2 approve TB-N` to
  dispatch.
- No unadopted `cron_proposed` events in the recent-events block —
  nothing to surface for `ap2 cron edit`.

## Proposals this cycle
- TB-150: `ap2 check`: lint TB-76 shell pitfalls in `## Verification`
  bullets (bare `python`, path-as-command, multi-line) — closes the
  verifier-robustness author-side gap.
- TB-151: surface pending-review TB-Ns (not just count) in
  `ap2 status` and the cron status-report — closes the review-gate
  visibility gap.
- TB-152: `ap2 reject TB-N` (CLI + chat) — logs intent to
  operator_log.md and removes the task, so ideation respects the
  decision next cycle.

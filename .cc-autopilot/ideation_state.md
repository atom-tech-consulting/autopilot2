# Ideation State

_Last updated: 2026-05-04T23:55:00Z by ideation cron_

## Mission alignment

ap2's mission (goal.md): autonomous dev loop driving a target project to its
declared `## Done when` with minimal operator intervention. Recent 5 completes
all serve the meta-mission of making that loop trustworthy:

- TB-169 (0d4fd53) — trim ideation's `_events_block` to a curated allowlist
  (filter judge_call/cron_complete out of the ideation prompt; keeps token
  cost low + signal high).
- TB-168 (c113f4c) — trim ideation's `_current_state_block` (drop board
  counts + recent commits; keep only `now:`); pairs with TB-169.
- TB-164 (2ce0b9f) — briefing validator now requires non-empty "Why now"
  rationale within `## Goal` (closes goal.md's delete-test gap mechanically).
- TB-163 (aa86c18) — ideation prompt header now renders "Recent operator
  rejections" (last K) — pattern-level reject signal reaches ideator.
- TB-161 (35364bd) — briefing validator requires `## Goal` body to cite a
  goal.md focus item or `## Done when` bullet (closes ap2-meta-polish drift).

No drift. Each ships a visible step toward operator-walks-away reliability.

## Current focus assessment

goal.md "Current focus: ideation quality" is the sole declared focus.

- **Ideation quality (gap-covering without drift; push for progress without
  scope creep)**
  - Progress so far: every gap from last cycle's assessment has shipped.
    TB-161 added the goal-anchor check to `_validate_briefing_structure`;
    TB-164 added the line-anchored "Why now" delete-test check; TB-163
    rendered "Recent operator rejections" in `build_control_prompt`;
    TB-168/TB-169 trimmed ideation's snapshot + events blocks so the agent
    operates on signal-dense context. Earlier scaffolding (TB-121 review
    gate, TB-138 auto-verifiable-bullet rule pinned in
    `ap2/ideation.default.md`, TB-154 canonical-section validator,
    TB-152 reject-reason capture) all still in place.
  - Gaps:
    (1) TB-138's "auto-verifiable bullets only — no `Manual:` bullets" rule
    is enforced by the briefing prompt + `ap2 check` warning
    (`_check_briefings_manual_bullets` in `ap2/check.py:152`) but NOT by
    `_validate_briefing_structure` (`ap2/tools.py:345`) at queue-append
    time. A briefing whose `## Verification` carries `- Manual: operator
    runs X` still passes the gate today — the same failure mode TB-122 hit
    (3 retries × 1 manual bullet → retry_exhausted) is one ideation
    hallucination away from re-occurring.
    (2) TB-156 / TB-165 / TB-166 each had to ship a follow-up commit
    fixing an identical shell-bullet typo (`grep -nE` on a directory arg
    instead of `grep -rnE`). That's 3 same-shape verification-fail events
    in 4 days. The TB-76 "Shell-bullet pitfalls to AVOID" section in
    `ap2/ideation.default.md` documents the rule but no validator catches
    it pre-allocation; the verifier only reports failure post-task.
    (3) The ideator's `## Open questions for operator` section in
    `.cc-autopilot/ideation_state.md` is read by no operator-facing
    surface — `ap2 status`, web home, and the cron status-report all
    skip it. Questions surfaced at ideation time sit unread until the
    operator manually reads the file.
  - Status: `in-progress`
  - Reasoning: structural gates + prompt context for goal-relevance and
    scope-creep are now shipped (TB-161/TB-163/TB-164/TB-168/TB-169). The
    next set of gaps is the remaining mechanical guards (Manual: + shell
    pitfalls) plus operator-side surfacing of ideator questions. After
    those, the focus item is plausibly `exhausted-needs-operator`.

## Non-goal risk check

None. Nothing in flight strays into goal.md's Non-goals (generic task
scheduler, multi-tenancy, real-time collab, cross-project orchestration,
operator-judgment-replacement). Note: pending operator op TB-170
(`--skip-goal-alignment` flag) loosens the validator FOR OPERATOR-DRIVEN
adds — it doesn't relax ideation's queue-append path, so the new
mechanical guards proposed below remain effective on ideation proposals.

## Considered & deferred this cycle

- **Auto-abandon of long-stale `Considered & deferred` items**: still
  no signal — same reasoning as last cycle. Defer.
- **`ap2 goal` CLI for operator goal.md edits**: low-impact ergonomics;
  operator can edit goal.md by hand.
- **Insight-file bootstrap**: `_index.md` is still empty. Per ideation
  prompt rules, only propose `#evaluation` reactively when a ranking
  gap needs grounded data. None of this cycle's three proposals do —
  all three are mechanical guards / surfacing tasks, not measurement work.
- **Cross-cycle deferral aging tracker**: requires a new state file or
  MCP tool; lower leverage than the three ranked ahead.
- **Judge-driven Why-now rationale check**: would catch length-passing-
  but-content-thin Why-now lines (e.g. "Why now: this is important and
  necessary work that improves things significantly"). Expensive at
  queue-append time; defer until thin Why-nows actually slip through.
- **No recurring rejection topics**: zero `rejected ideation proposal`
  lines in the recent operator_log.md tail. The TB-163 surfacing exists
  but has no signal yet.

## Open questions for operator

- After this cycle lands TB-171 / TB-172 / TB-173 to Backlog they will
  all sit `@blocked:review`. Approve via `ap2 approve TB-N` or reject
  via `ap2 reject TB-N --reason ...`.
- Pending operator op TB-170 (queued 23:48Z, not yet drained at the
  time of this snapshot) adds `--skip-goal-alignment`; orthogonal to
  this cycle's proposals (which extend the validator on the ideation
  path the skip flag does not touch).
- After TB-171/TB-172/TB-173 land, "Current focus: ideation quality"
  is plausibly `exhausted-needs-operator` — every mechanical guard +
  prompt-side surfacing the focus item describes will be in place.
  Operator may want to update goal.md `## Current focus` to declare
  the next focus area (e.g. "verifier robustness" or
  "operator-walk-away reliability") so future ideation has a fresh
  goal-anchor target.
- No unadopted `cron_proposed` events in the recent-events block.
- `.cc-autopilot/insights/_index.md` still empty. Not blocking.

## Proposals this cycle

- TB-171: extend `_validate_briefing_structure` to reject `Manual:` /
  `[manual]` bullets in `## Verification` at queue-append time
  (mirrors TB-138's prompt rule + `ap2 check` warning into the
  pre-allocation gate; closes gap #1).
- TB-172: extend `_validate_briefing_structure` to reject known
  shell-bullet pitfalls in `## Verification` shell-fenced bullets
  (bare `python`, bare-path-as-command, `grep` without `-r` on a
  directory) at queue-append time (closes gap #2 — the
  TB-156/TB-165/TB-166 pattern).
- TB-173: surface `.cc-autopilot/ideation_state.md`'s `## Open
  questions for operator` section in `ap2 status` and web home so
  ideator-surfaced questions reach the operator without manual
  file-reading (closes gap #3).

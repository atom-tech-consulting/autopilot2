## Mission alignment

ap2's mission (goal.md): autonomous dev loop driving a target project to its
declared `## Done when` with minimal operator intervention. Recent 5 completes
all serve the meta-mission of making that loop trustworthy:

- TB-152 (8bc5297) — `ap2 reject TB-N --reason ...` writes structured
  rejection lines to operator_log.md so ideation can learn from operator
  vetoes (closes the goal.md "drift" guard's feedback loop).
- TB-158 (cad5404) — surfaces failing-bullet headlines + judge notes in
  `ap2 logs` / `/events` / `/task-run/<id>` so operators triage failures
  without re-grepping events.jsonl.
- TB-156 (60c60ff / a4b085c) — judge diff cap 100KB→30KB,
  AP2_VERIFY_JUDGE_EFFORT=high, AP2_STATUS_REPORT_EFFORT=medium token tuning.
- TB-157 (95ec926) — judge_call event + per-run usage totals + ?show=tokens.
- TB-155 (9dcff0d) — web port auto-enumerate on conflict.

No drift. Each ships a visible step toward operator-walks-away reliability.

## Current focus assessment

goal.md "Current focus: ideation quality" is the sole declared focus.

- **Ideation quality (gap-covering without drift; push for progress without
  scope creep)**
  - Progress so far: TB-121 ships the review gate (every ideation add carries
    `@blocked:review`); TB-138 pins the auto-verifiable-bullet rule into
    `ap2/ideation.default.md` + `BRIEFING_TEMPLATE`; TB-154 validates the
    canonical Goal/Scope/Design/Verification/Out-of-scope shape at
    queue-append time (rejected before TB-N is allocated); TB-152 captures
    operator rejection reasons in `operator_log.md` so the ideator's Step 0
    read has structured signal.
  - Gaps: (1) the canonical-structure validator (TB-154) only checks
    section names — a `## Goal` section can sit empty or describe an
    ap2-meta-polish that doesn't connect to any goal.md focus item or
    `## Done when` bullet; goal.md explicitly calls this out
    ("Reject proposals whose value is only 'make ap2 itself nicer'…").
    (2) The reject-reason synthesis loop is half-built: TB-152 captures the
    reasons, but `build_control_prompt` (`ap2/prompts.py`) only injects
    "Recent events" — operator_log.md rejection lines never surface in the
    rendered ideation prompt header, so pattern-level signal (e.g. "operator
    keeps rejecting feature-additions") is invisible at proposal time.
    (3) The scope-creep "delete-test" guard from goal.md
    ("if we delete this and the goal still ships, was it useful?") has no
    mechanical check — briefings can omit any rationale entirely and still
    pass TB-154's structural validator.
  - Status: `in-progress`
  - Reasoning: structural scaffolding (review gate + canonical sections +
    reject capture) is shipped; the open work is folding goal-relevance and
    scope-creep guards into the validator + prompt header so proposals
    arrive already filtered, not just operator-gated after the fact.

## Non-goal risk check

None. Nothing in flight strays into goal.md's Non-goals (generic task
scheduler, multi-tenancy, real-time collab, cross-project orchestration,
operator-judgment-replacement).

## Considered & deferred this cycle

- **Auto-abandon of long-stale `Considered & deferred` items**: nice
  cross-cycle hygiene but no signal yet that any item is stuck. Defer
  until ideation_state.md actually shows repeat deferrals.
- **`ap2 goal` CLI for operator goal.md edits**: low-impact ergonomics
  for a once-per-project surface; operator can edit goal.md by hand.
- **Insight-file bootstrap**: `_index.md` is still empty. Per ideation
  prompt rules, only propose `#evaluation` reactively when a ranking gap
  needs grounded data. None of this cycle's three proposals do — all
  three are mechanical guards, not measurement work.
- **Aggregating reject reasons into ideation_state.md memory**: TB-160
  below covers the rendered-prompt half; cross-cycle aggregation can wait
  for an actual second-cycle re-proposal collision.

## Open questions for operator

- After this cycle lands TB-159 / TB-160 / TB-161 to Backlog they will
  all sit `@blocked:review`. Approve via `ap2 approve TB-N` or reject
  via `ap2 reject TB-N --reason ...`.
- No unadopted `"type":"cron_proposed"` events in the recent-events tail
  (the cron_proposed string only appears inside summary text of completed
  TB-122/TB-123/TB-145/TB-146 rows) — nothing to surface for
  `ap2 cron edit`.
- `.cc-autopilot/insights/_index.md` is still empty. Not blocking, but
  measurement-grounded ranking remains unavailable until insight files
  start landing.

## Proposals this cycle

- TB-159: TB-154 validator extension — require `## Goal` section to cite a
  goal.md focus-item line or `## Done when` bullet (closes the "drift into
  ap2-meta polish" gap mechanically at queue-append time).
- TB-160: Inject "Recent operator rejections (last 5)" block into the
  ideation prompt header via `build_control_prompt` (closes the
  pattern-level reject-signal gap that TB-152's per-line capture left open).
- TB-161: TB-154 validator extension — require a non-empty "Why now"
  rationale within `## Goal` answering the goal.md delete-test (closes
  the scope-creep guard gap mechanically).

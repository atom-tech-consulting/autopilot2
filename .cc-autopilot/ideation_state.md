# Ideation State

_Last updated: 2026-05-06T16:11:49Z by ideation cron_

## Mission alignment

Operator forced ideation (`applied operator-queued ideate → (forced)`
16:11:19Z, operator_log) **one second after** the focus-exhausted
gate correctly fired (`ideation_skipped reason=focus_exhausted`
16:11:18Z) — confirms the post-restart gate works (TB-174's a90b1c0
is now in the running binary) AND signals operator chose force-
override over goal.md rotation. The five most recent completes still
baseline goal.md's "Current focus: ideation quality":

- TB-183 (6583b07, 05:42Z) — proposal-slot-count plumbed into prompt
- TB-174 (a90b1c0, 05:34Z) — focus-exhausted auto-skip gate
- TB-182 (0b8aee9) — cron status-report tasks-awaiting-review fix
- TB-181 (e979fa4) — `/usage` web dashboard
- TB-180 (94a7240) — `ap2 logs` compact `usage` parity

Forced-cycle calibration this run: bias toward Done-when-bullet
anchors (operator-walk-away, failure-recovery) rather than
re-litigating exhausted "ideation quality" gaps; the operator's
forced-but-no-goal-rotation move says "produce forward proposals,
not paralysis, but stay narrow."

## Current focus assessment

- **Ideation quality (gap-covering without drift; push for progress
  without scope creep)**
  - Progress so far: full structural-guard cascade landed — TB-121
    review gate, TB-138 prompt rule, TB-152 reject reasons, TB-154
    canonical structure validator, TB-161 goal-cite, TB-163
    rejection-block, TB-164 Why-now check, TB-171 Manual rejection,
    TB-173 open-questions surfacing, TB-174 focus-exhausted gate
    (now firing post-restart per 16:11:18Z), TB-182 forwarded-
    reference validation, TB-183 slot-count plumbing. TB-170 is
    the operator escape hatch.
  - Gaps:
    (1) **Forced-ideation has no in-band intent channel.** Today's
    16:11:19Z confirms: operator can `ap2 ideate` past the gate but
    the resulting cycle inherits a stale `exhausted-needs-operator`
    assessment with zero signal about which slice of the focus
    item operator wants forward motion on. Heavyweight workarounds
    (rotate goal.md, mutate ideation_state.md) only. Proposal 1
    addresses this.
    (2) Pre-existing carried: TB-175 rejection authoritative;
    TB-172 wack-a-mole rejection authoritative; n=3 literal-string-
    anchor `## Verification` shell-bullet pattern operator-residual-
    risk-accepted.
  - Status: `exhausted-needs-operator`
  - Reasoning: structural guards complete; gap (1) is a candidate
    this cycle. Pre-existing gaps remain operator-rejected. Forced
    cycle today lifts the auto-skip but doesn't change the
    underlying judgment that the focus item is structurally done.

## Non-goal risk check

None. Both proposals anchor to Done-when bullets (operator-walk-
away + failure-recovery), neither touches generic-task-scheduler,
multi-tenancy, real-time collab, cross-project orchestration, or
operator-judgment replacement.

## Considered & deferred this cycle

- **`grep` exit-1 non-presence-check pitfall as ideation.default.md
  prompt addition**: spirit overlaps TB-172's wack-a-mole rejection
  ("enumerate-known-pitfalls generalizes poorly"). Deferred;
  operator residual-risk acceptance carried (n=3: TB-178/182/183).
- **Auto-rotate goal.md `## Current focus`**: violates Non-goal
  "Replacing operator judgment on goal definition."
- **Insight bootstrap (target-project-agnostic)**: TB-175 rejection
  authoritative.
- **Force-fill all 5 backlog slots**: fails goal.md delete-test;
  TB-172/TB-175 reject pattern signals operator wants focused,
  motivated proposals not exploration breadth.
- **`ideation_force_override` event-only observability**: too thin
  standalone; folded into Proposal 1 (the hint-bearing forced
  cycle's audit event covers the same use case).

## Open questions for operator

- **Forced-ideation cadence as goal-rotation signal.** Today's
  16:11:19Z is the first force-override since TB-174 landed. If
  these become routine (≥3/week), that's a stronger signal than
  `exhausted-needs-operator` alone that goal.md `## Current focus`
  should rotate. Proposal 1 lays the audit groundwork (hint-bearing
  events become trackable). Worth deciding: at what cadence do you
  want me to escalate "rotate goal.md" more loudly?
- **Focus rotation still warranted (carried, secondary).** Even
  with forced overrides as a release valve, the focus item self-
  declares exhausted across 5+ consecutive assessments. Refresh
  `goal.md ## Current focus`, OR pause the ideation cron, OR
  explicitly declare ap2 "done enough" for now. Carried candidates:
  "verifier robustness", "operator-walk-away resilience",
  "target-project pivot once one is declared".
- **TB-175 rejection had no `--reason` (carried).** `ap2 reject
  TB-N --reason "..."` is the documented path; one-liner would
  help future cycles avoid re-proposing semantically-similar work.
  Non-blocking.
- **Shell-bullet residual-risk acceptance, n=3 (carried):**
  TB-178/182/183 retries on `grep`-exit-1 non-presence-check
  pattern. All resolved within retry budget; TB-172-class
  structural intervention operator-rejected. Surfacing for
  awareness — confirm this is the durable decision.
- No unadopted `cron_proposed` events.
- `.cc-autopilot/insights/_index.md` still empty; TB-175 rejection
  means ideation will not seed without operator direction.

## Proposals this cycle

5 slots available; proposing 2 (calibrated restraint):

- **TB-184**: `ap2 ideate --hint "<text>"` — per-cycle operator
  intent forwarded into ideation prompt header. Closes gap (1)
  above; anchors to "Current focus: ideation quality" (gap-covering
  without drift) by giving operator an in-band knob to nudge the
  next forced cycle toward a specific slice without rotating
  goal.md.
- **TB-185**: `ap2 frozen TB-N` consolidated triage view (briefing
  + per-bullet failure notes from events.jsonl + git_log_grep
  commit summaries + suggested-classification heuristic). Anchors
  to goal.md Done when bullet 2 ("Failure recovery (verification
  fails, retries exhaust, ...) is fully automatic; only genuine
  design forks escalate") — when escalation is unavoidable, make
  it lightweight.

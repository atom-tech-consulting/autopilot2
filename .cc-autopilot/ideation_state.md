# Ideation State

_Last updated: 2026-05-06T18:37:42Z by ideation cron_

## Mission alignment

Operator pivoted goal.md `## Current focus` to "ideation quality
signal collection" at 18:07:11Z (commit 41bf85b, operator_log).
Forced ideation immediately afterward at 18:37:15Z. The five most-
recent completes were authored under the prior focus's structural-
guard era and don't yet bear on the new focus, but they DO confirm
the mechanical-guard cascade is durable enough that prompt-craft
iteration without empirical signal is the operator-diagnosed
bottleneck.

- TB-187 (33effb4, 18:37Z) — mixed-blocker pending-review surfacing fix
- TB-186 (4b9c553, 16:33Z) — slot-check / cooldown ordering fix
- TB-183 (6583b07, 05:42Z) — proposal slots plumbing
- TB-174 (a90b1c0, 05:34Z) — focus-exhausted gate
- TB-182 (0b8aee9) — cron status-report tasks-awaiting-review fix

This cycle re-derives from scratch against the new focus, per the
18:07Z operator_log directive.

## Current focus assessment

- **Ideation quality signal collection (operator pivot 18:07:11Z;
  goal.md lines 38-76)**
  - Progress so far: zero TB-Ns shipped against this focus item —
    it's ~30 minutes old. Adjacent prior work that produced the
    free-text rejection signal stream: TB-152 (`ap2 reject --reason`
    appends to operator_log.md), TB-163 (Recent operator rejections
    block injected into ideation prompt header), TB-173 (Open
    questions surfacing). Together those give Step 0 the existing
    operator-decision signal — but only as prose lines, with no
    structured per-proposal record binding cycle context to
    outcome.
  - Gaps:
    (1) **No structured per-proposal record exists.** When ideation
    calls `do_board_edit({"action": "add_backlog", ...})` (cf.
    ideation.default.md L262), the only artifacts are a TB-N row in
    TASKS.md, the briefing file, and the operator_log audit line.
    Cycle context (focus_anchor, why_now, gap_addressed) lives only
    in briefing prose, and outcome (approved / rejected / completed
    / failed) isn't linked back to that context in a queryable form.
    Proposal 1 (TB-188) addresses this — the seed every downstream
    signal-collection task will query.
    (2) **No retrospective delete-test signal stream.** goal.md L61-76
    names the delete-test as the diagnostic for goal-shaped pro-forma
    compliance, but there is no operator surface to record the
    delete-test verdict on a shipped proposal. The signal the focus
    item most needs (operator answering "did this advance the goal in
    substance") literally does not accumulate today. Proposal 2
    (TB-189) adds `ap2 classify TB-N --delete-test ...`.
    (3) **Track-record feedback into the next ideation cycle.** Once
    (1) and (2) accumulate data, ideation should read its own track
    record at proposal time. Out-of-scope this cycle (depends on
    (1)+(2) producing signal volume first); will propose a
    "Delete-test track record" prompt-header block in a later cycle.
  - Status: `in-progress`
  - Reasoning: focus is fresh; zero shipped TB-Ns; gaps (1)–(3) are
    the natural decomposition; (1) and (2) are this cycle's
    proposals; (3) waits for data.

## Non-goal risk check

Both proposals anchor strictly to ideation signal capture / signal
surfacing. Neither rotates goal.md (operator-owned), neither extends
the chat surface beyond TB-152's existing operator-queue precedent,
neither aggregates across projects, neither pushes into multi-tenancy
or real-time. Clear.

## Considered & deferred this cycle

- **Re-propose TB-175 framing (acceptance-rate insight regenerator
  → `.cc-autopilot/insights/ideation_quality.md`).** TB-175 was
  rejected at 05:15:59Z with "(no reason given)" BEFORE the 18:07Z
  pivot. The new focus arguably re-validates the framing, but the
  operator's recent rejection pattern (narrow / focused / one gap
  per proposal — TB-184/185 today) suggests landing TB-188 (the
  data source) first, then re-proposing the regenerator next cycle
  with richer signal density, is more likely to ship than re-
  proposing the same idea cold. Surfaced in Open questions below.
- **`ap2 reject --category <enum>` rejection-reason taxonomy.**
  TB-172's "enumerate-known-cases generalizes poorly" rejection
  pattern applies — a fixed N-key enum risks the same wack-a-mole
  framing, and free-text reasons + LLM mining at insight-regenerator
  time is structurally cleaner.
- **Self-evaluation field in ideation_state.md (Recent
  retrospectives prompt section).** Operator's 18:07Z pivot
  explicitly names "prompt-language craft" as NOT the bottleneck;
  operator-authored verdict (TB-189) is the right primary signal
  before any prompt-side retrospective field.
- **`ap2 ideate --hint` re-proposal.** TB-184 rejected today
  18:01:11Z: "goal.md is the operator-intent channel (Non-goal:
  'operator owns goal.md'); --hint creates a parallel surface that
  erodes that authority." Authoritative; will not re-propose.
- **`ap2 frozen TB-N` triage view (TB-185 re-proposal).** Rejected
  17:57Z as "Frozen tasks are very rare right now; consolidated
  triage isn't the bottleneck." Authoritative; will not re-propose.

## Open questions for operator

- **TB-175 re-proposal sequencing.** TB-175's framing (insight
  regenerator → `.cc-autopilot/insights/ideation_quality.md`) was
  rejected 05:15Z with no reason. The 18:07Z pivot makes this kind
  of work the explicit focus. Plan: land TB-188 first (the signal
  source), then re-propose the regenerator next cycle with richer
  data. Confirm sequencing, or signal "re-propose now with broader
  scope including legacy operator_log mining."
- **Delete-test verdict enum shape (TB-189).** Proposed three
  values: `advanced-goal`, `pro-forma`, `unclear`. Open to
  extending (e.g. `partial-pro-forma` for proposals that solved a
  sub-problem but not the headline gap). Will adopt your shape;
  proposal is a starting point.
- **Rejection pattern observation (n=4 today).** TB-184/185 today,
  TB-172/175 prior — operator vetoes lean toward "doesn't move the
  signal-collection focus forward" / "creates parallel intent
  surfaces" / "enumeration generalizes poorly." Both proposals this
  cycle pass each filter; flagging the pattern for cross-cycle
  memory.
- No unadopted `cron_proposed` events.
- `.cc-autopilot/insights/_index.md` still empty. TB-188's records
  become the natural seed for the first insight file; the next-
  cycle regenerator proposal would populate it.

## Proposals this cycle

5 slots available; proposing 2 (focused; both anchor to the new
focus item):

- **TB-188**: Capture per-proposal record at ideation `add_backlog`;
  reconcile outcome on terminal events. Closes gap (1) — the
  foundational signal capture.
- **TB-189**: Add `ap2 classify TB-N --delete-test <verdict>` for
  operator-authored retrospective delete-test verdicts on shipped
  proposals. Closes gap (2). Blocked on TB-188 (per-proposal record
  is the storage target for the verdict).

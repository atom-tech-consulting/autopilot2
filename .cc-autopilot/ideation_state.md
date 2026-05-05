# Ideation State

_Last updated: 2026-05-05T23:33:53Z by ideation cron_

## Mission alignment

Recent 5 completes still all serve the meta-mission of making the
ideation→approve→dispatch loop trustworthy:

- TB-176 (9df5a15) — added `ideate [force]` to MM handler chat-verb
  list (parity with `ap2 ideate [--force]` from TB-159); retry-passed
  after first run hit verification_failed on bullet 4, agent added
  e2e routing test in `ap2/tests/e2e/test_tb176_mm_ideate_routing.py`.
- TB-173 (aee515e) — surface ideation_state.md "Open questions for
  operator" in `ap2 status` text + JSON + web home + cron
  status-report state_extras.
- TB-171 (4344cc2) — `_validate_briefing_structure` rejects `Manual:`
  / `[manual]` bullets in `## Verification` at queue-append.
- TB-170 (a47328e) — `--skip-goal-alignment` flag on `ap2 add` /
  `ap2 update`.
- TB-169 (0d4fd53) — trim ideation `_events_block` to curated allowlist.

TB-176 was operator-added at 23:02:57Z (not ideation-proposed) so it
doesn't move the proposal-acceptance signal — but it's adjacent to
ideation tooling and the retry recovery is a clean win for the
verifier. **NINE consecutive no-op ideation ticks now (07:15Z, 09:17Z,
11:19Z, 13:21Z, 15:23Z, 17:27Z, 19:29Z, 21:32Z, 23:33Z)** since
TB-174/TB-175 were proposed — every one would have been suppressed
cleanly by TB-174's auto-skip.

## Current focus assessment

goal.md "Current focus: ideation quality" is the sole declared focus.

- **Ideation quality (gap-covering without drift; push for progress
  without scope creep)**
  - Progress so far: structural guards cover every gap last week's
    assessments named (TB-121 review gate, TB-138 prompt rule, TB-152
    reject reasons, TB-154 canonical structure validator, TB-161
    goal-cite, TB-163 rejection-block in prompt header, TB-164 Why-now
    check, TB-171 Manual-bullet rejection, TB-173 open-questions
    surfacing). TB-170 is the operator escape hatch. TB-176 closed
    the chat-side gap on `ap2 ideate` parity.
  - Gaps:
    (1) `parse_focus_statuses` + auto-skip wiring is **proposed** as
    TB-174 and awaiting `ap2 approve` since 2026-05-05T01:09Z (~22.5h
    pending review). This cycle (23:33Z) is the NINTH consecutive
    no-op tick TB-174 would have suppressed — cost evidence keeps
    compounding (~$0.90-$1.00 per `control_run_usage` × 9 ticks).
    (2) Shell-bullet pitfall enumeration: rejected by operator on
    2026-05-05T00:45Z (TB-172) — accepted residual risk. Not
    re-proposed.
    (3) Ideation acceptance-rate insight is **proposed** as TB-175
    and awaiting `ap2 approve` (~22.5h pending review). Until it
    lands, no quantitative signal on whether the structural-gate
    cascade moved acceptance rate vs the pre-gate baseline.
  - Status: `in-progress`
  - Reasoning: gaps #1 and #3 are addressed by tasks already in
    Backlog; both blocked on operator review. The actionable next
    step is operator review of pending proposals, not a fresh
    proposal from ideation. Status stays `in-progress` rather than
    `exhausted-needs-operator` because TB-174's auto-skip is exactly
    the mechanism that would let this focus item surface
    `exhausted-needs-operator` cleanly — until it lands, the
    distinction is fuzzy and ideation keeps firing on cooldown.

## Non-goal risk check

None. No in-flight work; nothing strays into goal.md's Non-goals.

## Considered & deferred this cycle

- **Re-proposing anything covered by TB-174/TB-175**: both still in
  Backlog, blocked on review since ~22.5h. A third proposal addressing
  the same gaps would be drift (and would compete with the existing
  proposals for operator attention without adding signal).
- **Shell-bullet pitfall validator (any flavor)**: operator rejected
  TB-172 on 2026-05-05 with "wack-a-mole … generalizes poorly."
  Authoritative — including `bash -n` / shellcheck / actually-execute-
  in-sandbox variants. Don't re-propose. Pattern: operator vetoes
  enumerate-known-cases linters that "generalize poorly"; future
  validator proposals should target structural rules (TB-154/161/164
  shape) not enumerated-pitfall lists.
- **Surface focus statuses in `ap2 status`** (per-focus-item status
  row): defer until TB-174 lands and reveals whether per-focus
  surfacing is needed beyond the auto-skip behavior + TB-173
  open-questions row.
- **Auto-rotate goal.md `## Current focus` when exhausted**:
  violates Non-goal "Replacing operator judgment on goal definition."
  Operator owns focus rotation.
- **Cross-cycle deferral aging tracker / "stale deferral" event**:
  carried; NINE consecutive no-op cycles now; still no signal that
  long-stale deferrals are a problem in themselves. Defer.
- **Greenfield follow-ups on TB-168/169/170/171/173/176**: each just
  shipped a focused improvement; no edge case or natural extension
  surfaces yet that isn't already covered by the two pending
  proposals. TB-176's first-run verification_failed → retry-pass was
  the verifier behaving correctly (agent added the missing e2e test);
  no follow-up implied.
- **"Ideation cron self-throttle on no-op streak" task**: tempting
  given nine consecutive no-op ticks, but TB-174 is exactly that
  mechanism. A separate no-op-streak counter would duplicate the
  gate without adding signal — wait for TB-174 first.
- **Force-propose a third item just to fill Backlog to ≥3**: with
  every gap covered by an awaiting-review proposal, a third task's
  only "Why now" would be "fill the slot" — the exact "cool /
  might-be-useful-later" failure mode goal.md lines 61-70 prohibit.
  Backlog<3 is a ceiling, not a mandate.
- **Surface daemon_pause/_resume audit in operator log**: carried;
  17:17→17:27Z pause/resume window already digested by status-report
  cron; still no signal that operator pauses need persistent narration
  beyond the events feed. Defer.

## Open questions for operator

- **Tasks awaiting review (`ap2 approve` / `ap2 reject`)**: TB-174,
  TB-175. Both gated `@blocked:review` per TB-121 and pending since
  2026-05-05T01:09Z (~22.5h). **NINE consecutive no-op ideation ticks
  (07:15Z → 23:33Z)** since they were proposed — TB-174 is the exact
  fix for that wasted-tick shape and gets more cost-justified each
  cycle it sits unreviewed (~$0.90-$1.00 per `control_run_usage`).
- **Focus-rotation candidate** (carried): after TB-174/TB-175 land +
  approve, "Current focus: ideation quality" is plausibly
  `exhausted-needs-operator`. Operator may want to refresh goal.md
  `## Current focus` (e.g. "verifier robustness", "operator-walk-away
  resilience", or a target-project focus item) so future ideation
  has a fresh anchor and TB-174's auto-skip gate unlatches.
- **Shell-bullet residual-risk acceptance** (carried): TB-172 reject
  implies the shell-bullet pitfall class stays a verifier-side gate
  (no queue-append linting). Confirm that's the durable decision
  (or thaw via a fresh design proposal — but not as a wack-a-mole
  enumerator).
- No unadopted `cron_proposed` events.
- `.cc-autopilot/insights/_index.md` still empty; TB-175 bootstraps
  the first insight on approval.

## Proposals this cycle

Backlog already populated with the proposals that address every
actionable gap (TB-174, TB-175, both awaiting review). No new
proposals this cycle — adding a third would be drift relative to
goal.md's "push for progress without scope creep" guard, and would
compete for operator attention with the two already queued.

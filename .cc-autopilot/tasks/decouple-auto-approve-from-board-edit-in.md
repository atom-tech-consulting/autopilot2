## Goal

Advance `Current focus: extract the remaining core subsystems into components`
by making `board_edit` policy-free and moving the auto-approve gate chain into
the `auto_approve` component as a discrete loop pass. goal.md axis (3): today the
strip is evaluated inside `board_edits.py`'s `add_backlog` branch
(`evaluate_auto_approve_decision`, L201) mid-agent-run, and the
`should_auto_approve` tags policy squats in `ideation.py` (L798) — the
cross-boundary knot that blocks the ideation extraction (axis 4). Make proposals
always born `@blocked:review`, and turn the `auto_approve` component's no-op
`_tick_hook` (`ap2/components/auto_approve/manifest.py` L105) into a real loop
pass that runs after ideation and before dispatch, stripping `@blocked:review`
from Backlog tasks that clear the gates. Behavior + event payloads
(`auto_approved`, `would_auto_approve`) preserved bit-for-bit.

Why now: goal.md's axis-(3) delete-test says "if the strip stays in
`board_edit`, ideation can't be extracted without core→component import
violations" — untangling this coupling is the prerequisite that unblocks axis 4.

## Scope

- Make `board_edits.py`'s `add_backlog` path policy-free: drop the inline
  `evaluate_auto_approve_decision` call + the `@blocked:review` strip; proposals
  always land with the review token intact.
- Move the auto-approve gate chain (`evaluate_auto_approve_decision` + the
  `should_auto_approve` tags policy) into `ap2/components/auto_approve/` so it is
  owned by the component, not reached from core; the no-op `_tick_hook`
  placeholder becomes a real loop pass.
- The loop pass runs after the ideation stage and before dispatch in
  `daemon._tick`, walks Backlog `@blocked:review` tasks, strips the token for
  tasks that clear the gates (master knob + tags + violation/window gates), and
  emits the existing `auto_approved` / `would_auto_approve` events with unchanged
  payloads.
- Preserve every `AP2_AUTO_APPROVE*` knob name and the dry-run / window
  semantics verbatim.

## Design

Invert the current control flow: instead of `board_edit` deciding approval at
mutation time (mid-agent-run, where a task-agent snapshot can capture a
half-applied board), proposals are uniformly born `@blocked:review` and a
separate daemon loop pass evaluates approval between runs. The loop pass lives on
the `auto_approve` component's tick hook (today a no-op POST_DISPATCH
placeholder); it reuses the existing `evaluate_auto_approve_decision` gate chain
verbatim — only its call site moves. `should_auto_approve` relocates from
`ideation.py` into the component so core no longer reaches across the boundary
(the import-direction CI gate stays green). Sequencing is load-bearing: the pass
must run after ideation appends proposals and before the dispatch stage promotes
Ready tasks, so a proposal added this tick can be auto-approved and dispatched in
the same tick exactly as today. Event payloads (`auto_approved`,
`would_auto_approve`) are emitted from the new site with identical fields so the
web/events surfaces and the 24h would-auto-approve dry-run window are unchanged.

## Verification

- `uv run pytest -q` — full suite passes.
- `! grep -nE 'evaluate_auto_approve_decision' ap2/board_edits.py` — `board_edit`
  no longer evaluates the auto-approve gate inline; the add_backlog branch is
  policy-free.
- `uv run pytest -q ap2/tests/test_tb223_auto_approve.py ap2/tests/test_queue_drain_auto_approve.py` — the auto-approve gate chain + would-auto-approve simulation behavior is preserved.
- `ap2/components/auto_approve/manifest.py` Prose: the `_tick_hook` is now a real
  loop pass that strips `@blocked:review` from gate-clearing Backlog tasks (not
  the no-op placeholder); judge confirms via Read.
- Prose: a new regression test pins that a proposal added via `board_edit` lands
  `@blocked:review` regardless of `AP2_AUTO_APPROVE`, and the `auto_approve` loop
  pass strips it on a subsequent pass only when the master knob + gates pass;
  judge confirms via Read.

## Out of scope

- Extracting pipeline (axis 1), cron (axis 2), the prose-judge (axis 5), or
  ideation itself (axis 4) — separate tasks. This task only unties the
  auto-approve coupling that axis 4 depends on.
- Any change to which tasks the gate chain approves (tag policy, violation/window
  thresholds) — the decision logic moves unchanged.
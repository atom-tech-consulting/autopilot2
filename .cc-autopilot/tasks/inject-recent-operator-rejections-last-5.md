# TB-160 — Inject "Recent operator rejections (last 5)" block into ideation prompt header

Tags: `#autopilot` `#ideation` `#prompts` `#operator-log` `#review-gate`

## Goal

Close the second half of TB-152's reject-feedback loop. TB-152 captures
structured rejection reasons in `operator_log.md`
(`<ts> — rejected ideation proposal → TB-N (<title>): <reason>`), and the
ideation prompt's Step 0 says "operator_log.md is authoritative — ideation
won't re-propose decisions logged here, even if your prior assessment
surfaced them." But that's per-line shadowing — pattern-level signal
("operator keeps rejecting feature-additions framed as 'might be useful
later'") is invisible at proposal-authoring time because the rendered
ideation prompt only exposes the daemon's "Recent events" tail, which
doesn't include operator_log.md.

This advances goal.md's "Current focus: ideation quality" — specifically the
"Gap-covering without drift" failure mode goal.md flags (goal.md lines 50-59)
— by making operator-veto patterns visible during the same SDK turn the
ideator drafts proposals.

## Scope

- `ap2/prompts.py::build_control_prompt` (or its `_current_state_block`
  helper) — read the last `N` lines from `.cc-autopilot/operator_log.md`,
  filter for `rejected ideation proposal` lines, render into a block titled
  `## Recent operator rejections (last <K>)` ahead of the existing recent
  events tail. Default `K=5`; cap N read at 200 to avoid scanning the
  whole file. No new env knob — keep the surface tight.
- New helper `ap2/operator_log.py::tail_rejections(cfg, limit=5)` (extract
  if symmetric helpers already exist; otherwise add a small reader). Pure
  function, no I/O side effects beyond the read.
- `ap2/ideation.default.md` — add a one-line directive telling the ideator
  to consult the new block when ranking and to record any pattern in
  `ideation_state.md`'s "Considered & deferred" section.
- New tests in `ap2/tests/test_prompts.py` covering the block's presence
  with/without rejection lines, the truncation cap, and the chronological
  order (newest last, matching the events block convention).

Out-of-scope: cross-cycle aggregation in `ideation_state.md` itself
(the assessment file is ideator-written; mechanical aggregation can wait
for an actual second-cycle collision); web/CLI surfaces for the rejection
log (operator already has direct file access).

## Design

The existing `_current_state_block` formats `now:`, board counts, and a
`git log -n 10` block. Add a sibling subsection rendered only when at
least one rejection line was found in the last 200 lines of
`operator_log.md`:

    ## Recent operator rejections (last K)
    - 2026-05-04T05:53:25Z — TB-150 (web /pending-review section): superseded by web tag-pill renderer
    - 2026-05-03T22:17:12Z — TB-X (...): ...
    ...

Truncate each reason to ~120 chars; preserve TB-N + title for searchability.
Skip the block entirely (no empty heading) when there are no recent
rejections — keeps the prompt clean for fresh projects.

The reader walks operator_log.md backwards line-by-line (already O(file
size) but the file is append-only and small). Filter regex matches the
TB-152 line shape (`— rejected ideation proposal → TB-`). Stop after K
matches or 200 lines, whichever first.

## Verification

- `uv run pytest -q ap2/tests/` — full regression gate passes.
- `uv run pytest -q ap2/tests/test_prompts.py -k rejection` — the new prompt
  cases pass.
- `grep -q "Recent operator rejections" ap2/prompts.py` — block heading
  emitted by the renderer.
- `grep -qE "tail_rejections|rejected ideation proposal" ap2/prompts.py ap2/operator_log.py` —
  reader is wired into the prompt path (operator_log.py may not exist yet
  — accept either tools.py or a new module file).
- `grep -q "Recent operator rejections" ap2/ideation.default.md` — directive
  added to the ideation prompt body so the ideator is told to consult it.
- New test `test_build_control_prompt_renders_rejection_block_when_present`
  in `ap2/tests/test_prompts.py` writes a fixture operator_log.md containing
  3 rejection lines + unrelated lines, asserts the rendered prompt contains
  the heading and all 3 TB-Ns in newest-last order.
- New test `test_build_control_prompt_skips_rejection_block_when_empty`
  in `ap2/tests/test_prompts.py` asserts the heading is absent when no
  rejection lines exist.
- New test `test_build_control_prompt_truncates_rejection_block_to_default_limit`
  in `ap2/tests/test_prompts.py` writes 7 rejection lines, asserts only
  the most recent 5 appear.

## Out of scope

- Cross-cycle synthesis into `ideation_state.md`'s "Considered & deferred"
  section (the ideator can do this manually now that the data is in their
  rendered prompt — mechanical aggregation can wait for evidence it's
  needed).
- Web view / CLI subcommand for browsing rejection history (operator can
  read operator_log.md directly).
- Filtering by reason-classifier (e.g. "drift" vs "scope-creep" vs
  "duplicate") — defer until enough volume exists to make patterns visible
  without classification.

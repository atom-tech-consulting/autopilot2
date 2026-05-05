# TB-175 — Insight: compute post-TB-121 ideation acceptance rate from operator_log.md; bootstrap `.cc-autopilot/insights/ideation_quality.md`

Tags: `#autopilot` `#ideation` `#evaluation` `#insights` `#observability`

## Goal

Compute the post-TB-121 ideation proposal acceptance rate (count of
`approve TB-N` lines vs `rejected ideation proposal → TB-N` lines in
`.cc-autopilot/operator_log.md`) and write the result to
`.cc-autopilot/insights/ideation_quality.md` with YAML front matter
(`tldr`, `updated`, `updated_by: TB-175`, `cites`) so TB-89's index
regeneration renders the file's tldr in `_index.md`. The insight
becomes the first grounded signal future ideation cycles can cite
when re-evaluating goal.md's "Current focus: ideation quality"
follow-up question of "is the structural-gate cascade
(TB-121/138/154/161/163/164/171) actually improving outcomes?" Today
the focus is judged on intuition; goal.md's "Push for progress
without scope creep" delete-test ("if we delete this and the goal
still ships, was it useful?") needs grounded answers, not hand-waves.

Why now: structural ideation-quality work since TB-121 is approaching
exhaustion — last cycle's assessment + this cycle's both flag the
focus as "plausibly exhausted-needs-operator next cycle." Before the
operator considers rotating focus, they need a grounded signal — not
intuition — for whether the structural gates worked. The
`.cc-autopilot/insights/_index.md` is empty today; this also
bootstraps the insights directory the project has carried since
TB-89, so future Step 0.5 cycles have at least one grounded
artifact to cite.

## Scope

This is a one-shot analysis task — no production code changes, only
the new insight file.

- Read `.cc-autopilot/operator_log.md`. Lines of interest (literal
  patterns the file uses today):
  - `... — applied operator-queued add_backlog → TB-N`
    (proposal landed in Backlog — most are ideation proposals,
    some are direct operator adds; classify by whether the line
    appears within ~minutes of an `ideation_complete` event in
    events.jsonl, or fall back to "all add_backlog post-TB-121
    are candidate proposals").
  - `... — applied operator-queued approve → TB-N`
    (operator approval — TB-121 review-gate path).
  - `... — rejected ideation proposal → TB-N (...): <reason>`
    (TB-152 explicit reject reasons).
  - `... — applied operator-queued delete → TB-N`
    (older delete pre-TB-152 — pre-reason-capture).
- Compute counts since TB-121 landed (find via
  `git log --grep="TB-121:" --reverse --format=%H -1` then take
  the timestamp of that commit, or substitute the operator_log.md
  cutoff line whose timestamp is just after that commit).
- Write `.cc-autopilot/insights/ideation_quality.md` with this
  YAML front matter at the top of the file:

      ---
      tldr: <≤200-char one-line summary, e.g. "post-TB-121 ideation
        acceptance rate: X/Y = Z%; N rejections cite N distinct
        reasons">
      updated: 2026-05-05
      updated_by: TB-175
      cites: [TB-121, TB-138, TB-152, TB-154, TB-161, TB-163, TB-164,
              TB-171]
      ---

  Body: a short table (Proposed | Approved | Rejected | Deleted) for
  the post-TB-121 window, the per-rejection reason list pulled
  verbatim from operator_log.md (TB-152 captured them; today there's
  one — TB-172), and one paragraph on whether the trend supports
  rotating goal.md `## Current focus` next cycle.

## Design

The task agent reads `operator_log.md` directly (it's in the agent's
read scope — it's fenced for ideation/MM-handler writes, but
read-only access stays open). Counts are line-based regex matches; no
SDK call needed beyond the agent's own analysis. The output file
must satisfy the existing insights-index regeneration shape (TB-89)
— front matter fields are required for `_index.md` to render the
file's tldr line.

Citation rule (per Step 0.5): the file MUST list every TB-N it
relies on in `cites:`. The list above covers the structural-gate
cascade the insight evaluates; the agent may add more if the
analysis surfaces other relevant TB-Ns.

## Verification

- `test -f .cc-autopilot/insights/ideation_quality.md` — output
  artifact exists.
- `grep -nE "^tldr:" .cc-autopilot/insights/ideation_quality.md` —
  YAML front matter has tldr.
- `grep -nE "^updated_by: TB-175" .cc-autopilot/insights/ideation_quality.md`
  — front matter cites this task.
- `grep -nE "^cites:" .cc-autopilot/insights/ideation_quality.md` —
  cites field present.
- `grep -nE "TB-121" .cc-autopilot/insights/ideation_quality.md` —
  body or front matter cites TB-121 (the post-review-gate baseline).
- `uv run python -c "import yaml, pathlib; t=pathlib.Path('.cc-autopilot/insights/ideation_quality.md').read_text(); _, fm, _ = t.split('---', 2); m=yaml.safe_load(fm); assert m.get('updated_by')=='TB-175', m; assert isinstance(m.get('cites'), list) and len(m['cites'])>=3, m"` — front matter is parseable YAML and contains the required keys.
- The file body contains a numeric acceptance-rate figure (a
  percentage or fraction shape `X/Y` or `Z%`) computable from
  operator_log.md — judge confirms by cross-checking the line counts
  in operator_log.md.
- `uv run pytest -q ap2/tests/` — full suite passes (no production
  code changed; this is just a sanity gate that the briefing didn't
  accidentally edit `ap2/`).

## Out of scope

- Wiring the acceptance-rate metric into a CLI / status surface or
  the web UI (insights are read by `_index.md` regeneration today;
  if a CLI surface is wanted, propose separately after this lands).
- Ongoing measurement / dashboards / cron-driven re-computation —
  this is a one-shot grounding artifact; future cycles can re-run
  via a follow-up task or evolve it manually.
- Editing operator_log.md to backfill missing reject reasons (its
  format is operator-owned; we only read).
- Drawing conclusions for the operator on whether to rotate
  goal.md `## Current focus` — the file presents the data; the
  rotation decision is operator-owned (Non-goal: replacing operator
  judgment on goal definition).

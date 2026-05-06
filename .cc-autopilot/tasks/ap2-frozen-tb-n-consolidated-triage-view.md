# `ap2 frozen TB-N` — consolidated triage view for retry-exhausted tasks

## Goal

When a task lands in Frozen via `retry_exhausted`, the operator
faces a "design fork" — re-run vs. edit-briefing vs. follow-up
vs. abandon (the exact taxonomy ideation already documents in
its failure-review step). Today the information needed to
classify is scattered across events.jsonl (verification_failed
events with bullet notes), the briefing file (the verification
bullet text), and `git log --grep=<TB-N>` (commits that may
already cover scope). A focused `ap2 frozen TB-N` CLI consolidates
this into one read-only operator view, anchoring directly to
goal.md's Done when bullet 2 — "Failure recovery (verification
fails, retries exhaust, daemon restart, cron drift, agent
timeouts) is fully automatic; only genuine design forks escalate"
— by making the residual escalation path lightweight.

Why now: every retry_exhausted today forces the operator into a
multi-tool dig (`ap2 logs --grep <TB-N>`, `cat
.cc-autopilot/tasks/<slug>.md`, manual git-log scan, manual
cross-reference of bullet text against failure events) before
they can decide. The ideation cron prompt already documents the
exact triage classification (edit-briefing / split / follow-up /
abandon) but only ideation runs that path; pre-ideation operator
triage has no equivalent surface. Three Frozen tasks today
(TB-119/120/133) plus the recurring n=3 retry pattern observed
in TB-178/182/183 (each near-frozen before fix) confirm this is
a routine operator workflow, not a one-off.

## Scope

(1) New `ap2 frozen TB-N` subcommand in `ap2/cli.py` (handler
`cmd_frozen`):
  - Reads the briefing file from the task's `[→ brief](...)`
    link path (or computes the canonical path from TB-N + slug
    via the existing briefing-path helper).
  - Walks `.cc-autopilot/events.jsonl` for events whose
    `task_id` field matches TB-N AND whose `type` is one of
    `verification_failed`, `verification_partial`,
    `retry_exhausted`, `task_error`. Surfaces, per failed
    bullet: bullet text (verbatim from briefing's `##
    Verification` section), exit code, captured notes
    (stderr/stdout snippet from verifier).
  - Calls `git log --grep=<TB-N> --oneline -n 20` (subprocess)
    and prints commit summaries — operator sees implementation
    that may already be on disk despite the verification
    failure (TB-127/TB-136 era).
  - Prints a `Suggested classification:` heuristic line based
    on the event pattern (e.g. "all failed bullets share
    `exit=127` → likely edit-briefing", "no `<TB-N>` commits
    in HEAD → likely follow-up").

(2) JSON output mode (`--json`) emits a structured object with
the same fields (briefing_path, failed_bullets[], commits[],
classification_hint) for programmatic consumption / future
status-JSON parity.

(3) Doc updates: `skills/ap2/SKILL.md` gets a one-line entry
under operator inspection surfaces; `ap2/architecture.md`
mentions the triage view alongside `ap2 status` / `ap2 logs`.

## Design

Pure read-only — reuses existing parsers (briefing reader, event
walker, board parser for the `[→ brief](...)` link extraction)
and the `git log --grep` subprocess pattern already used
elsewhere. No new state files. No SDK calls. No mutations to
the operator queue or task state. Subcommand lives alongside
`ap2 status` and `ap2 logs` as a third inspection tool.

Argparse wires a `frozen` subparser that takes a positional
`task_id` (TB-N) and an optional `--json` flag; CLI exits 1
with a clear message when TB-N is not in the Frozen section.

## Verification

- `uv run pytest -q ap2/tests/` — full suite passes
- `test -f ap2/tests/test_cli_frozen.py` — dedicated test module
  exists
- `grep -nE "def cmd_frozen" ap2/cli.py` — CLI handler present
- `grep -nE "ap2 frozen|cmd_frozen" ap2/cli.py` — subcommand
  registered with argparse
- `grep -nE "ap2 frozen" skills/ap2/SKILL.md` — skill doc
  updated
- New unit tests in `ap2/tests/test_cli_frozen.py` pin: command
  resolves TB-N → briefing path; walks events.jsonl filtering by
  task_id + event type; renders failed-bullet text with exit
  codes and notes; surfaces `git log --grep` summaries; emits
  the `Suggested classification:` heuristic line; `--json` mode
  produces a parseable structured object; CLI exits non-zero
  with a clear message when TB-N is not Frozen
- New e2e test in `ap2/tests/e2e/test_tb185_frozen_triage.py`
  seeds a fixture project with one Frozen task carrying multiple
  `verification_failed` events and a `<TB-N>:` commit, runs `ap2
  frozen TB-N`, and asserts the rendered output contains the
  briefing path, the failed-bullet text, the exit code from the
  events, and the commit summary

## Out of scope

- Auto-classifying the failure (edit-briefing / split /
  follow-up / abandon) — that judgment stays with the operator
  + ideation's failure-review step. The CLI surfaces the
  signals, doesn't decide.
- Auto-unfreezing or auto-editing briefings (operator owns those
  via existing `ap2 unfreeze` / `ap2 update` queue ops; this
  surface is read-only).
- Web-UI parallel page (could follow as a separate task once
  the CLI shape stabilizes — keeps THIS task scoped).
- Coverage of operator-frozen tasks that never had a
  `retry_exhausted` event (TB-119/TB-120/TB-133 are intentional
  freezes; the CLI still resolves them but the events list will
  be empty — that's correct behavior, not a gap to close here).

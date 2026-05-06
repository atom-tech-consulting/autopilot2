# `ap2 classify TB-N --delete-test <verdict>` — operator-authored retrospective verdict on shipped proposals

## Goal

Current focus: ideation quality signal collection names "goal-shaped
pro-forma compliance" as the failure mode signal collection exists
to detect (goal.md L66-76), and names the delete-test as the
diagnostic (L61-70: "if we delete this and the goal still ships,
was it useful?"). The delete-test is *retrospective* — applied
AFTER a proposal lands and produces an outcome.

There is currently no operator surface to record the delete-test
verdict on a shipped proposal. Operator-authored verdict is the
strongest signal in the focus item's signal-collection program
because it is the OPERATOR answering the failure-mode question
directly — not an LLM inferring it from prose. Without this
surface, the data ideation needs to learn "which proposal shapes
turned out to be pro-forma" does not accumulate.

Why now: TB-188 (queued same cycle) creates the per-proposal record
file. Without a complementary operator-authored verdict surface
writing into that record, the focus item's primary diagnostic stays
unrecorded — even with TB-188's outcome block populated, we'd only
know the proposal completed / failed verification, not whether the
operator considers the landed work to have advanced the goal in
substance.

## Scope

Add a new operator-queue op `classify` and the matching CLI verb
`ap2 classify TB-N --delete-test <verdict> [--reason "<text>"]`
where `<verdict>` is one of three fixed values:

- `advanced-goal`: operator judges the proposal substantively moved
  the goal forward.
- `pro-forma`: operator judges the proposal satisfied validators
  but did not move the goal forward (the failure mode in goal.md
  L66-76).
- `unclear`: operator can't classify yet (e.g. impact will only be
  visible after subsequent work; downstream signal still maturing).

CLI surface (`ap2/cli.py`):

- `ap2 classify TB-N --delete-test <verdict> [--reason "<text>"]`
  validates the verdict against the enum and queues an operator-
  queue record `{op: "classify", task_id: "TB-N", verdict, reason,
  ts}`. CLI exits non-zero if verdict is missing or not in the enum.

Chat surface (MM handler — already routes through
`operator_queue_append` per TB-152):

- `classify TB-N <verdict> ["<reason>"]` — same routing pattern as
  the existing `reject` chat verb (TB-152 + TB-176 precedent).

Operator-queue drain (`ap2/tools.py` / `ap2/daemon.py` drain stage):

- New handler for `op == "classify"`. Appends to `operator_log.md`:
  `<ts> — classified TB-N delete-test=<verdict>: <reason>` (matches
  the line shape used by TB-152's reject handler).
- Loads `.cc-autopilot/ideation_proposals/<TB-N>.json` (created by
  TB-188); appends a `delete_test` block:
  `{verdict, classified_at, reason}`. Tolerates missing record file
  (legacy proposals from before TB-188 landed) — log a warning to
  events.jsonl and skip the per-proposal-record write; the
  operator_log line is still authoritative.
- Idempotent on uuid (existing operator-queue contract from TB-131).

Status surfacing (`ap2/cli.py` `cmd_status`):

- `ap2 status` text adds a line: `classifications last 30d:
  advanced-goal=<n>, pro-forma=<m>, unclear=<k>` when at least one
  classification exists in the last 30 days.
- `ap2 status --json` adds a `classifications_last_30d_by_verdict`
  dict with the three integer counts (always present, zeros when
  empty).

Module-level enum (`ap2/tools.py`):

- `DELETE_TEST_VERDICTS: tuple[str, ...] = ("advanced-goal",
  "pro-forma", "unclear")` — single source of truth, imported by
  CLI / drain / tests.

## Design

- Verdict enum is FIXED (3 values) by intent: the goal.md failure-
  mode statement defines two endpoints (advanced-goal vs pro-forma)
  and reserves `unclear` for proposals whose impact is not yet
  legible. Adding values later is a one-line tuple edit; expanding
  the enum operator-side is welcomed in the briefing's open
  questions.
- Reason field is optional but encouraged; matches TB-152's
  optional-`--reason` UX.
- Operator authority by design: no LLM auto-classification path.
  ap2/janitor.py-style judges are inappropriate here because the
  operator IS the source of truth for the delete-test verdict.

## Verification

- `uv run pytest -q ap2/tests/` — full regression gate passes.
- `uv run python -c "from ap2.tools import DELETE_TEST_VERDICTS; assert DELETE_TEST_VERDICTS == ('advanced-goal', 'pro-forma', 'unclear')"` — enum exposed and stable.
- New unit test in `ap2/tests/test_cli.py::test_classify_writes_operator_log_line`
  pins that `ap2 classify TB-N --delete-test advanced-goal --reason "..."`
  exits 0 and queues a `classify` record that drains to the expected
  operator_log.md line shape (`classified TB-N delete-test=advanced-goal: ...`).
- New unit test in `ap2/tests/test_cli.py::test_classify_invalid_verdict_exits_nonzero`
  pins that `ap2 classify TB-N --delete-test bogus` exits non-zero
  and does not queue any record.
- New unit test in `ap2/tests/test_operator_queue.py::test_classify_drain_appends_to_proposal_record`
  pins that draining a `classify` op appends a `delete_test` block
  (`verdict`, `classified_at`, `reason`) to
  `.cc-autopilot/ideation_proposals/<TB-N>.json`.
- New unit test in `ap2/tests/test_operator_queue.py::test_classify_drain_tolerates_missing_proposal_record`
  pins that draining a `classify` op for a TB-N without a per-
  proposal record on disk completes successfully (logs warning,
  appends operator_log line, no exception).
- New unit test in `ap2/tests/test_cli.py::test_status_renders_classifications_30d`
  pins that `ap2 status --json` includes
  `classifications_last_30d_by_verdict` with the three integer keys.
- New e2e test in `ap2/tests/e2e/test_tb189_mm_classify_routing.py`
  pins that the MM handler chat verb `classify TB-N pro-forma "<text>"`
  routes through `operator_queue_append` with `op="classify"` and
  the expected verdict/reason payload.
- `grep -nE "DELETE_TEST_VERDICTS" ap2/tools.py ap2/cli.py` — enum
  used at both surfaces.
- `grep -nE "classifications_last_30d_by_verdict" ap2/cli.py` —
  status surfacing wired in.

## Out of scope

- LLM auto-classification (operator authority by design — let the
  operator drive).
- Bulk-classify migrations for already-shipped proposals (operator
  classifies individually as cycles surface signal).
- Reclassification UI / undo (just append a new line; latest wins
  in any per-TB-N reduction).
- Prompt-header "Delete-test track record" block injection — depends
  on enough verdicts accumulating to be useful; a separate proposal
  in 2-3 cycles after this lands.
- Insight regeneration that aggregates verdicts into per-week
  trends — separate proposal (the TB-175-shaped follow-up).

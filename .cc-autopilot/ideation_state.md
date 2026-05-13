# Ideation State

_Last updated: 2026-05-13T12:38:00Z by ideation cron_

## Mission alignment

Code-quality consolidation still mission-aligned: 11 Completes against
the four axes (TB-203/204/205/206/207/208/209/210 shipped;
TB-211/212/213/215 in flight). Board: `0A / 0R / 5B / 0P / 84C / 3F`,
1 slot this cycle. The 3 most recent Completes still ground the testing
axis:

- TB-210 (`843b379`, 2026-05-13T07:33Z) — 4 env knobs (TB-208 debt closure).
- TB-209 (`1a54d14`, 2026-05-13T07:17Z) — CLI-verb 4th drift-gate surface
  + `_collect_cli_verbs` reusability extraction.
- TB-208 (`e2179b9`, 2026-05-13T01:35Z) — 3-surface coverage drift gate.

## Current focus assessment

- **Current focus: code quality (goal.md L38-97, four axes)**
  - Progress so far:
    - Docs axis: TB-203, TB-206, TB-207 — all four operator-facing
      registry surfaces have docs entries + drift gates.
    - Testing axis: TB-205, TB-208, TB-209, TB-210 shipped;
      TB-211/212/213/215 in flight close event-type + 8/12 CLI-verb
      coverage debt enumerated in `test_coverage_drift.py` L391-413.
      TB-214 IS DEAD-LETTER ON DISK (see Gap 1).
    - Reusability axis: TB-204 (`_briefing_fixtures.py`) + TB-209
      (`_source_registry.py`).
    - Cleanness axis: untouched (goal.md L86-87 anti-speculative-refactor
      guardrail).
  - Gaps:
    (1) **TB-214 dead-letter — operator hand-edit required.** Its title
        `Pin 4 sandbox install-* CLI verbs (...)` contains literal `*`
        (the `install-*` glob), which collides with `ap2/board.py`
        TASK_LINE_RE's `\*\*(?P<title>[^*]+)\*\*` group. Verified by
        parsing live `TASKS.md`: TB-214 lands in `Board.malformed_lines`,
        `Board.find('TB-214')` returns None, so operator-queue verbs
        `approve` / `update` / `delete` all KeyError. `ap2 status --json`
        shows `"pending_review": 4` and `"pending_review_ids": ["TB-211",
        "TB-212", "TB-213", "TB-215"]` against 5 Backlog tasks. Last
        events.jsonl ref: `board_malformed_line` at 2026-05-13T10:37:20Z.
        Decision needed below — only hand-edit fixes this specific row.
    (2) **No queue-append validator for `*` in `title`.** `_validate_single_line`
        (`ap2/tools.py` L126-139) rejects `\n`/`\r` per TB-134 but no
        other char that breaks TASK_LINE_RE. Any future ideation /
        operator add that names a glob/wildcard/footnote-marker literally
        in the title repeats TB-214's dead-letter trap. This cycle's
        single proposal (TB-216) closes the gate.
    (3) **Cleanness axis (untouched)** — goal.md L86-87 anti-speculative-
        refactor guardrail. Unchanged.
  - Status: `in-progress`
  - Reasoning: TB-214 is a generalizable parser-shape bug, not a one-off
    briefing typo — the cheapest fix is a TB-134-shape loud-reject at
    queue-append, with happy + error path tests pinning all three entry
    points (cmd_add, do_board_edit, do_operator_queue_append).
    Field-specific (title only) so existing description/tag/blocked
    values with `*` aren't retroactively rejected.

## Non-goal risk check

None. Validator extension stays inside ap2's validation + test
infrastructure; no drift into generic-task-scheduler / replace-
operator-judgment / multi-tenancy / real-time / cross-project axes.

## Considered & deferred this cycle

- **Cleanness module decomposition** — goal.md L86-87 guardrailed
  ("when the boundary becomes clear from reading — not via
  speculative refactor"). Unchanged.
- **Auto-sanitize `*` → `_` at write time** — silently rewrites operator
  intent; TB-134's docstring explicitly rejected this shape ("400-char
  run-on lines that nobody actually wanted"). Loud reject + actionable
  hint forces the right semantic split (move wildcard mention into
  briefing prose).
- **Parser refactor to tolerate `*` in titles** — bigger lift; TB-119
  (Frozen) is the tracker for the mistune-AST migration that subsumes
  this whole class. Defer until a second char class needs similar
  tolerance.
- **Surface malformed_line counts in `ap2 status`** — secondary
  hardening worth doing later, but not the gate fix; the validator
  prevents the entry rather than papering over the symptom.
- **n=4 authoritative rejects** (TB-172/175/184/185) — unchanged. The
  TB-216 proposal matches TB-134's loud-reject pattern (concrete
  observed failure in production), not the wack-a-mole / forecasting
  / parallel-surface / operator-intent-erosion shapes the rejects
  flagged.

## Cycle observations

- TB-214 finding: `board_malformed_line` events are operator-invisible
  unless they read `events.jsonl` directly. `ap2 status` shows board
  counts but the pending_review drop from 5 → 4 doesn't flag the
  parse failure. Carrying this observation: it informs both this
  cycle's gate choice (prevent at write rather than detect at read)
  AND a possible future hardening proposal — but only as a secondary
  layer once the gate ships and the question becomes "what slipped
  past the gate?"

## Decisions needed from operator

- Decision needed: TB-214 is dead-letter on disk (title contains `*`
  → parsed as malformed; `Board.find` returns None; queue verbs can't
  address it). Manual unblock: edit `TASKS.md` line 14, replace both
  `install-*` occurrences with an asterisk-free form (e.g.
  `install verbs` or `install_*` with the `_*` escaped). After the
  edit, `ap2 status --json` should re-show 5 IDs in
  `pending_review_ids` and `ap2 approve TB-214` will dispatch
  normally. Unblock-condition: the asterisk-bearing line stops
  blocking the sandbox install-* CLI-verb coverage closure that
  TB-214 was supposed to ship.

## Proposals this cycle

1 proposal (slots=1):

- TB-216: Extend `_validate_single_line` to reject titles containing
  `*` at queue-append time (TB-214-shape dead-letter prevention) —
  closes Gap (2).

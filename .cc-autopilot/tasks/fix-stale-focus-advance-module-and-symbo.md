# Fix stale focus_advance module and symbol references in source comments after the ideation-halt rename

Tags: #autopilot #cleanup #docs-drift #ideation-halt #comments

## Goal

TB-342 (collapse multi-focus rotation) + TB-345 (merge focus_advance
into core as `ideation_halt`) moved the empty-cycles detector to
`ap2/ideation_halt.py` and renamed its counter from
`_ideation_empty_against_focus` to `_consecutive_empty_ideation_cycles`.
Five comments/docstrings across the codebase still cite the removed
module path and/or the old symbol name:

- `ap2/events.py:128` — `_ideation_empty_against_focus` (symbol stale)
- `ap2/events.py:403` — `focus_advance._ideation_empty_against_focus`
  (module + symbol stale)
- `ap2/goal.py:521` — `_ideation_empty_against_focus` (symbol stale)
- `ap2/ideation.py:1135` — `_ideation_empty_against_focus` (symbol stale)
- `ap2/tools.py:1142` — `ap2/focus_advance.py:_ideation_empty_against_focus`
  (module + symbol stale)

These are comment-only references — no behavior depends on them — but
each points a future reader (or the agent) at a module and symbol that
no longer exist, which is exactly the kind of rot that bites right
before the OSS cut.

Why now: the broken `ap2/focus_advance.py` path resolves to nothing
since TB-345 deleted that module, so anyone following the comment hits
a dead end; cleaning it keeps the cross-reference web accurate while
the rename is fresh in the tree. Operator-directed 2026-05-29;
meta-infra comment cleanup with no active focus, so
`--skip-goal-alignment`. Depends on TB-345 having landed the rename,
so `@blocked:TB-345`.

## Scope

Comment/docstring edits only — no executable code changes.

- In each of the five sites above, update the reference to the current
  module + symbol: `ap2/ideation_halt.py` and
  `_consecutive_empty_ideation_cycles`. Preserve the surrounding
  explanatory prose and any TB-N citations (TB-292/TB-300 accounting
  history stays); only the module path and symbol token change.
- Do NOT modify the backward-compat alias
  `"AP2_FOCUS_ADVANCE_EMPTY_CYCLES": "core.ideation_halt_empty_cycles"`
  in `ap2/config_compat.py` — that mapping is intentional (it keeps
  the old env var working) and must stay.
- Do NOT touch `ap2/ideation.default.md` — its rotation-era references
  are handled by the companion ideation-prompt cleanup task.

## Design

- **Comment-only, zero behavior delta.** The detector itself
  (`ap2/ideation_halt.py`) is unchanged; this only corrects stale
  cross-references so they resolve. The full suite must stay green
  with no expectation changes.
- **Alias is not drift.** `config_compat.py`'s
  `AP2_FOCUS_ADVANCE_EMPTY_CYCLES → core.ideation_halt_empty_cycles`
  entry is a deliberate backward-compatibility shim (an operator who
  still sets the old env var keeps working), not a stale reference.
  It is explicitly preserved.

## Verification

- `uv run --extra dev pytest -q ap2/tests/` — full suite passes
  (canonical `AP2_VERIFY_CMD`, scoped to `ap2/tests/`); comment-only
  edits must not change any test outcome.
- `! grep -rnE "focus_advance\.py|_ideation_empty_against_focus" ap2/events.py ap2/goal.py ap2/ideation.py ap2/tools.py` — the stale module path and old symbol name are gone from all four source files.
- `grep -rnE "_consecutive_empty_ideation_cycles" ap2/events.py ap2/goal.py ap2/ideation.py ap2/tools.py` — the corrected symbol name is referenced in the updated comments.
- `grep -nE "AP2_FOCUS_ADVANCE_EMPTY_CYCLES" ap2/config_compat.py` — the intentional backward-compat alias is preserved (not removed by this cleanup).
- `ap2/tools.py` Prose: the comment near L1142 cites `ap2/ideation_halt.py` and the current `_consecutive_empty_ideation_cycles` symbol instead of the removed `ap2/focus_advance.py:_ideation_empty_against_focus`. Judge confirms via Read.

## Out of scope

- `ap2/ideation.default.md` rotation-era references — companion task.
- Renaming `_consecutive_empty_ideation_cycles` or any executable
  symbol — this task only fixes comments that point at it.
- The `config_compat.py` backward-compat alias — preserved, see Scope.

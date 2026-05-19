# Split `ap2/tools.py` (224KB / ~5000 LOC) by surface area into focused sibling modules

Tags: #autopilot #refactor #modularity #agent-friendliness #regression-pin

## Goal

`ap2/tools.py` is currently 224KB (~5000 LOC) — the largest module in the codebase by a 1.2x margin over `daemon.py` (187KB). It mixes at least five distinct surface areas:

- Briefing-structure validators (`_validate_briefing_structure`, goal-anchor matcher, Why-now check, Manual-bullet validator, section regexes — TB-138, TB-154, TB-161, TB-164, TB-171).
- Validator-judge dep-coherence (`_judge_dep_coherence_default`, `_check_dependency_coherence`, judge response parsing — TB-247).
- Operator-queue handlers (`do_operator_queue_append`, drain helpers — TB-131, TB-141, TB-142, TB-143).
- Board-edit operations (`do_board_edit` + helpers — TB-153).
- MCP tool dispatch + registration.

The codebase's "one concept per module" principle (articulated in `goal.md`'s code-quality arc and `_shared.py`'s threshold-three rule) says these should be separate modules. Concrete cost of the status quo: TB-261 (validator-judge JSON-extraction fix) loaded the full 224KB even though only ~30 LOC needed to change — context budget waste compounds across an agent's turns.

Goal anchor: this serves `goal.md` `## Done when` bullet "Failure recovery (verification fails, retries exhaust, daemon restart, cron drift, agent timeouts) is fully automatic; only genuine design forks escalate." Large modules → longer per-task agent runs → higher chance of verify timeout, max-turns, state-violation rollback. TB-247 / TB-250 / TB-255 all hit the 600s verify ceiling on tasks that touched these large surfaces; reducing per-module context load reduces that exposure class.

Why now: tools.py grew ~3x over the recent task arc. Every TB-N touching it pays the full context tax. Splitting it pays dividends on every subsequent task touching MCP dispatch, briefing validators, or the operator queue — that's the majority of tasks the daemon ships.

## Scope

- Split `ap2/tools.py` into ~4-5 focused modules at the flat `ap2/` level. Suggested split (final exact division is a Design call for the agent):
  - `ap2/briefing_validators.py` — `_validate_briefing_structure` + goal-anchor matcher + Why-now check + Manual-bullet validator + section regexes.
  - `ap2/validator_judge.py` — `_judge_dep_coherence_default` + `_check_dependency_coherence` + validator-judge response parsing.
  - `ap2/operator_queue.py` — `do_operator_queue_append` + drain helpers shared with daemon.
  - `ap2/board_edits.py` — `do_board_edit` + helpers.
  - `ap2/tools.py` (remains) — MCP tool dispatch + registration + anything that doesn't fit the above.
- Preserve every existing public symbol: re-export from `ap2/tools.py` for backward compat OR update all call sites in this same commit if re-exports add noise — agent picks the cleaner path.
- Update import sites across `daemon.py`, `cli.py`, `web.py`, `verify.py`, tests as needed.

## Design

- **Flat structure only** — NO `ap2/tools/` subpackage. Each split becomes a sibling module at `ap2/`. `goal.md` and `_shared.py`'s docstring both anchor the flat-structure principle.
- **One concept per module** — each new module owns one of tools.py's internal axes.
- **No new abstraction layers** — direct functions and dataclasses, same idioms as `_shared.py` / `events.py` / `config.py`.
- **Backward compat via re-export OR call-site update** — both patterns acceptable; tests pin the choice.
- **Resolve import cycles** by routing shared state through `config.py` / `_shared.py` (already there) rather than cross-importing between the new modules.
- **Mechanical move, not redesign** — the goal is to reduce per-module context size, not to redesign internal interfaces. Symbol names, signatures, and call contracts stay identical.

## Verification

- `uv run pytest -q` — full project suite passes (1789+ tests).
- `wc -c ap2/tools.py | awk '$1 < 80000 { exit 0 } { exit 1 }'` — `tools.py` reduced to under 80KB after the split.
- `ls ap2/briefing_validators.py ap2/validator_judge.py 2>/dev/null | wc -l | awk '$1 >= 2 { exit 0 } { exit 1 }'` — at minimum two of the suggested split modules exist (if the agent picks different names but the principle holds, this bullet should be re-stated to the agent's chosen names in a follow-up).
- `python3 -c "from ap2.tools import do_board_edit, do_operator_queue_append; from ap2.briefing_validators import _validate_briefing_structure"` exits 0 — public symbols importable from their canonical paths (whether direct or re-exported).
- prose: `ap2/tools.py` is split into at least three sibling modules at the flat `ap2/` level (NOT under `ap2/tools/`). Each new module owns one coherent surface area named in `## Scope`. The judge can verify by reading the new module files and confirming each has a single clear responsibility documented in its module docstring.
- prose: no MCP tool registration is dropped — the count of `mcp_register` / `@mcp_tool` decorators across `ap2/*.py` (non-test) is non-decreasing across the split. Each registration moved, not removed.
- prose: `ap2/tools.py`'s new contents are focused on one responsibility (MCP dispatch + registration), not a junk drawer. The judge confirms by reading the file's top-level structure.

## Out of scope

- Subpackage creation (`ap2/tools/`) — violates `goal.md`'s flat-structure principle.
- Behavior changes / new features — pure refactor. All public APIs stay.
- Cross-module abstraction layers (`BaseValidator` ABC, plugin registries) — direct function patterns.
- Splitting `daemon.py` / `cli.py` / `web.py` — separate TBs (next three in this batch).
- Refactoring internal function signatures inside the moved code — mechanical move only.
- Adding new validators / new MCP tools / new operator-queue ops alongside the split — those are separate concerns.

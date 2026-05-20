# Reconcile post-split doc/skill references: refresh architecture.md module map + fix moved-symbol citations in howto.md and skills/ap2-task/SKILL.md

Tags: #autopilot #docs #refactor #post-split #regression-pin

## Goal

The TB-262 / TB-263 / TB-264 / TB-265 module splits and TB-261's new `json_extract.py` rotted documentation references. `ap2/architecture.md`'s module map still shows the pre-split monolithic layout — it misses all ~20 new sibling modules and mis-attributes moved symbols (`do_board_edit` still listed under `tools.py` but now in `board_edits.py`; `_commit_state_files` under `daemon.py` but now in `state_commit.py`; `do_operator_queue_append` under `tools.py` but now in `operator_queue.py`). Scattered moved-symbol citations also rotted in `ap2/howto.md` (`_validate_briefing_structure` cited as `tools.py`; auto-approve gate cited as `tools.py`) and in the skill source `skills/ap2-task/SKILL.md` (`_validate_briefing_structure` in `ap2/tools.py`). This task reconciles those references with the post-split reality. The `@blocked` codespan records the source-split predecessors (all already Complete, so the blocker is satisfied).

Note scope boundary: the BEHAVIORAL doc content (env hot-reload, the 60s `AP2_VALIDATOR_JUDGE_TIMEOUT_S` default, new events, new knobs) already landed correctly alongside the code changes — this task is reference/location reconciliation only, not behavioral updates.

Goal anchor: serves `goal.md` `## Done when` bullet "an operator can point ap2 at a fresh project, paste a goal.md, and walk away for a week without intervention." A wholesale-stale architecture module map misleads any operator OR onboarding agent trying to understand the system — they look for `do_board_edit` in `tools.py`, don't find it, and lose time. Accurate structural docs are part of the legibility that makes hands-off operation possible.

Why now: the splits just landed (TB-262 through TB-265) and the architecture map is the single worst-affected doc — it's the canonical structural reference and is now entirely wrong about where code lives. Every day it stays stale, anyone reading it to navigate the (now much larger) module set is misdirected. Pairs naturally with the splits as their doc-reconciliation tail.

## Scope

- `ap2/architecture.md` — regenerate the module-map / file-tree section to reflect the flat post-split layout. Add the new sibling modules: `briefing_validators.py`, `validator_judge.py`, `operator_queue.py`, `board_edits.py` (from the `tools.py` split); `auto_approve.py`, `auto_unfreeze.py`, `state_commit.py`, `watchdog.py` (from `daemon.py`); `cli_daemon.py`, `cli_board.py`, `cli_review.py`, `cli_diagnostic.py` (from `cli.py`); `web_home.py`, `web_events.py`, `web_tasks.py`, `web_stats.py`, `web_insights.py`, `web_chrome.py`, `web_usage.py` (from `web.py`); and `json_extract.py`. Re-attribute moved symbols (`do_board_edit` → `board_edits.py`, `_commit_state_files` → `state_commit.py`, `do_operator_queue_append` → `operator_queue.py`, the auto-approve/auto-unfreeze logic → their modules) to their new homes.
- `ap2/howto.md` — fix moved-symbol path citations: `_validate_briefing_structure` (now `briefing_validators.py`, not `tools.py`); the auto-approve gate reference (now `auto_approve.py`, not `tools.py`); verify the `IMPACT_VERDICTS` "single source of truth" attribution still names the correct module. Bare prose mentions that don't imply a module path can stay.
- `skills/ap2-task/SKILL.md` — fix the `_validate_briefing_structure in ap2/tools.py` citation to `ap2/briefing_validators.py`. (This is the repo SOURCE; the deployed copy under `~/.claude/skills/` is refreshed by a separate operator-run deploy step — see Out of scope.)

## Design

- Reference/location reconciliation only — do NOT rewrite behavioral prose that's already correct. Confirm before changing any sentence that it's actually a stale module/symbol reference, not current behavior.
- For the architecture module map: mirror the actual `ap2/*.py` layout on disk (run `ls ap2/*.py`) so the map matches reality exactly, including the modules each split produced.
- Prefer symbol-and-module references (`do_board_edit in board_edits.py`) over file-line-number citations (`tools.py:684`), which rot on the next refactor.
- The skill SOURCE is what gets edited; the deployed copy at `~/.claude/skills/ap2-task/` is downstream of `scripts/deploy-skills.sh --apply`, an operator-run step outside this repo — NOT performed by this task.

## Verification

- `uv run pytest -q ap2/tests/` — full suite passes (including `test_deploy_skills.py`).
- `grep -qE "board_edits\.py" ap2/architecture.md && grep -qE "validator_judge\.py" ap2/architecture.md && grep -qE "auto_unfreeze\.py" ap2/architecture.md && grep -qE "web_stats\.py" ap2/architecture.md` — a representative module from each of the four splits now appears in architecture.md.
- `grep -qE "json_extract\.py" ap2/architecture.md` — `json_extract.py` is in the module map.
- `grep -oE "briefing_validators\.py|validator_judge\.py|operator_queue\.py|board_edits\.py|state_commit\.py|auto_approve\.py|auto_unfreeze\.py|watchdog\.py|cli_board\.py|web_home\.py" ap2/architecture.md | sort -u | wc -l | awk '$1 >= 8 { exit 0 } { exit 1 }'` — at least 8 distinct new split-module names are present in architecture.md.
- `! grep -qE "_validate_briefing_structure.{0,30}tools\.py" skills/ap2-task/SKILL.md` — the stale `tools.py` attribution for that symbol is gone from the skill source (`!` inverts grep so absence passes).
- `grep -qE "_validate_briefing_structure.{0,40}briefing_validators\.py" skills/ap2-task/SKILL.md` — the skill source now points the symbol at its new home.
- Prose: `ap2/architecture.md`'s module map reflects the full flat post-split layout (the four splits all represented as sibling modules, `json_extract.py` listed, and moved symbols `do_board_edit` / `_commit_state_files` / `do_operator_queue_append` attributed to `board_edits.py` / `state_commit.py` / `operator_queue.py` respectively). The judge confirms via Read of the module-map section against `ls ap2/*.py`.
- Prose: `ap2/howto.md` no longer attributes `_validate_briefing_structure` to `tools.py` (now `briefing_validators.py`) and the auto-approve-gate reference points to `auto_approve.py`. The judge confirms via Grep/Read.

## Out of scope

- Running `scripts/deploy-skills.sh --apply` — that writes to `~/.claude/skills/` OUTSIDE the repo and is an operator-run follow-on step, not a task-agent action.
- Editing `~/.claude/ap2-howto.md` — it has no repo source (it's a hand-maintained global doc) and is outside this repo; leave it.
- `ap2/ideation.default.md`'s `## Shell-bullet` section — already covered by the pending TB-273.
- Behavioral doc updates (env hot-reload, 60s timeout default, new events/knobs) — those already landed correctly with their code; this task is reference reconciliation only.
- Re-attributing every bare prose mention of a module name — only fix citations that imply a now-wrong file location.

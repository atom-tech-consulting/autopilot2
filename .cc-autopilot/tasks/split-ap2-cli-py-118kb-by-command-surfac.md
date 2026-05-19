# Split `ap2/cli.py` (118KB) by command surface: lifecycle / board / review / diagnostic groups

Tags: #autopilot #refactor #modularity #agent-friendliness #regression-pin

## Goal

`ap2/cli.py` is currently 118KB containing ~26 `cmd_<verb>` handlers covering wildly different surfaces — daemon lifecycle (`cmd_start`, `cmd_stop`, `cmd_status`, `cmd_pause`, `cmd_resume`, `cmd_web`), board mutation (`cmd_add`, `cmd_update`, `cmd_backlog`, `cmd_unfreeze`, `cmd_delete`, `cmd_reject`, `cmd_approve`, `cmd_classify`), review surfaces (`cmd_audit`, `cmd_ack`, `cmd_rollback`, `cmd_ideate`, `cmd_update_goal`, `cmd_backfill_proposals`), diagnostic (`cmd_doctor`, `cmd_check`, `cmd_logs`), and cron (`cmd_cron_list`, `cmd_cron_edit`). Each handler is its own self-contained surface; co-locating them just inflates the file.

The argparse dispatch + main entrypoint is small; ~95% of the file is per-verb handler bodies. Splitting by surface area lets tasks touching one verb group (e.g. board mutation TBs like TB-153, TB-260) load only the relevant module instead of the full 118KB.

Goal anchor: serves `goal.md` `## Done when` bullet "Failure recovery (verification fails, retries exhaust, daemon restart, cron drift, agent timeouts) is fully automatic; only genuine design forks escalate." Smaller per-module context loads = faster agent runs = lower verify-timeout exposure on CLI-touching tasks (which are a significant share of the daemon's work).

Why now: cli.py is the third-largest module and the easiest split of the four because its internal structure is already partitioned by verb. The split is nearly mechanical — group handlers by surface, lift to sibling modules, keep `cli.py` as the argparse dispatcher.

## Scope

- Keep `ap2/cli.py` as the argparse dispatcher: parser construction, subcommand registration, the `main()` entrypoint, and any utility functions used across handler groups.
- Lift `cmd_<verb>` handlers to focused sibling modules at the flat `ap2/` level. Suggested grouping (agent picks exact boundaries):
  - `ap2/cli_daemon.py` — `cmd_start`, `cmd_stop`, `cmd_status`, `cmd_pause`, `cmd_resume`, `cmd_web`.
  - `ap2/cli_board.py` — `cmd_add`, `cmd_update`, `cmd_backlog`, `cmd_unfreeze`, `cmd_delete`, `cmd_reject`, `cmd_approve`, `cmd_classify`.
  - `ap2/cli_review.py` — `cmd_audit`, `cmd_ack`, `cmd_rollback`, `cmd_ideate`, `cmd_update_goal`, `cmd_backfill_proposals`.
  - `ap2/cli_diagnostic.py` — `cmd_doctor`, `cmd_check`, `cmd_logs`, `cmd_cron_list`, `cmd_cron_edit`, `cmd_init`.
- Each handler keeps its existing signature `(cfg: Config, args: argparse.Namespace) -> int`.
- `ap2/cli.py`'s argparse setup imports the handlers from the new sibling modules and binds them with `parser.set_defaults(func=...)` as it does today.

## Design

- Flat structure only — NO `ap2/cli/` subpackage. Sibling modules at `ap2/`.
- `cli.py` remains the canonical CLI entrypoint — `python -m ap2 --project ... <verb>` resolution path unchanged.
- Each handler module exports its `cmd_<verb>` functions directly; `cli.py` imports them.
- No shared base class for handlers — direct functions, same idiom as `do_<op>` in `tools.py`.
- Briefing-parsing helpers used only by `cmd_add` and `cmd_update` move to whichever module ends up owning those handlers (likely `cli_board.py`), OR stay in `cli.py` if used cross-module.
- Editor-invocation fallback in `cmd_add` (TB-135's git-commit-style editor flow) moves with `cmd_add`.

## Verification

- `uv run pytest -q` — full project suite passes.
- `wc -c ap2/cli.py | awk '$1 < 40000 { exit 0 } { exit 1 }'` — `cli.py` reduced to under 40KB after the split (mostly just argparse + dispatch left).
- `ls ap2/cli_daemon.py ap2/cli_board.py 2>/dev/null | wc -l | awk '$1 >= 2 { exit 0 } { exit 1 }'` — at minimum two of the suggested sibling modules exist.
- `ap2 --help 2>&1 | grep -cE '^\s+(start|stop|status|add|update|backlog|unfreeze|delete|approve|reject|classify|audit|ack|rollback|ideate|doctor|check|logs|pause|resume|web|cron|init|update-goal|backfill-proposals)' | awk '$1 >= 25 { exit 0 } { exit 1 }'` — every CLI verb still registered (count is non-decreasing).
- `ap2 --project /tmp/nonexistent status 2>&1 | head -1 | grep -qE '(error|ERROR|not found)' && echo ok` — `cmd_status` still callable through the dispatcher (exits non-zero on a missing project, but argparse routing still works).
- prose: every `cmd_<verb>` function is exported from a sibling `cli_*.py` module and bound through `cli.py`'s argparse dispatcher. The judge can verify by reading `cli.py` and confirming all `parser.set_defaults(func=...)` calls reference imported handlers, not locally-defined ones.

## Out of scope

- Subpackage creation — flat-structure principle.
- Changing CLI surface (verb names, args, help text) — pure refactor.
- Adding new verbs alongside the split — separate concern.
- Consolidating argparse setup across new modules — argparse-builder stays in `cli.py`.
- Splitting `tools.py` / `daemon.py` / `web.py` — separate TBs in this batch.
- Refactoring handler internals beyond what's needed for the move — mechanical lift only.
## Attempts

### 2026-05-19 — verification_failed
(no summary)
- **kind:** per_task
- **failed_criteria:** [fail] `ap2 --project /tmp/nonexistent status 2>&1 | head -1 | grep -qE '(error|ERROR|not found)' && echo ok` — `cmd_status` st
- **Debug dumps:** `prompt: .cc-autopilot/debug/20260519T215004Z-TB-264.prompt.md`, `stream: .cc-autopilot/debug/20260519T215004Z-TB-264.stream.jsonl`, `messages: .cc-autopilot/debug/20260519T215004Z-TB-264.messages.jsonl`

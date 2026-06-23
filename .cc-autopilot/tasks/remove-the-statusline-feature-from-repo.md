# Remove the statusline feature from repo and code (it references a deleted hooks/ script and is not required by ap2)

Tags: #autopilot #sandbox #cleanup #statusline #cli #distribution

## Goal

Remove the statusline feature entirely. ap2 does not require it, and the script it
deploys (`hooks/statusline-command.sh`) no longer exists in the repo or the package
— `git ls-files` shows no `hooks/` tree and no `statusline-command.sh`, so
`_statusline_source()` is a dangling reference that prints "statusline source
missing: …/hooks/statusline-command.sh" during `ap2 sandbox user-setup` (and would
for any install). Strip the `ap2 sandbox install-statusline` verb, the
`install_statusline()` / `_statusline_source()` functions, the user-setup statusline
step + `--skip-statusline` flag, and the doc references. Operator-filed cleanup; no
goal.md focus anchor (filed `--skip-goal-alignment`).

Why now: a fresh `uv tool install` + `ap2 sandbox user-setup` (the distribution's
onboarding path) emits a spurious "statusline source missing" error, because the
install-statusline path can never succeed — the script isn't shipped or present
anywhere. Removing the vestigial, cosmetic feature stops the error and tidies the
setup surface for the public cut.

## Scope

- `ap2/sandbox.py`: remove `_statusline_source()`, `install_statusline()`, the
  `cmd_install_statusline` CLI handler, the `skip_statusline` parameter +
  `--skip-statusline` handling + the `if not skip_statusline: install_statusline(...)`
  block in the `user-setup` flow, and the statusline `settings.json` merge logic.
- Remove the `ap2 sandbox install-statusline` subcommand from the CLI parser
  (`build_parser`).
- Remove statusline doc references: the `install-statusline` row + the
  `--skip-statusline` mention in `ap2/README.md`, and the statusline mentions in
  `ap2/architecture.md`.
- Sweep `ap2/` (code + tests) for remaining `statusline` references and remove or
  repoint them; update any docs-drift / CLI-verb-coverage gate so the removed verb
  isn't expected. (No file to delete — `hooks/` and the script are already absent.)

## Design

- Pure removal of a cosmetic, non-functional feature — no change to the daemon loop.
  Keep every other `user-setup` step intact (token install, skills sync, AGENTS.md,
  Mattermost); only the statusline step goes.

## Verification

- `! grep -rIn "statusline" ap2/` — no statusline references remain in ap2/ source or docs (the `-I` skips binary `.pyc`).
- `! ap2 sandbox --help 2>&1 | grep -q "install-statusline"` — the `install-statusline` subcommand is gone from the CLI.
- `uv run --extra dev pytest -q ap2/tests/ --ignore=ap2/tests/smoke` — the full suite stays green (any statusline test removed/updated, not left failing).
- `ap2/sandbox.py` Prose: `install_statusline`, `_statusline_source`, the `install-statusline` verb, and the `--skip-statusline` flag are gone, and `user-setup` no longer attempts a statusline install; judge confirms via Read/Grep.

## Out of scope

- The other `user-setup` steps (token / skills / AGENTS.md / Mattermost).
- The AGENTS.md packaging fix (sibling task).
- Re-adding any statusline functionality — this is a removal.

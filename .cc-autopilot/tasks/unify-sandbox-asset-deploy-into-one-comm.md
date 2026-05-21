# Unify sandbox asset deploy into one command that syncs BOTH skills and howto, sudo-by-default with a --sbuser non-sudo mode

Tags: #autopilot #sandbox #deploy #cli #refactor #regression-pin

## Goal

Deploying ap2's Claude-Code assets to `~/.claude` is split across two inconsistent commands with two different mechanisms and two different target-user semantics:
- `ap2 sandbox sync-skills` (wraps `scripts/deploy-skills.sh`) — rsyncs `repo/skills/*` → `$HOME/.claude/skills/` of the CURRENT user, NO sudo.
- `ap2 sandbox install-howto [user]` — copies `ap2/howto.md` → `~<user>/.claude/ap2-howto.md` via `sudo -u <user> tee`, cross-user.

There's no reason to sync one asset but not the other, and no reason they use opposite write models. Consolidate into ONE command that deploys both skills and the howto in a single invocation, with a consistent, explicit user/sudo model.

Desired model (operator-specified):
- Default: sync from the original (operator) user using `sudo -u <sandbox-user>` to write into the sandbox user's `~/.claude/` (the `install-howto` cross-user model).
- `--sbuser`: allow the sandbox user to update its OWN `~/.claude/` directly, NO sudo (so a Claude session already running as the sandbox user — which lacks sudo — can refresh assets itself).
- Both modes deploy BOTH assets: `skills/*` AND `ap2/howto.md`.

Goal anchor: serves `goal.md` `## Done when` bullet "an operator can point ap2 at a fresh project, paste a goal.md, and walk away for a week without intervention." Sandbox provisioning (skills + howto deploy) is part of standing up a project; one consistent command with a self-service `--sbuser` mode reduces the setup + maintenance friction and removes a footgun where assets drift because one of two commands was forgotten.

Why now: the inconsistency just bit during a doc/skill reconciliation. A Claude session running AS the sandbox user (`claude-agent`, not in sudoers) could deploy the skills (`sync-skills` — no sudo) but NOT the howto (`install-howto` — needs sudo it lacks), forcing the operator to run the howto deploy manually as a separate step. A unified command with `--sbuser` lets the sandbox user deploy both in one shot.

## Scope

- Add a single `ap2 sandbox` verb (suggest `sync-assets` — name is the implementer's call) that deploys BOTH `repo/skills/*` → `<home>/.claude/skills/` AND `ap2/howto.md` → `<home>/.claude/ap2-howto.md` in one invocation.
- Modes:
  - Default (no `--sbuser`): takes a target sandbox-user positional arg (like `install-howto [user]`); writes both assets into `~<user>/.claude/` via `sudo -u <user>`.
  - `--sbuser`: writes both assets into the CURRENT user's `$HOME/.claude/` with NO sudo (the path a sandbox-user Claude session takes).
- Preserve the existing ergonomics: dry-run by default with a per-asset drift summary, `--apply` to write, `--dest` override for tests.
- Reconcile/retire the two old verbs: either alias `sync-skills` + `install-howto` to the unified command or remove them; update ALL callers — `ap2/cli.py` arg wiring, `ap2/sandbox.py` (`sync_skills`, `install_howto`, `cmd_*`), any internal callers (e.g. the `install-howto` call inside the sandbox setup flow at sandbox.py:426).
- Extend or absorb `scripts/deploy-skills.sh` so the howto rides the same path (or reimplement both in the Python sandbox helper — implementer's call), keeping the rsync `--delete` semantics for skills.
- Update tests: `ap2/tests/test_deploy_skills.py`, `ap2/tests/test_tb214_sandbox_install_verbs.py` (and any others pinning the old verbs).
- Update docs referencing the old verbs: the sandbox-setup sections of `ap2/README.md` / `ap2/howto.md` and the operator runbook.

## Design

- DECISION POINT for the implementer: the current `sync-skills` targets the OPERATOR's own home (its docstring says operators run `/ap2` etc. from their own session, so skills live under the operator's home), while `install-howto` targets the SANDBOX user's home. The unified model above targets the sandbox user's home for BOTH assets (default sudo, or `--sbuser` self-write). Confirm this is the intended consolidation — both skills and howto land in the SAME target user's `~/.claude/`, selected by the positional user arg (sudo) or `--sbuser` (self). If operators also need skills in their own home, that's the `--sbuser` case run as the operator, or simply running with their own username — document whichever resolution is chosen.
- One deploy function handling the {skills, howto} × {sudo-cross-user, sbuser-self} matrix. In sudo mode, write both via `sudo -u <user>` into the target home; in `--sbuser` mode, write both directly into `$HOME` (rsync skills, copy howto), no sudo.
- `--sbuser` and a positional `[user]` are mutually exclusive (sbuser means "current user is the target, skip sudo"); error clearly if both are given.
- Keep `roadmap`/no-back-compat-hack norms: prefer replacing the two verbs over indefinitely maintaining aliases, but if an alias is cheap and reduces operator surprise during transition, that's acceptable — document the choice.

## Verification

- `uv run pytest -q ap2/tests/` — full suite passes (with the deploy/install tests updated to the unified command).
- `ap2 sandbox --help 2>&1 | grep -qE "sync-assets|sync-claude|sync-all"` — a unified asset-sync verb is registered (adjust the alternation to the implementer's chosen name).
- Prose: the unified command deploys BOTH `skills/*` and `ap2/howto.md` in a single invocation (not two separate verbs). The judge confirms by reading the command's implementation and seeing both asset copies in one code path.
- Prose: the command supports a `--sbuser` flag that writes to the current user's `$HOME/.claude/` WITHOUT sudo, and a default (non-`--sbuser`) path that writes to a target user's home via `sudo -u <user>`. The judge confirms via Read of the mode branching.
- Prose: a regression-pin test exercises both modes against a `--dest`/temp target — `--sbuser` (no-sudo self-write) and the default path — and asserts both skills and the howto land. The judge confirms the test covers both assets in both modes.
- Prose: the old `sync-skills` / `install-howto` surfaces are reconciled (aliased or removed) and every internal caller (notably the sandbox setup flow that called `install_howto`) routes through the unified path. The judge confirms via Grep that no stale standalone-verb call remains unhandled.

## Out of scope

- Changing WHAT the assets are (the `skills/` set or `howto.md` content) — this is about the deploy mechanism only.
- Deploying assets other than skills + howto (e.g. statusline, settings.json) — those have their own `install-*` verbs; leave them, though a future TB could fold them in.
- Changing the rsync `--delete` semantics for skills (renames/deletions still propagate).
- Auto-running the sync on daemon start or on a cron — deploy stays an explicit operator/sandbox-user action.
- Adding sudoers entries for the sandbox user — `--sbuser` deliberately avoids needing sudo at all; do NOT modify sudoers.

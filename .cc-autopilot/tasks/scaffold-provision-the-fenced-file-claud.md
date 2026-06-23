# Scaffold + provision the fenced-file `.claude/settings.json` deny list (ap2 init writes it committed; project-setup backfills the clone)

Tags: #autopilot #init #sandbox #project-setup #fence #permissions #security

## Goal

Make every ap2-managed project carry a `.claude/settings.json` `permissions.deny`
block that denies `Edit`/`Write` on the daemon-fenced files, so a Claude Code task
agent cannot directly edit board state and corrupt it. Today the fence is enforced
only at the SDK layer via `--disallowedTools` derived from `TASK_AGENT_FENCED_PATHS`
(`ap2/tools.py:1281`); a fresh `ap2 init` does NOT scaffold a `.claude/settings.json`
deny list, so the project-settings defense layer this very repo relies on
(`.cc-autopilot`-managed `autopilot2/.claude/settings.json`) is missing from every
consumer project. Two writers must produce it: (1) `ap2 init` scaffolds a committed
`.claude/settings.json` deny block (so it travels with every `git clone` and also
protects local, non-sandbox `ap2 start` runs); (2) `ap2 sandbox project-setup`
defensively merges the same deny entries into the clone, backfilling repos that were
`init`-ed before this change. Both derive the deny entries from the single canonical
`TASK_AGENT_FENCED_PATHS` tuple — no second hand-maintained copy. Operator-filed
hardening; no goal.md focus anchor (filed `--skip-goal-alignment`).

Why now: an operator just stood up a fresh sandbox project (`ap2 init` + git +
`project-setup`) and it came up with no `.claude/settings.json` fence — the daemon's
task agents there can Edit `goal.md` / `TASKS.md` / `.cc-autopilot/` state directly,
exactly the corruption the fence exists to prevent. This repo ships the deny list as a
committed file; consumer projects must get the same protection automatically.

## Scope

- Add a single helper (e.g. in `ap2/tools.py` next to `TASK_AGENT_FENCED_PATHS`, or a
  small `settings`/sandbox helper) that renders the canonical `TASK_AGENT_FENCED_PATHS`
  tuple into the `permissions.deny` list shape — an `Edit(<path>)` and a `Write(<path>)`
  entry per fenced path. Both writers call this one renderer so the deny set can never
  drift from the SDK-layer fence.
- `ap2/init.py`: scaffold `.claude/settings.json` during `init_project` with
  `{"permissions": {"deny": [...]}}` from the renderer. If `.claude/settings.json`
  already exists, MERGE — union the deny entries into the existing `permissions.deny`
  (dedup, preserve every other key and any pre-existing deny/allow/ask entries); never
  clobber a user's settings. Track it in the init report/summary like the other
  scaffolded files. (init writes the file; the operator's commit picks it up so it
  travels via clone.)
- `ap2/sandbox.py` `project_setup`: after the clone, defensively merge the same deny
  entries into `<clone>/.claude/settings.json` (create if absent, union-merge if
  present), written as the sandbox user (same sudo-as-user discipline as the git-config
  / channel-env writes). Idempotent: a clone that already carries the committed file
  (init-produced) is a no-op. Surface the result in the project-audit/print output.
- Mirror the exact fenced set this repo uses today (`autopilot2/.claude/settings.json`)
  — it already equals `TASK_AGENT_FENCED_PATHS`; deriving from the tuple reproduces it.

## Design

- One renderer, two call sites — the canonical source stays `TASK_AGENT_FENCED_PATHS`,
  so the project-settings deny layer and the SDK `--disallowedTools` layer are
  guaranteed in lockstep.
- Merge, never overwrite: a consumer project may already have a `.claude/settings.json`
  with its own permissions/hooks. Union the deny entries into `permissions.deny` and
  leave everything else untouched.
- **Execution discipline.** Run verification in the FOREGROUND; do NOT
  `run_in_background` + poll. Iterate against the targeted new test; the daemon verifier
  runs the full suite after you report. Keep tool calls bounded.

## Verification

- `uv run --extra dev pytest -q ap2/tests/test_fenced_deny_settings.py` — a new test asserts: the renderer emits an `Edit(<p>)` AND a `Write(<p>)` entry for every `TASK_AGENT_FENCED_PATHS` element (and nothing outside that set); `init_project(tmp)` writes `tmp/.claude/settings.json` whose `permissions.deny` covers the full fenced set; and `init_project` on a tmp that already has a `.claude/settings.json` with an unrelated deny entry MERGES (the unrelated entry survives and the fenced entries are added).
- `uv run --extra dev pytest -q ap2/tests/ --ignore=ap2/tests/smoke` — the full suite stays green.
- `grep -q "TASK_AGENT_FENCED_PATHS" ap2/init.py` — `ap2 init` derives the deny list from the canonical fenced-paths set (whether directly or via the shared renderer it imports), not a second hand-maintained list.
- `ap2/sandbox.py` Prose: `project_setup` merges (create-or-union, never clobber) the fenced-file deny entries — rendered from `TASK_AGENT_FENCED_PATHS` — into the clone's `.claude/settings.json`, written as the sandbox user, idempotent against an init-produced committed file; judge confirms via Read.
- `ap2/init.py` Prose: `init_project` scaffolds (or union-merges into an existing) `.claude/settings.json` a `permissions.deny` block covering every `TASK_AGENT_FENCED_PATHS` entry as `Edit`/`Write` pairs, and reports it in the init summary; judge confirms via Read.

## Out of scope

- Changing `TASK_AGENT_FENCED_PATHS` itself or the SDK `--disallowedTools` fence layer
  (this adds the project-settings layer that mirrors it; it does not alter the set).
- Retrofitting this repo's own `.claude/settings.json` (already present and correct).
- Any allow/ask permission rules, hooks, or non-deny settings — only the fenced-file
  `deny` entries are managed.
- Re-cloning / re-syncing the operator's already-provisioned VM project (an operator
  action, not code).

# Add daemon_state.json to the ap2 init gitignore template + drift-gate test pinning every daemon-written .cc-autopilot file is committed-or-ignored

Tags: #autopilot #init #gitignore #drift-gate #regression-pin

## Goal

`ap2 init`'s `.cc-autopilot/.gitignore` template — `NESTED_GITIGNORE_BLOCKS` at `ap2/init.py:~230` — is missing `daemon_state.json`. TB-260 added that file (the env-file-mtime stash powering the env-stale warning) and the env-stale feature, but never added the gitignore entry. So every fresh `ap2 init` project inherits `daemon_state.json` as untracked-and-unignored, one stray `git add` from being committed — and it's pure runtime state that should never be (rollback must not restore a prior daemon-start mtime).

This is the source of a recurring whack-a-mole. TB-226's `focus_pointer.json` DID get added to the template; TB-260's `daemon_state.json` did NOT. Three runtime files turned up untracked-and-unignored in two days (an operator scratch file, `focus_pointer.json` — template-present but the self-hosted repo had drifted behind its own template — and `daemon_state.json` — genuinely absent from the template). The pattern: a new daemon-written `.cc-autopilot/` state file ships without its gitignore entry, and nobody notices until it shows up in `git status`.

Two parts: (1) add `daemon_state.json` to the template, and (2) add a drift-gate test that makes "new state file with no committed-or-ignored classification" a test failure, so this can't recur silently.

Goal anchor: serves `goal.md` `## Done when` bullet "an operator can point ap2 at a fresh project, paste a goal.md, and walk away for a week without intervention." A fresh project must scaffold a COMPLETE gitignore so daemon runtime state never accidentally lands in commits — that's part of clean hands-off operation, and the drift gate keeps it true as the daemon grows new state files.

Why now: this is the third such file in two days. The minimal patch (add `daemon_state.json`) fixes today's gap; the drift gate stops the class permanently — cheaper than re-discovering it on the next state file.

## Scope

- `ap2/init.py` — add `daemon_state.json` to `NESTED_GITIGNORE_BLOCKS` (the runtime-state group, next to `operator_queue_state.json` / `focus_pointer.json`), with a comment matching the siblings' rollback-rule rationale (TB-260 env-mtime stash; rewritten each daemon start; rollback must not restore it).
- New drift-gate test (e.g. `ap2/tests/test_state_file_gitignore_drift.py`) pinning the invariant: every file the daemon writes under `.cc-autopilot/` is EITHER committed (listed in `_STATE_FILE_NAMES`, so rollback restores it) OR ignored (present in the `NESTED_GITIGNORE_BLOCKS` template, so it's runtime-only) — never neither, never both. The test fails with a message telling the next author to classify their new state file into one bucket or the other.
- The drift gate needs a source of truth for "files the daemon writes under `.cc-autopilot/`". Prefer an explicit enumeration the test maintains (simpler and less fragile than scanning write-sites), cross-checked so a daemon-written file missing from BOTH `_STATE_FILE_NAMES` and the gitignore template trips the gate.

## Design

- Keep the settled rollback-rule split: committed (`_STATE_FILE_NAMES`: TASKS.md, progress.md, ideation_state.md, cron.yaml, retry_state.json, operator_log.md, tasks/, insights/) = rollback restores; ignored (cron_state / mm_state / auto_diagnose_state / operator_queue* / focus_pointer / daemon_state) = runtime, rollback must not touch.
- The test's value is the FAILURE MESSAGE: when a future TB adds a `.cc-autopilot/` state file, the gate should fail at CI time with "classify <file> as committed (_STATE_FILE_NAMES) or ignored (NESTED_GITIGNORE_BLOCKS)" — turning a silent drift into a loud, self-explaining gate. Model it on the existing drift gates (env-knob / MCP-tool / event-type / CLI-verb) that already enforce "every X is registered/documented".
- This repo's own `.cc-autopilot/.gitignore` was already reconciled this session (focus_pointer.json + daemon_state.json added directly) — this task changes the TEMPLATE and adds the gate, NOT this repo's already-correct copy.

## Verification

- `uv run pytest -q ap2/tests/` — full suite passes, including the new drift-gate test.
- `grep -qE "daemon_state\.json" ap2/init.py` — `daemon_state.json` is present in the init gitignore template.
- Prose: a new drift-gate test asserts every daemon-written `.cc-autopilot/` state file is classified as either committed (`_STATE_FILE_NAMES`) or ignored (`NESTED_GITIGNORE_BLOCKS`), never neither. The judge confirms the test enumerates `daemon_state.json` and would have FAILED before this task added it to the template (i.e. the gate actually catches the bug it's meant to prevent).
- Prose: the `daemon_state.json` entry in `ap2/init.py` carries a rollback-rule rationale comment (TB-260 runtime mtime stash, rewritten each daemon start, must not roll back), consistent with the neighboring `focus_pointer.json` / `operator_queue_state.json` comments.
- Prose: the drift-gate test's failure message names the remedy (classify the new file into `_STATE_FILE_NAMES` or the gitignore template), so a future author who adds a state file gets actionable guidance rather than a bare assertion error. The judge confirms via Read of the test.

## Out of scope

- This repo's own `.cc-autopilot/.gitignore` — already reconciled this session; don't touch.
- Re-running `ap2 init` on existing projects to union the new entry — that's an operator action; the idempotent union handles it whenever run.
- Reclassifying which files are committed vs ignored — the rollback-rule split is settled; this task only ensures completeness + a guardrail.
- Folding the drift gate into `ap2 check` / `ap2 doctor` as a runtime check — a pytest drift gate is sufficient; a runtime surface can be a follow-up if wanted.
- Adding root-level (above `.cc-autopilot/`) gitignore entries — scope is the nested daemon-state template only.

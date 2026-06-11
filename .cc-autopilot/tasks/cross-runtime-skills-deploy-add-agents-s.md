## Goal

This task advances goal.md's "Current focus: consolidate the operator manual into auto-triggered, cross-runtime skills" by making the deploy cross-runtime (axis 3): `sync_assets` (ap2/sandbox.py) gains a Codex/standard target (`~/.agents/skills`) alongside the existing Claude skills target, adds an `AGENTS.md` analog of the operator discovery pointer, and MANAGES the runtime skills-discovery pointers in code — closing the current gap where the skills are deployed but the discovery pointer is hand-maintained. It is additive and independent of the content carves.

Why now: the agentskills.io standard just converged across Claude + Codex and the codex backend just shipped, but today's deploy only targets the Claude skills directory and leaves the discovery pointer hand-edited — so a Codex operator session cannot discover the skills at all; this closes that gap and delivers the cross-runtime onboarding surface the OSS cut needs.

## Scope

- Extend `sync_assets` to also mirror `skills/*` into a `~/.agents/skills` destination under the same per-skill `rsync --delete` mechanism used for the Claude skills target.
- Add a repo-source `AGENTS.md` (the Codex analog of the operator discovery pointer) and deploy it to the Codex target.
- Have `sync_assets` MANAGE the runtime skills-discovery pointers in code: write/update a delimited pointer stanza (begin/end markers) in the deployed Claude-side and Codex-side pointer files at the user's home, idempotently — repeated runs must converge, not duplicate the stanza.
- Leave the existing Claude skills mirror and the `ap2-howto.md` target intact — dropping the howto target belongs to the later howto-retirement task, not here.
- Extend `ap2/tests/test_sync_assets.py` with coverage for the new target, `AGENTS.md`, and idempotent pointer management.

## Design

- Reuse `_skills_source()`; add an `agents_dir` destination parallel to the Claude `claude_dir`, sharing the per-skill rsync helper so renames/deletions propagate to both roots.
- Pointer management is a delimited-block rewrite keyed on begin/end markers in the deployed home-directory pointer files, so repeated `sync-assets` runs converge.
- The task agent edits only `ap2/sandbox.py` + `ap2/tests/test_sync_assets.py` + a new repo `AGENTS.md`; the pointer files it manages live under the target user's home at deploy time, not in the repo.

## Verification

- `grep -q '.agents/skills' ap2/sandbox.py` — the Codex `~/.agents/skills` target is wired into `sync_assets`.
- `find . -name 'AGENTS.md' -not -path './.git/*' | grep -q .` — an `AGENTS.md` source exists in the repo.
- `grep -q 'AGENTS' ap2/sandbox.py` — the deploy references/manages the `AGENTS.md` pointer.
- `ap2/tests/test_sync_assets.py` Prose: a new test asserts `sync_assets` mirrors skills into the `~/.agents/skills` target and writes/updates the discovery pointer idempotently (a second run produces no duplicate stanza); judge confirms via Read.
- `uv run --extra dev pytest -q ap2/tests/ --ignore=ap2/tests/smoke` — full suite green.

## Out of scope

- Dropping the `ap2-howto.md` sync target (deferred to the howto-retirement task, after the carves land).
- Carving any howto content into skills (TB-397 / TB-398 / TB-399 / TB-400).
- Editing any repo-fenced operator file directly; pointer management happens in `sandbox.py` deploy code against home-directory targets.

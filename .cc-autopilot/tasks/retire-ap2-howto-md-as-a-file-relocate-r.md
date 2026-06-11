## Goal

Retire `ap2/howto.md` as a standalone operation-manual file — the final step of the project's **Current focus: consolidate the operator manual into auto-triggered, cross-runtime skills**. After TB-404 lands, every operator-domain section has been carved into a skill and howto.md holds only orientation/core prose (What ap2 is, On-disk layout, What the daemon does each tick, Verification summary, Sandbox model, Convergence model, Reading order) plus carved-section pointer stubs. Relocate that residual content to its proper home, drop the `ap2-howto.md` deploy target, flip the residual `HOWTO_PATH`-keyed test gates, and delete the file.

Why now: `ap2/howto.md` surviving as the canonical operation manual is exactly the focus's delete-test failure ("if howto.md survives as the canonical operation manual, the consolidation didn't happen"); the deploy code already flags this step in-line ("the `ap2-howto.md` target stays until the later howto-retirement task", `ap2/sandbox.py`), so the surface is only half-retired until the file is gone.

## Scope

- Relocate each residual orientation/core howto section to a proper home with NO operator-operation knowledge lost. Design prose (What ap2 is, On-disk layout, What the daemon does each tick, Convergence model, Sandbox model) belongs in `ap2/architecture.md` (the standalone design doc — fold in only what it doesn't already cover); operator-facing orientation / reading order belongs in the top-level `skills/ap2/SKILL.md`; the "Verification — what the daemon checks before Complete" operator summary belongs alongside the ap2-task verification reference (cross-reference, don't duplicate). Pick one destination per section and record it.
- Drop the `ap2-howto.md` target from `ap2/sandbox.py`'s `sync_assets` deploy (the `_howto_source` read, the `howto_dest` / `ap2-howto.md` tuple entry, and the CLAUDE.md / AGENTS.md pointer-stanza text that references `ap2-howto.md`); update `ap2/tests/test_sync_assets.py` to match.
- Flip every residual `HOWTO_PATH` / `ap2/howto.md` reference in `ap2/tests/` onto the skill (or `architecture.md`) that now owns that content; remove the `HOWTO_PATH` constant once unused.
- Fix any skill or doc cross-reference that points at `ap2/howto.md` (now deleted) to point at the owning skill / architecture.md.
- Delete `ap2/howto.md`.

## Design

This is the focus's terminal delete-test and is hard-sequenced after TB-404 (the last carve) via `@blocked:review,TB-404`, so it never runs while substantive operator-domain content still lives in howto. It is a docs/tooling-only change — no daemon behavior moves. Keep the relocation faithful: the goal is "no knowledge lost, just relocated," not a rewrite. The project-wide gate (`AP2_VERIFY_CMD`) runs the full suite, so any missed `HOWTO_PATH` reference surfaces there even if not enumerated in the bullets below.

## Verification

- `! test -f ap2/howto.md` — the file is deleted (absence check: passes iff the path does not exist).
- `! grep -qF "ap2-howto.md" ap2/sandbox.py` — no `sync_assets` deploy target references the retired howto (absence check: passes iff the string is absent).
- `uv run --extra dev pytest -q ap2/tests/test_sync_assets.py` — the deploy test passes with the `ap2-howto.md` target dropped.
- `uv run --extra dev pytest -q ap2/tests/test_docs_drift.py` — the docs-drift gates pass after every `HOWTO_PATH` gate is flipped onto its skill / architecture.md.
- `uv run --extra dev pytest -q ap2/tests/test_docs.py` — the docs-structure test passes after relocation.
- `ap2/architecture.md` Prose: the residual orientation/core howto sections (What ap2 is, On-disk layout, daemon tick, Verification summary, Sandbox model, Convergence model, Reading order) are each relocated to architecture.md or a skill with no operator knowledge lost; judge confirms each former section has a destination via Read/Grep.

## Out of scope

- Any change to daemon runtime behavior — this is a pure docs/deploy retirement.
- New skill domains beyond relocating the residual sections; the ~6-9 domain-skill set is already established by TB-397..404.

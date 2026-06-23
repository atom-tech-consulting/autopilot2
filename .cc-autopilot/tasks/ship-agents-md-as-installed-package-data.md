# Ship AGENTS.md as installed package data so sync-assets deploys it after `uv tool install` (mirror TB-422 skills fix)

Tags: #autopilot #packaging #sandbox #agents-md #codex #distribution #sync-assets

## Goal

Make the Codex operator-reference `AGENTS.md` deployable by `ap2 sandbox sync-assets`
/ `user-setup` after a `uv tool install` / `pip install`, mirroring TB-422's skills
fix. Today `_agents_md_source()` (`ap2/sandbox.py:631`) resolves
`Path(__file__).resolve().parent.parent / "AGENTS.md"` (repo root), and `AGENTS.md`
is not installed package data, so after a non-editable install it resolves to a
nonexistent `site-packages/AGENTS.md` → "sync-assets: AGENTS.md source missing"
during `user-setup`. Relocate it under the package and resolve via
`importlib.resources`, exactly as TB-422 did for `skills/`. Operator-filed packaging
fix; no goal.md focus anchor (filed `--skip-goal-alignment`).

Why now: a fresh `uv tool install` + `ap2 sandbox user-setup` prints "AGENTS.md
source missing", so the Codex-runtime discovery pointer (`~/.agents/AGENTS.md`)
can't be deployed and a Codex operator session won't auto-discover the skills. This
is the same gap TB-422 closed for the skill bundles; this finishes the cross-runtime
asset packaging for the distribution cut.

## Scope

- Relocate `AGENTS.md` to `ap2/AGENTS.md` so it ships inside the installed package.
  The existing `[tool.setuptools.package-data]` `ap2 = ["*.md", …]` glob already
  covers `ap2/*.md` — confirm `ap2/AGENTS.md` lands in the wheel (add an explicit
  entry only if the glob does not cover it).
- Update `_agents_md_source()` to resolve from the installed package via
  `importlib.resources.files("ap2") / "AGENTS.md"`, with a repo-relative fallback for
  editable / dev checkouts — mirroring `_skills_source()` post-TB-422.
- Update references to the old repo-root location: the `_agents_md_source` docstring
  + the sync-assets comments in `ap2/sandbox.py`, the `ap2/README.md` `sync-assets`
  row (which says "repo `AGENTS.md`"), and the "What's in this repo" tree (list
  `ap2/AGENTS.md` alongside `ap2/skills/`).
- Preserve `sync-assets` deploy behavior unchanged otherwise (→ `~/.agents/AGENTS.md`
  plus the managed discovery-pointer stanza).

## Design

- Mirror TB-422 exactly: relocate under `ap2/` + `importlib.resources` resolution +
  editable fallback. No new packaging mechanism — the `*.md` package-data glob
  already ships `ap2/*.md`.
- Whether the ap2 repo keeps a separate contributor-facing root `AGENTS.md` is out of
  scope; this task ships the DEPLOYED operator reference.

## Verification

- `test -f ap2/AGENTS.md` — AGENTS.md now lives under the package (shipped as data).
- `uv run --extra dev pytest -q ap2/tests/test_agents_md_packaging.py` — a new test asserts `_agents_md_source()` resolves to an existing `AGENTS.md` via the installed-package resolver (with an editable fallback), mirroring TB-422's skills-packaging test.
- `uv run --extra dev pytest -q ap2/tests/ --ignore=ap2/tests/smoke` — the full suite stays green.
- `ap2/sandbox.py` Prose: `_agents_md_source()` resolves `AGENTS.md` from the installed `ap2` package via `importlib.resources` (repo-relative fallback for editable installs), and it ships as package data, so `ap2 sandbox sync-assets` / `user-setup` deploys `~/.agents/AGENTS.md` after a bare `uv tool install`; judge confirms via Read.

## Out of scope

- The statusline removal (sibling task).
- Changing what `AGENTS.md` contains or how `sync-assets` deploys it (target /
  discovery pointer) beyond the source-location resolution.
- A separate contributor-facing root `AGENTS.md` for the ap2 repo itself.

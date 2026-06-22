# Ship operator skills with the installed package so sync-assets works after `uv tool install` (not only from a repo clone)

Tags: #autopilot #packaging #skills #distribution #sandbox #sync-assets

## Goal

Make the operator skills (`skills/ap2-*`) deployable by `ap2 sandbox sync-assets`
after a `uv tool install` / `pip install`, not only from an editable / git-clone
checkout. Today `_skills_source()` (`ap2/sandbox.py:601-606`) resolves the skills
tree as `Path(__file__).resolve().parent.parent / "skills"` — i.e. `<repo>/skills/`
relative to the module. After a non-editable install, `ap2/sandbox.py` lives in
site-packages, so that resolves to `site-packages/skills`, which does NOT exist:
`skills/` is a top-level dir (not a declared package — `pyproject` `packages` is
`ap2` / `ap2.adapters` / `ap2.tests*`), and `MANIFEST.in graft skills` only
populates the sdist tarball, not the installed tree. Result: the README's
recommended install (`uv tool install`) gives the daemon + CLI but cannot deploy
the auto-triggered operator manual. Operator-filed packaging fix for the
distribution cut; no goal.md focus anchor (filed `--skip-goal-alignment`).

Why now: the README recommends `uv tool install` as the primary install and now
carries a caveat that the skills need a clone — a real gap for the source-available
distribution: a user installing the recommended way can't get the operator skills,
and the caveat is a band-aid over a fixable packaging defect.

## Scope

- Make the `skills/` tree ship inside the installed `ap2` package so it is present
  after `uv tool install` / `pip install` (not only the sdist). The natural
  mechanism is relocating the tree under the package as `ap2/skills/` (package data
  via `[tool.setuptools] packages`/`package-data`); an equivalent setuptools
  mechanism that lands the tree in the wheel is acceptable — implementer's call.
- Update `_skills_source()` (`ap2/sandbox.py`) to resolve the skills tree from the
  INSTALLED package (e.g. `importlib.resources.files("ap2") / "skills"`), with a
  fallback to the repo-relative path for editable / dev checkouts, so `sync-assets`
  works in BOTH install modes.
- Update every reader/reference that assumes the old top-level `skills/` location:
  the `MANIFEST.in` graft, any docs-drift / coverage gate that scans `skills/`, the
  README "What's in this repo" tree, and the README skills-need-a-clone caveat
  (which can then be removed once `uv tool install` deploys skills).
- Preserve `sync-assets` behavior unchanged otherwise (deploy to `~/.claude/skills`
  + `~/.agents/skills`, the managed discovery pointer).

## Design

- `importlib.resources.files("ap2")` resolves correctly for both installed and
  editable installs; keep a defensive fallback to the repo-relative path so a bare
  source checkout that hasn't been `pip install`-ed still resolves.
- If relocating to `ap2/skills/`, do it as a clean move and fix all readers in the
  same change so no gate references a stale path.
- **Execution discipline.** Run verification in the FOREGROUND; do NOT
  `run_in_background` + poll. Iterate against targeted tests; the daemon verifier
  runs the full suite after you report. Keep tool calls bounded.

## Verification

- `grep -qE "skills" pyproject.toml` — the packaging metadata declares the skills tree as installed package data (not only the sdist `MANIFEST.in` graft).
- `uv run --extra dev pytest -q ap2/tests/test_skills_packaging.py` — a new test asserts `_skills_source()` (or its replacement) resolves to an existing directory that contains the operator skills (e.g. an `ap2-board-ops` / `ap2-task` `SKILL.md`), via the installed-package resolver with an editable fallback.
- `uv run --extra dev pytest -q ap2/tests/ --ignore=ap2/tests/smoke` — the full suite (incl. any docs-drift / skills-coverage gate updated for the new location) stays green.
- `ap2/sandbox.py` Prose: `_skills_source()` resolves the skills tree from the installed `ap2` package (`importlib.resources`) with a repo-relative fallback, and the skills ship as package data so `ap2 sandbox sync-assets` can deploy them after a `uv tool install`; judge confirms via Read.

## Out of scope

- Changing what the skills CONTAIN or how `sync-assets` deploys them (targets /
  discovery pointer) beyond the source-location resolution.
- The daemon's own runtime (it does not read skills; `setting_sources=["project"]`).
- The README prose beyond removing/softening the now-obsolete clone caveat.

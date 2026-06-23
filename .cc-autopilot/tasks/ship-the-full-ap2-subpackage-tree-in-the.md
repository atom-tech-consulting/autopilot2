# Ship the full ap2 subpackage tree in the wheel via setuptools autodiscovery (fixes `ModuleNotFoundError: ap2.components` on non-editable installs)

Tags: #autopilot #packaging #distribution #pyproject #components #blocker

## Goal

Make every `ap2` subpackage ship in the built wheel so `ap2 init` / `ap2 start`
work after a non-editable `uv tool install` / `pip install`. Today
`[tool.setuptools] packages` in `pyproject.toml` is a hand-maintained list —
`["ap2", "ap2.adapters", "ap2.tests", "ap2.tests.e2e", "ap2.tests.smoke"]` — that
omits the entire `ap2.components` tree (9 packages: the parent plus attention,
auto_approve, auto_unfreeze, communication, cron, ideation, janitor, mattermost),
which was extracted during the components refactor but never added. The registry
imports `ap2.components` at startup (`registry.py` `Registry.discover()` →
`importlib.import_module`), so on a non-editable install where site-packages lacks
the tree it raises `ModuleNotFoundError: No module named 'ap2.components'` and every
command that builds the default registry (`ap2 init`, `ap2 start`) crashes. Replace
the manual list with setuptools autodiscovery so the wheel always carries the whole
`ap2` package tree and future extractions can't silently fall out. Operator-filed
packaging fix; no goal.md focus anchor (filed `--skip-goal-alignment`).

Why now: this is a hard release blocker for the source-available distribution cut —
a fresh `uv tool install` followed by `ap2 init` (the documented onboarding path)
crashes immediately with `ModuleNotFoundError: No module named 'ap2.components'`,
reproduced on a real VM. Editable operator checkouts mask it because `ap2.components`
resolves from the repo tree, so it only surfaces on the install path real users take.

## Scope

- `pyproject.toml`: replace the manual `[tool.setuptools] packages = [...]` list with
  setuptools autodiscovery — `[tool.setuptools.packages.find]` with
  `include = ["ap2", "ap2.*"]` — so all 14 current packages (incl. the full
  `ap2.components.*` tree) ship in the wheel, and any future subpackage is picked up
  automatically. Preserve the existing `[tool.setuptools.package-data]` `ap2` glob
  (skills/AGENTS.md/markdown) unchanged.
- Add a regression test (`ap2/tests/test_packaging_completeness.py`) that walks `ap2/`
  for every directory containing an `__init__.py`, computes the package set the
  effective `pyproject` find-config would ship (via `setuptools.find_packages` with
  the same include/exclude), and asserts every on-disk package is covered — failing if
  any subpackage (e.g. `ap2.components.ideation`) is omitted. This guards the
  PACKAGING DECLARATION, not a runtime `import` (a bare import passes trivially in an
  editable checkout and would not catch the regression).

## Design

- Mirror the TB-422/TB-424 packaging-fix posture: fix the declaration + add a
  declaration-level regression test, no daemon-loop change.
- Autodiscovery (`packages.find`) is preferred over re-enumerating the 11 packages by
  hand, since the manual list is exactly what rotted here.
- **Execution discipline.** Run verification in the FOREGROUND; do NOT
  `run_in_background` + poll. Iterate against the targeted new test; the daemon
  verifier runs the full suite after you report. Keep tool calls bounded.

## Verification

- `grep -q "tool.setuptools.packages.find" pyproject.toml` — packaging uses setuptools autodiscovery, not a hand-maintained `packages` list.
- `uv run --extra dev pytest -q ap2/tests/test_packaging_completeness.py` — the new regression test asserts every on-disk `ap2` package dir is covered by the effective find-config (fails if any `ap2.components.*` subpackage is omitted).
- `rm -rf dist && uv build --wheel && python3 -c "import glob,zipfile; names=zipfile.ZipFile(glob.glob('dist/autopilot2-*.whl')[0]).namelist(); comp=[n for n in names if n.startswith('ap2/components/')]; assert any('ideation' in n for n in comp), comp; print('components files in wheel:', len(comp))"` — the built wheel actually contains the `ap2.components` tree.
- `uv run --extra dev pytest -q ap2/tests/ --ignore=ap2/tests/smoke` — the full suite stays green.
- `pyproject.toml` Prose: `[tool.setuptools.packages.find]` autodiscovers `ap2` + `ap2.*` so the full `ap2.components` subpackage tree ships in the wheel, and `test_packaging_completeness.py` guards every package dir against future omission; judge confirms via Read.

## Out of scope

- Whether `ap2.tests*` should ship in the public wheel at all (current behavior ships
  them; autodiscovery preserves that — narrowing the distribution is a separate call).
- The skills / AGENTS.md package-data globs (TB-422/424; already correct — leave them).
- Any runtime/registry code change — `ap2.components` imports fine in editable
  checkouts; the defect is purely the wheel's package manifest.
## Attempts

### 2026-06-23 — error
(no summary)
- **error:** Exception: Claude Code returned an error result: success
- **stderr_tail:** 
- **Debug dumps:** `prompt: .cc-autopilot/debug/20260623T064012Z-TB-425.prompt.md`, `stream: .cc-autopilot/debug/20260623T064012Z-TB-425.stream.jsonl`, `messages: .cc-autopilot/debug/20260623T064012Z-TB-425.messages.jsonl`

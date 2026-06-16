## Goal

Make the source distribution carry the committed top-level `skills/` tree and the
docs, not just the wheel — the packaging-completeness half of axis 1 under
goal.md's **Current focus: cut a public source-available distribution**. ap2
builds with setuptools and has no MANIFEST.in, and `skills/` is not a Python
package, so today an `sdist` omits the operator-manual skills an outside consumer
needs. Add a MANIFEST.in (the setuptools sdist mechanism) that grafts `skills/`
and the docs into the source distribution, and pin it with a hermetic packaging
test. Serves the focus delete-test clause "or the sdist omits the skills/docs —
the wiring didn't happen."

Why now: the committed top-level `skills/` tree is ap2's operator manual, yet it
ships in neither the wheel package-data (`ap2/*.md,*.yaml` only) nor any sdist
manifest — so a clean checkout's built source distribution would silently drop
the manual an outside consumer needs.

## Scope
- Add a `MANIFEST.in` at the repo root that includes the committed top-level
  `skills/` tree (e.g. `graft skills`) and the top-level docs an outside consumer
  needs (e.g. `README.md`, `LICENSE`, `CHANGELOG.md`, and `ap2/architecture.md`
  if not already covered by package-data).
- Extend the hermetic packaging gate (`ap2/tests/test_packaging.py`) with a test
  that reads `MANIFEST.in` and asserts `skills/` and the docs are
  grafted/included — no network, no actual build, mirroring the file's existing
  parse-only pattern.

## Design
- setuptools sdist reads MANIFEST.in for non-package files; `skills/` is not a
  Python package, so package-data cannot carry it — MANIFEST.in is the correct
  mechanism.
- Keep the test hermetic (read MANIFEST.in as text); a live `python -m build`
  sdist tarball smoke is out of scope (operator/CI).

## Verification
- `test -f MANIFEST.in` — a MANIFEST.in exists at the repo root.
- `grep -q "skills" MANIFEST.in` — the manifest references the skills tree.
- `uv run --extra dev pytest -q ap2/tests/test_packaging.py` — the extended hermetic packaging gate passes.
- `MANIFEST.in` Prose: the manifest grafts the committed top-level `skills/` tree and the docs (README/LICENSE and the architecture doc) into the source distribution; judge confirms via Read.

## Out of scope
- A live `python -m build --sdist` tarball-contents smoke (operator/CI).
- pyproject `license`/identity metadata changes (separate tasks).
- Wheel package-data restructuring beyond what's needed to include the docs.
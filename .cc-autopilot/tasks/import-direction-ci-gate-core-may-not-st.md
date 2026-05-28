# Import-direction CI gate

## Goal

Pin the structural cleavage required by the **Current focus: refactor
features into opt-in components**. Goal.md L57-59 makes the cleavage a
hard rule: "A CI gate fails the build if any core module directly
imports from `ap2/components/<name>/`. All cross-references flow
through the registry's hook protocol." This TB lands that gate as a
pytest the regression suite always runs. Without it, the cleavage
erodes silently — a future refactor accidentally re-couples core to a
component (e.g. someone adds `from ap2.components.janitor import X`
to a tick handler for a "quick fix" reason) and nobody notices until
a downstream OSS-distribution attempt discovers the leak. The
Done-when bullet "A CI gate fails the build if any core module
directly imports from `ap2/components/<name>/`" (goal.md L57-59) is
satisfied by this work.

Why now: lands incrementally per goal.md L219-221 ("(6) lands
incrementally — the disabled-config test gets re-run after each
migration; the import-direction gate lands once the first component
is in `ap2/components/`"). The moment a component exists in
`ap2/components/`, the import-gate should exist too, before any
axis-(5) migration can leak. Pinning the cleavage at the canary stage
is much cheaper than discovering accumulated leaks later.

## Scope

- Add `ap2/tests/test_core_import_direction.py`. The test walks every
  `.py` file under `ap2/` EXCEPT files under `ap2/components/` and
  `ap2/tests/`, parses each via `ast.parse`, and asserts that no
  `Import` or `ImportFrom` node references `ap2.components` (absolute)
  or `.components` / `..components` (relative).
- The registry's discovery layer (the module that walks
  `ap2/components/*/manifest.py`) is EXEMPT — it must import the
  components package to walk it. Declare the exemption by a
  small `_EXEMPT_FILES` tuple in the test (path-keyed, not
  pattern-keyed, for explicit auditability). Document the
  exemption in the test docstring + a code comment so future
  readers know why the exemption exists.
- The test reports every violation (path + line + offending
  statement) on failure, not just the first — so a refactor that
  introduces multiple leaks gets a complete fix list in one
  pytest run, not a one-at-a-time game of whack-a-mole.
- The test handles both static import forms (`from
  ap2.components.X import Y`, `import ap2.components.X`) and the
  relative form (`from ..components.X import Y`, `from .components.X
  import Y`). Dynamic imports via `importlib.import_module(...)`
  are NOT caught by the gate by design — the registry uses them
  intentionally; the test docstring documents this exemption.

## Design

AST-based detection (not regex over file contents) is the right
shape because:
- Comments and docstrings can mention `from ap2.components.X import Y`
  without it being an actual import (e.g. this very briefing's prose);
  regex would flag those as false positives.
- Multi-line imports / `if TYPE_CHECKING:` guarded imports are
  unambiguously parseable by `ast` but messy for regex.
- Relative vs absolute imports are distinguishable cleanly via
  `ImportFrom.level` (0 for absolute, >=1 for relative).

The exemption set is path-keyed (not pattern-keyed) for explicit
auditability: a future reader can see exactly which files are
allowed to import from components and why. Today the exemption is
the registry module alone; if axis (2) or axis (3) work introduces
another necessary direct importer, the exemption is widened
explicitly in the test, not by relaxing the pattern.

Dynamic-import exemption is intentional: the registry MUST use
`importlib.import_module` to discover components without a hardcoded
list. Static-import detection is the cleavage; runtime-import is
the mechanism.

## Verification

- `uv run pytest -q` — full suite passes (the new test joins the
  default regression set, so this implicitly proves the gate test
  itself passes against the current tree).
- `uv run pytest -q ap2/tests/test_core_import_direction.py` —
  the gate test passes in isolation (the only direct importer of
  `ap2.components.*` is the registry module, which is in the
  exempt set).
- `test -f ap2/tests/test_core_import_direction.py` — gate file
  exists at the expected path.
- A unit test inside the same file
  (`test_core_import_direction.py`) that synthesizes a fake `.py`
  file containing `from ap2.components.janitor import X` and
  confirms the detector flags it — proves the detector actually
  catches leaks, not just passes vacuously. Pin via
  `uv run pytest -q ap2/tests/test_core_import_direction.py -k detector_catches_synthetic_leak`.
- `ap2/tests/test_core_import_direction.py` Prose: the
  implementation uses `ast.parse` (not regex over file contents)
  to detect imports, and handles both absolute (`Import` /
  `ImportFrom` with `module='ap2.components.X'`) and relative
  (`ImportFrom` with `level>=1` and `module='components.X'`) forms
  — judge confirms via Read.

## Out of scope

- Toggle-correctness test (`test_components_disabled.py`) —
  separate axis (6) TB; lands once enough components have env
  flags to make a meaningful "all disabled" config (probably after
  3-4 axis-(5) migrations land).
- Detecting dynamic imports via `importlib.import_module(...)` —
  by design exempt; the registry uses them intentionally.
- Any component migrations themselves (each is its own TB-N).
- Adding a separate CI step outside pytest; the gate is a pytest
  test in the default suite, no separate CI plumbing.

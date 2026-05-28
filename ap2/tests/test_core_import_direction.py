"""TB-311: import-direction CI gate — core may not statically import from
`ap2/components/` (axis 6 partial).

Pins the structural cleavage required by the **refactor features into
opt-in components** focus (goal.md L57-59):

  "A CI gate fails the build if any core module directly imports from
   `ap2/components/<name>/`. All cross-references flow through the
   registry's hook protocol."

Without this gate, the cleavage erodes silently — a future refactor
accidentally re-couples core to a component (e.g. someone adds
`from ap2.components.janitor import X` to a tick handler for a "quick
fix" reason) and nobody notices until a downstream OSS-distribution
attempt discovers the leak. Pinning the cleavage at the canary stage
(one component in `ap2/components/`) is much cheaper than discovering
accumulated leaks later.

Mechanism: AST-walk every `.py` file under `ap2/` EXCEPT
`ap2/components/` and `ap2/tests/`. For each Import / ImportFrom node,
flag any reference to `ap2.components` (absolute form) or to
`components` resolved via a relative import that lands on
`ap2.components` (e.g. `from ..components.X import Y` from
`ap2/foo/bar.py`).

Why AST, not regex: comments, docstrings (including this very file)
and string literals can mention `from ap2.components.X import Y`
without it being a real import. A regex over file contents would flag
those false positives; `ast.parse` distinguishes code from prose
cleanly. Multi-line imports and `if TYPE_CHECKING:` guarded imports
are also unambiguously parseable by `ast` but messy for regex.

Dynamic-import exemption (by design): the registry uses
`importlib.import_module(...)` to discover components without a
hardcoded name list. Static-import detection is the cleavage we
enforce; runtime-import is the mechanism by which the registry
discovers components. The test does NOT walk `Call` nodes looking for
`importlib.import_module("ap2.components...")` — that surface is
intentionally exempt.

Path-keyed exemption (by design): the registry's discovery layer
walks the components package and is the sole exempt path today
(declared in `_EXEMPT_FILES` below). The exemption is path-keyed, not
pattern-keyed, so a future reader can audit exactly which files are
allowed to import from components and why. If axis (2) or axis (3)
work introduces another necessary direct importer, the exemption is
widened explicitly in `_EXEMPT_FILES`, not by relaxing the detection
pattern.
"""
from __future__ import annotations

import ast
import pathlib

import pytest


# Repository root, derived from this file's location:
# ap2/tests/test_core_import_direction.py -> repo/
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_AP2_ROOT = _REPO_ROOT / "ap2"

# Paths (repo-root-relative, POSIX) that are EXEMPT from the import-
# direction gate. Path-keyed (not pattern-keyed) so a reader can
# audit exactly which files are allowed to import from components
# and why each exemption exists.
#
# Today only the registry's discovery layer is exempt: it walks
# `ap2/components/*/manifest.py` via `importlib.import_module(...)`
# and is the sole place where a reference to the components package
# is legitimate. The registry's current implementation uses dynamic
# `importlib.import_module(components_pkg_name)` so it does not in
# fact contain a static `import ap2.components` today; the exemption
# is declared defensively so a future registry implementation that
# adds a static helper import (e.g. `from ap2.components import
# __path__`) is still allowed without weakening the gate.
_EXEMPT_FILES: tuple[str, ...] = (
    "ap2/registry.py",
)


def _iter_core_py_files() -> list[pathlib.Path]:
    """Every `.py` file under `ap2/` EXCEPT files inside
    `ap2/components/` (which are allowed — and expected — to live in
    the components package) and `ap2/tests/` (test code can name the
    components package freely; it isn't core).

    Returned paths are absolute. Caller filters against
    `_EXEMPT_FILES` (repo-root-relative POSIX strings) as needed.
    """
    out: list[pathlib.Path] = []
    for path in _AP2_ROOT.rglob("*.py"):
        rel_parts = path.relative_to(_AP2_ROOT).parts
        if rel_parts and rel_parts[0] in ("components", "tests"):
            continue
        # Skip any __pycache__ residue defensively (rglob shouldn't
        # surface .py files there, but bytecode caches can confuse
        # third-party tooling).
        if "__pycache__" in rel_parts:
            continue
        out.append(path)
    return out


def _resolve_relative_import(
    file_relpath_posix: str, level: int, module: str | None
) -> str:
    """Resolve a relative `ImportFrom` to its absolute module path.

    `file_relpath_posix` is the importing file's path relative to
    repo root, POSIX-style (e.g. `'ap2/daemon.py'` or
    `'ap2/sub/__init__.py'`).

    `level` is `ast.ImportFrom.level` (>=1 for relative). `module`
    is the dotted suffix after the dots (e.g. `'components.janitor'`
    in `from ..components.janitor import X`) or None for
    `from .. import X`.

    Python's relative-import semantics (PEP 328): a module at
    `pkg.sub.mod` has a containing package `pkg.sub`. `from . import
    X` resolves into that package; `from .. import X` resolves into
    `pkg`; etc. Dropping `(level - 1)` parts from the file's own
    package gives the base; the `module` suffix is appended.

    Package vs module: if the file is a package's `__init__.py`,
    the file's "own package" is the directory containing
    `__init__.py` (e.g. `ap2/sub/__init__.py` lives in package
    `ap2.sub`). For a regular module, the file's own package is the
    parent directory.
    """
    parts = file_relpath_posix.replace(".py", "").split("/")
    # For a regular module `ap2/foo/bar.py` parts == ['ap2', 'foo', 'bar'];
    # for a package `ap2/foo/__init__.py` parts == ['ap2', 'foo', '__init__'].
    # The own-package is the directory: drop the last name in both cases.
    own_pkg_parts = parts[:-1]
    # `level=1` means same package (drop 0 parts from own_pkg);
    # `level=2` means parent (drop 1 part); etc.
    drop = level - 1
    if drop > 0:
        base_parts = own_pkg_parts[:-drop] if drop <= len(own_pkg_parts) else []
    else:
        base_parts = list(own_pkg_parts)
    if module:
        return ".".join(base_parts + module.split("."))
    return ".".join(base_parts)


def _is_components_target(absolute_module: str) -> bool:
    """True iff `absolute_module` names the components package or any
    submodule of it (e.g. `ap2.components`, `ap2.components.janitor`,
    `ap2.components.janitor.manifest`)."""
    return (
        absolute_module == "ap2.components"
        or absolute_module.startswith("ap2.components.")
    )


def find_violations(
    source: str, file_relpath_posix: str
) -> list[tuple[int, str]]:
    """Parse `source` and return every static-import reference to
    `ap2.components` as (lineno, offending-statement) tuples.

    Handles four import forms (briefing § Scope):

      1. `import ap2.components.X`           — `Import` with
                                                `alias.name='ap2.components.X'`
      2. `from ap2.components.X import Y`    — `ImportFrom` with
                                                `level=0`, `module='ap2.components.X'`
      3. `from .components.X import Y`       — `ImportFrom` with
                                                `level=1`, `module='components.X'`
                                                (only resolves to `ap2.components`
                                                when the file lives directly
                                                under `ap2/`)
      4. `from ..components.X import Y`      — `ImportFrom` with
                                                `level=2`, `module='components.X'`
                                                (resolves to `ap2.components`
                                                when the file lives in a
                                                first-level subpackage of `ap2/`)

    Dynamic imports via `importlib.import_module(...)` are NOT
    inspected — by design. The registry uses them intentionally; the
    gate enforces the static cleavage only.

    Returns ALL violations, not just the first, so a refactor that
    introduces multiple leaks gets a complete fix list in one
    pytest run.
    """
    tree = ast.parse(source, filename=file_relpath_posix)
    violations: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_components_target(alias.name):
                    stmt = f"import {alias.name}"
                    if alias.asname:
                        stmt += f" as {alias.asname}"
                    violations.append((node.lineno, stmt))
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                absolute = node.module or ""
                if _is_components_target(absolute):
                    names = ", ".join(
                        a.name + (f" as {a.asname}" if a.asname else "")
                        for a in node.names
                    )
                    violations.append(
                        (node.lineno, f"from {absolute} import {names}")
                    )
            else:
                resolved = _resolve_relative_import(
                    file_relpath_posix, node.level, node.module
                )
                if _is_components_target(resolved):
                    dots = "." * node.level
                    suffix = node.module or ""
                    names = ", ".join(
                        a.name + (f" as {a.asname}" if a.asname else "")
                        for a in node.names
                    )
                    violations.append(
                        (
                            node.lineno,
                            f"from {dots}{suffix} import {names}"
                            f"  (resolves to {resolved})",
                        )
                    )
    return violations


def test_no_core_module_statically_imports_from_components():
    """The load-bearing gate: every `.py` under `ap2/` (except
    `ap2/components/`, `ap2/tests/`, and `_EXEMPT_FILES`) is AST-
    parsed and asserted to contain no static reference to
    `ap2.components` in any `Import` / `ImportFrom` node.

    On failure the message lists EVERY violation across EVERY file
    (path, line, offending statement) — not just the first — so a
    refactor that introduces multiple leaks gets a complete fix
    list in one pytest run, not a one-at-a-time game of
    whack-a-mole.
    """
    all_violations: list[tuple[str, int, str]] = []
    for path in _iter_core_py_files():
        rel = path.relative_to(_REPO_ROOT).as_posix()
        if rel in _EXEMPT_FILES:
            continue
        source = path.read_text(encoding="utf-8")
        for lineno, stmt in find_violations(source, rel):
            all_violations.append((rel, lineno, stmt))

    if all_violations:
        msg_lines = [
            "TB-311: core modules must not statically import from "
            "`ap2.components`. All cross-references flow through the "
            "registry's hook protocol (goal.md L57-59).",
            "",
            f"Found {len(all_violations)} violation(s):",
        ]
        for rel, lineno, stmt in all_violations:
            msg_lines.append(f"  {rel}:{lineno}: {stmt}")
        msg_lines.append("")
        msg_lines.append(
            "If a new direct-importer is genuinely necessary (e.g. a "
            "new registry-side helper), add the file path to "
            "`_EXEMPT_FILES` in this test with a comment explaining why."
        )
        pytest.fail("\n".join(msg_lines))


def test_detector_catches_synthetic_leak_absolute_from():
    """The detector flags `from ap2.components.janitor import X`.
    Proves the gate's positive path actually catches leaks, not
    just passes vacuously against the current tree (which contains
    no violations by design).
    """
    source = "from ap2.components.janitor import run_janitor\n"
    hits = find_violations(source, "ap2/daemon.py")
    assert len(hits) == 1, hits
    lineno, stmt = hits[0]
    assert lineno == 1
    assert "ap2.components.janitor" in stmt
    assert "run_janitor" in stmt


def test_detector_catches_synthetic_leak_import_dotted():
    """The detector flags `import ap2.components.janitor`."""
    source = "import ap2.components.janitor\n"
    hits = find_violations(source, "ap2/daemon.py")
    assert len(hits) == 1, hits
    lineno, stmt = hits[0]
    assert lineno == 1
    assert "import ap2.components.janitor" == stmt


def test_detector_catches_synthetic_leak_bare_package_import():
    """The detector flags a bare `import ap2.components`."""
    source = "import ap2.components\n"
    hits = find_violations(source, "ap2/daemon.py")
    assert len(hits) == 1, hits
    assert hits[0][1] == "import ap2.components"


def test_detector_catches_synthetic_leak_relative_one_level():
    """The detector flags `from .components.janitor import X` when
    the file lives directly under `ap2/` (so `.components` resolves
    to `ap2.components`)."""
    source = "from .components.janitor import run_janitor\n"
    hits = find_violations(source, "ap2/daemon.py")
    assert len(hits) == 1, hits
    lineno, stmt = hits[0]
    assert lineno == 1
    assert ".components.janitor" in stmt
    assert "resolves to ap2.components.janitor" in stmt


def test_detector_catches_synthetic_leak_relative_two_levels():
    """The detector flags `from ..components.janitor import X` when
    the file lives in a first-level subpackage of `ap2/` (so
    `..components` resolves to `ap2.components`)."""
    source = "from ..components.janitor import run_janitor\n"
    hits = find_violations(source, "ap2/sub/mod.py")
    assert len(hits) == 1, hits
    lineno, stmt = hits[0]
    assert lineno == 1
    assert "..components.janitor" in stmt
    assert "resolves to ap2.components.janitor" in stmt


def test_detector_does_not_flag_unrelated_relative_imports():
    """A `from .components import X` from a file at
    `ap2/foo/bar.py` resolves to `ap2.foo.components`, NOT
    `ap2.components`. The detector must distinguish them via
    `level` arithmetic — a regex pattern would false-positive
    here. (There's no `ap2.foo.components` package in the tree
    today; the assertion is purely about the detector's
    resolution logic.)
    """
    source = "from .components import janitor\n"
    hits = find_violations(source, "ap2/foo/bar.py")
    assert hits == [], hits


def test_detector_does_not_flag_comments_or_docstrings():
    """Comments and docstrings (and string literals) can mention
    `from ap2.components.X import Y` as prose; the detector must
    only flag real import nodes. AST-based detection is what
    makes this work — a regex over file contents would
    false-positive here.
    """
    source = (
        '"""A docstring that mentions from ap2.components.janitor '
        'import X."""\n'
        "# A comment: import ap2.components.attention\n"
        "x = 'from ap2.components.foo import bar'\n"
    )
    hits = find_violations(source, "ap2/daemon.py")
    assert hits == [], hits


def test_detector_does_not_flag_dynamic_importlib_calls():
    """`importlib.import_module("ap2.components")` is exempt by
    design — the registry uses dynamic imports intentionally to
    discover components without a hardcoded name list. The
    detector only walks `Import` / `ImportFrom` nodes; a `Call`
    node referencing the components package as a string argument
    is NOT a static import and the gate stays quiet.
    """
    source = (
        "import importlib\n"
        "mod = importlib.import_module('ap2.components.janitor')\n"
    )
    hits = find_violations(source, "ap2/registry.py")
    assert hits == [], hits


def test_detector_reports_all_violations_not_just_first():
    """A refactor that introduces multiple leaks should get a
    complete fix list in one pytest run, not a one-at-a-time game
    of whack-a-mole. The detector returns every violation across
    the file, with the right lineno for each.
    """
    source = (
        "from ap2.components.janitor import run_janitor\n"  # line 1
        "import ap2.components.attention\n"                 # line 2
        "from ap2.components.focus_advance import maybe_advance\n"  # line 3
    )
    hits = find_violations(source, "ap2/daemon.py")
    assert len(hits) == 3, hits
    linenos = sorted(lineno for lineno, _ in hits)
    assert linenos == [1, 2, 3], linenos


def test_exempt_files_tuple_only_contains_real_paths():
    """`_EXEMPT_FILES` is path-keyed for explicit auditability:
    each entry must point at a real file in the tree, so a stale
    exemption (entry left over after a file moved or was deleted)
    surfaces as a test break. Path-keyed (not pattern-keyed) is a
    deliberate design choice — see module docstring.
    """
    for rel in _EXEMPT_FILES:
        assert (_REPO_ROOT / rel).is_file(), (
            f"TB-311: `_EXEMPT_FILES` entry {rel!r} does not point at "
            f"an existing file; either the file was moved/deleted (in "
            f"which case prune the exemption) or the path is "
            f"mistyped."
        )

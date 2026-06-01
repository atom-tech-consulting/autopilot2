"""TB-366: import-direction CI gate — `claude_agent_sdk` may only be imported
inside `ap2/adapters/`.

Pins the **codex support through an agent adaptor layer** focus's Progress
signal: "`claude_agent_sdk` is imported only inside `ClaudeCodeAdapter`, not
across `daemon.py` / `tools.py` / `verify.py` / `ideation_scrub.py`." All six
dispatch CALLS were adapter-routed by TB-360/362/363/364/365; the residual
IMPORTS were relocated behind `ap2/adapters/` by TB-366 (the daemon's
startup availability gate + injected-`sdk` seam now resolve through
`ap2.adapters.load_claude_sdk`; `tools.py` imports the `@tool` decorator via
`from ap2.adapters import tool`; the validator-judge's hermetic fake-SDK
capture routes through `load_claude_sdk` too).

This gate locks the single-backend-surface invariant the Codex adapter
depends on: without it, any future module could silently re-introduce a
direct `import claude_agent_sdk` against the Claude stream shape and the
Progress signal would erode unnoticed. Mirrors the component focus's
`test_core_import_direction.py` (core may not import from `ap2/components/`).

Mechanism: AST-walk every `.py` under `ap2/` EXCEPT `ap2/adapters/` (the one
package allowed to import the SDK) and `ap2/tests/` (test code — including the
real-SDK smoke tests — may name the SDK freely; it isn't production source).
For each `Import` / `ImportFrom` node, flag any reference to
`claude_agent_sdk` (or a submodule of it).

Why AST, not regex: comments, docstrings (this file mentions
`import claude_agent_sdk` repeatedly) and string literals routinely name the
SDK as prose. A regex over file contents would false-positive on those;
`ast.parse` distinguishes code from prose cleanly. The gate matches import
statements ONLY.
"""
from __future__ import annotations

import ast
import pathlib

import pytest


# Repository root, derived from this file's location:
# ap2/tests/test_sdk_import_boundary.py -> repo/
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_AP2_ROOT = _REPO_ROOT / "ap2"

# The package allowed to import `claude_agent_sdk`, plus test code. Keyed by
# the first path component under `ap2/` so a whole subtree is exempt.
_EXEMPT_TOP_LEVEL: frozenset[str] = frozenset({"adapters", "tests"})


def _iter_non_adapter_py_files() -> list[pathlib.Path]:
    """Every `.py` file under `ap2/` EXCEPT files inside `ap2/adapters/`
    (the sole package allowed to import the SDK) and `ap2/tests/` (test
    code may name the SDK freely; it isn't production source).

    Returned paths are absolute. `__pycache__` bytecode residue is skipped
    defensively.
    """
    out: list[pathlib.Path] = []
    for path in _AP2_ROOT.rglob("*.py"):
        rel_parts = path.relative_to(_AP2_ROOT).parts
        if rel_parts and rel_parts[0] in _EXEMPT_TOP_LEVEL:
            continue
        if "__pycache__" in rel_parts:
            continue
        out.append(path)
    return out


def _is_sdk_target(module: str) -> bool:
    """True iff `module` names the Claude SDK package or any submodule of it
    (e.g. `claude_agent_sdk`, `claude_agent_sdk.types`)."""
    return (
        module == "claude_agent_sdk"
        or module.startswith("claude_agent_sdk.")
    )


def find_violations(
    source: str, file_relpath_posix: str
) -> list[tuple[int, str]]:
    """Parse `source` and return every static-import reference to
    `claude_agent_sdk` as (lineno, offending-statement) tuples.

    Handles both import forms:

      1. `import claude_agent_sdk[.X] [as sdk]`  — `Import` node whose
                                                    `alias.name` targets the SDK
      2. `from claude_agent_sdk[.X] import Y`     — `ImportFrom` node whose
                                                    `module` targets the SDK

    Relative `ImportFrom` (`level >= 1`) can never resolve to the top-level
    third-party `claude_agent_sdk`, so only absolute (`level == 0`) forms are
    inspected. Comments / docstrings / string literals naming the SDK are NOT
    flagged — `ast` only surfaces real import nodes.

    Returns ALL violations (not just the first) so a regression that adds
    multiple direct imports gets a complete fix list in one pytest run.
    """
    tree = ast.parse(source, filename=file_relpath_posix)
    violations: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_sdk_target(alias.name):
                    stmt = f"import {alias.name}"
                    if alias.asname:
                        stmt += f" as {alias.asname}"
                    violations.append((node.lineno, stmt))
        elif isinstance(node, ast.ImportFrom):
            # A relative import (level >= 1) resolves within `ap2`, never to
            # the top-level third-party `claude_agent_sdk` — skip it.
            if node.level == 0 and _is_sdk_target(node.module or ""):
                names = ", ".join(
                    a.name + (f" as {a.asname}" if a.asname else "")
                    for a in node.names
                )
                violations.append(
                    (node.lineno, f"from {node.module} import {names}")
                )
    return violations


def test_claude_sdk_imported_only_in_adapters():
    """The load-bearing gate: every `.py` under `ap2/` (except
    `ap2/adapters/` and `ap2/tests/`) is AST-parsed and asserted to contain
    no `import claude_agent_sdk` / `from claude_agent_sdk` statement.

    On failure the message lists EVERY violation across EVERY file (path,
    line, offending statement) — not just the first — so a regression that
    re-introduces multiple direct imports gets a complete fix list in one
    pytest run.
    """
    all_violations: list[tuple[str, int, str]] = []
    for path in _iter_non_adapter_py_files():
        rel = path.relative_to(_REPO_ROOT).as_posix()
        source = path.read_text(encoding="utf-8")
        for lineno, stmt in find_violations(source, rel):
            all_violations.append((rel, lineno, stmt))

    if all_violations:
        msg_lines = [
            "TB-366: `claude_agent_sdk` may be imported only inside "
            "`ap2/adapters/`. Route the SDK surface through the adapter layer "
            "(`ap2.adapters.load_claude_sdk` for the module handle, "
            "`from ap2.adapters import tool` for the schema decorator) so the "
            "single-backend-surface invariant the Codex adapter depends on "
            "holds.",
            "",
            f"Found {len(all_violations)} violation(s):",
        ]
        for rel, lineno, stmt in all_violations:
            msg_lines.append(f"  {rel}:{lineno}: {stmt}")
        pytest.fail("\n".join(msg_lines))


def test_detector_catches_import_alias():
    """The detector flags `import claude_agent_sdk as sdk`."""
    source = "import claude_agent_sdk as sdk\n"
    hits = find_violations(source, "ap2/daemon.py")
    assert len(hits) == 1, hits
    assert hits[0] == (1, "import claude_agent_sdk as sdk")


def test_detector_catches_from_import():
    """The detector flags `from claude_agent_sdk import tool`."""
    source = "from claude_agent_sdk import tool\n"
    hits = find_violations(source, "ap2/tools.py")
    assert len(hits) == 1, hits
    lineno, stmt = hits[0]
    assert lineno == 1
    assert stmt == "from claude_agent_sdk import tool"


def test_detector_catches_submodule_import():
    """The detector flags a submodule import like
    `from claude_agent_sdk.types import X`."""
    source = "from claude_agent_sdk.types import ClaudeAgentOptions\n"
    hits = find_violations(source, "ap2/daemon.py")
    assert len(hits) == 1, hits
    assert "claude_agent_sdk.types" in hits[0][1]


def test_detector_does_not_flag_comments_or_docstrings():
    """Comments, docstrings, and string literals can mention
    `import claude_agent_sdk` as prose; the detector must only flag real
    import nodes. AST-based detection is what makes this work — a regex over
    file contents would false-positive here.
    """
    source = (
        '"""A docstring mentioning import claude_agent_sdk as sdk."""\n'
        "# A comment: from claude_agent_sdk import tool\n"
        "x = 'import claude_agent_sdk'\n"
    )
    hits = find_violations(source, "ap2/daemon.py")
    assert hits == [], hits


def test_detector_does_not_flag_adapter_reexport_import():
    """`from ap2.adapters import tool` / `load_claude_sdk` — the relocated
    re-export path TB-366 introduced — is NOT a `claude_agent_sdk` import and
    must stay quiet (it's how non-adapter source legitimately reaches the SDK
    surface now).
    """
    source = (
        "from ap2.adapters import tool\n"
        "from ap2.adapters import load_claude_sdk\n"
        "from .adapters import load_claude_sdk\n"
    )
    hits = find_violations(source, "ap2/tools.py")
    assert hits == [], hits


def test_detector_reports_all_violations_not_just_first():
    """A regression that adds multiple direct imports should get a complete
    fix list in one pytest run, not a one-at-a-time game of whack-a-mole.
    """
    source = (
        "import claude_agent_sdk as sdk\n"          # line 1
        "from claude_agent_sdk import tool\n"       # line 2
        "import claude_agent_sdk\n"                 # line 3
    )
    hits = find_violations(source, "ap2/daemon.py")
    assert len(hits) == 3, hits
    assert sorted(lineno for lineno, _ in hits) == [1, 2, 3]

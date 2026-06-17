"""TB-415: regression gate — no shipped file carries an absolute path under
the sandbox operator's local checkout root.

Why this module exists
----------------------
goal.md's "cut a public source-available distribution" focus requires (axis-1
identity scrub, Progress signal 1) that "a clean checkout installs and runs the
test suite green with no sandbox-specific paths or identity baked into source".
TB-409 swept ``ap2/*.py`` (a NON-recursive glob) and missed the test tree;
``ap2.tests`` is a declared package (pyproject ``[tool.setuptools] packages``),
so anything baked there ships in the sdist/wheel. TB-415 scrubbed the one
residual leak (a ``/Users/<sandbox-user>/repos/...`` captured-response path in
``test_json_extract_util.py``) and this gate pins the invariant recursively so
the next copy-pasted debug path can't silently re-introduce it.

What this gate pins (ONE invariant)
-----------------------------------
No shipped source/doc carries an absolute path under the sandbox operator's
local repo root — the ``/Users/<sandbox-user>/repos`` shape TB-409 scrubbed
from ``ap2/json_extract.py``. It is deliberately NOT an enumerated-case linter:
it does not classify many path shapes, and it does NOT forbid the bare project
name ``post-train`` (TB-409 kept those narrative cost/bug-repro provenance
comments as sandbox-neutral references — see ``ap2/json_extract.py``,
``ap2/cli_review.py``, ``ap2/operator_queue.py``).

Self-match avoidance
--------------------
This gate file lives inside the scanned tree, so the forbidden needle is built
from parts at runtime (the ``ap2.sandbox.DEFAULT_USER`` token) — the contiguous
leak literal never appears in this source, otherwise the gate would flag itself
as it walks ``ap2/``.

Binary artifacts
----------------
We scan SHIPPED source/doc text only and skip ``__pycache__`` / build dirs.
Compiled ``*.pyc`` embed the absolute build path as ``co_filename``, but
bytecode is gitignored and excluded from the sdist/wheel — it is not a shipped
leak, and folding it in would make the gate depend on transient build state.
"""
from __future__ import annotations

from pathlib import Path

from ap2.sandbox import DEFAULT_USER

# Repo root: this file is ``<root>/ap2/tests/test_no_sandbox_path_leak.py``.
_REPO_ROOT = Path(__file__).resolve().parents[2]

# Forbidden needle, built from parts so the contiguous leak literal is never
# embedded in this file (self-match avoidance). ``DEFAULT_USER`` is the
# sandbox operator's overridable default username; the leak shape is an
# absolute path under that user's local ``repos`` checkout root.
_FORBIDDEN_CHECKOUT_ROOT = "/Users/" + DEFAULT_USER + "/repos"

# Shipped distribution surface: ``ap2/`` recursively (incl. ``ap2/tests/``),
# ``skills/``, and the top-level docs — what setuptools packages + what a clean
# checkout reads.
_SCAN_DIRS = ("ap2", "skills")
_SCAN_FILES = ("README.md", "CHANGELOG.md", "ap2/architecture.md")

# Text/source suffixes we ship. Anything else (notably compiled ``*.pyc``) is
# skipped — see the module docstring's "Binary artifacts" note.
_SOURCE_SUFFIXES = frozenset({
    ".py", ".md", ".txt", ".rst", ".toml", ".cfg", ".ini",
    ".yaml", ".yml", ".json", ".sh", ".j2",
})

# Build/VCS dirs to skip entirely while walking.
_SKIP_DIR_NAMES = frozenset({"__pycache__", ".git", ".pytest_cache", ".mypy_cache"})

# Allowlist (Design): legitimate generic/parameterized example paths the sweep
# surfaces — none is the sandbox operator's real checkout root, so none must
# false-fail. Belt-and-suspenders: with the narrow ``_FORBIDDEN_CHECKOUT_ROOT``
# needle none of these even matches (no ``/repos`` segment), but pinning them
# here documents the carve-out so a future needle-broadening can't silently
# start false-failing on them. The companion test below actively exercises it.
_ALLOWLISTED_GENERIC_PATHS = (
    "/Users/{user}",      # ap2/sandbox.py parameterized template
    "/Users/fakeuser",    # ap2/tests/test_doctor.py `which ap2` fixture
    "/tmp/proj",          # generic example project path
    "/home/user",         # generic example home path
)


def _line_is_only_allowlisted(line: str) -> bool:
    """True iff the only checkout-root-shaped content on ``line`` is one of the
    allowlisted generics. With the narrow needle this never suppresses a real
    leak (an allowlisted generic does not contain ``_FORBIDDEN_CHECKOUT_ROOT``),
    so it is a documented guard, not an escape hatch."""
    return (
        _FORBIDDEN_CHECKOUT_ROOT not in line
        and any(generic in line for generic in _ALLOWLISTED_GENERIC_PATHS)
    )


def _iter_shipped_source_files():
    """Yield every shipped text/source file on the distribution surface."""
    for d in _SCAN_DIRS:
        root = _REPO_ROOT / d
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if any(part in _SKIP_DIR_NAMES for part in path.parts):
                continue
            if path.suffix.lower() not in _SOURCE_SUFFIXES:
                continue
            yield path
    for rel in _SCAN_FILES:
        path = _REPO_ROOT / rel
        if path.is_file():
            yield path


def test_no_sandbox_checkout_root_path_in_shipped_source():
    """FAIL if any shipped source/doc carries an absolute path under the
    sandbox operator's local checkout root (``/Users/<sandbox-user>/repos/...``).
    Passes iff absent — the durable companion to the briefing's ``! grep``
    bullet."""
    offenders: list[str] = []
    for path in _iter_shipped_source_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            # Non-UTF-8 / unreadable: not shipped source we can audit; skip.
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if _FORBIDDEN_CHECKOUT_ROOT in line and not _line_is_only_allowlisted(line):
                rel = path.relative_to(_REPO_ROOT)
                offenders.append(f"{rel}:{lineno}: {line.strip()}")

    assert not offenders, (
        "Shipped source/doc carries an absolute path under the sandbox "
        "operator's local checkout root (" + _FORBIDDEN_CHECKOUT_ROOT + "/...). "
        "Replace with a sandbox-neutral source (env-overridable lookup or a "
        "repo-relative path):\n  " + "\n  ".join(offenders)
    )


def test_allowlisted_generic_paths_are_not_flagged():
    """The named generic/parameterized example paths are sandbox-neutral and
    must never trip the gate — exercise the Design's carve-out directly so the
    allowlist is load-bearing, not decorative."""
    # The parameterized template + the doctor fixture really are present in
    # source (the sweep surfaced them); confirm the gate does NOT treat them as
    # the leak.
    sandbox_src = (_REPO_ROOT / "ap2" / "sandbox.py").read_text(encoding="utf-8")
    doctor_src = (_REPO_ROOT / "ap2" / "tests" / "test_doctor.py").read_text(encoding="utf-8")
    assert "/Users/{user}" in sandbox_src
    assert "/Users/fakeuser" in doctor_src

    for generic in _ALLOWLISTED_GENERIC_PATHS:
        assert generic not in _FORBIDDEN_CHECKOUT_ROOT
        # A line whose only checkout-root-shaped content is an allowlisted
        # generic is not an offender.
        assert _line_is_only_allowlisted(f"    path = f'{generic}/x'")

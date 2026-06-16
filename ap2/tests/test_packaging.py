"""Hermetic packaging-surface gates for ap2's declared dependencies (TB-371).

The codex backend is code-complete (AgentAdapter ABC + ClaudeCodeAdapter,
CodexAdapter, per-kind backend selection, adapter-routed dispatch, the parity
suite, and the gated real-SDK smoke) but only becomes *installable* once
`openai-codex` (OpenAI's official Codex SDK, import name `openai_codex`) is
declared as an optional dependency. `CodexAdapter` lazily `import openai_codex`
(ap2.adapters.load_codex_sdk) at first dispatch, the daemon-start codex-handle
gate calls it, and the smoke gates on `pytest.importorskip("openai_codex")` —
all dead without an install path.

These tests parse `pyproject.toml` (no network resolution) and assert:

1. The `codex` optional-dependencies extra exists and references the
   `openai-codex` distribution, so the packaging surface can't silently regress.
2. The base `dependencies` list stays Claude-only — `claude-agent-sdk` is the
   always-installed backend and `openai-codex` is NOT pulled by a bare install —
   so `pip install autopilot2` with no extras remains a working Claude install.

The MANIFEST.in tests (TB-410) parse `MANIFEST.in` as text (no build) and
assert the committed top-level `skills/` operator manual and the docs an outside
consumer needs are grafted/included into the setuptools source distribution.
`skills/` is not a Python package, so package-data cannot carry it — an sdist
that omits MANIFEST.in would silently drop the operator manual.
"""
from __future__ import annotations

import tomllib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"
MANIFEST_PATH = REPO_ROOT / "MANIFEST.in"


def _load_pyproject() -> dict:
    return tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))


def _requirement_names(reqs: list[str]) -> list[str]:
    """Return the bare distribution name for each PEP 508 requirement string.

    Strips version specifiers / extras / markers so an unpinned `openai-codex`
    and a future pinned `openai-codex>=1.2` both compare equal.
    """
    names: list[str] = []
    for raw in reqs:
        token = raw.strip()
        # cut markers / extras / version specifiers
        for sep in (";", "[", "<", ">", "=", "!", "~", " "):
            idx = token.find(sep)
            if idx != -1:
                token = token[:idx]
        names.append(token.strip().lower())
    return names


def test_codex_extra_declared():
    """The `codex` extra exists and references the `openai-codex` distribution."""
    data = _load_pyproject()
    extras = data["project"]["optional-dependencies"]
    assert "codex" in extras, (
        "pyproject.toml [project.optional-dependencies] must declare a `codex` "
        "extra so `pip install 'autopilot2[codex]'` / `uv sync --extra codex` "
        "can install the second backend's `openai_codex` handle."
    )
    codex_reqs = _requirement_names(extras["codex"])
    assert "openai-codex" in codex_reqs, (
        "the `codex` extra must list an `openai-codex` requirement (OpenAI's "
        "official Codex SDK, the distribution providing `import openai_codex`); "
        f"got {extras['codex']!r}."
    )


def test_base_install_stays_claude_only():
    """A bare install resolves claude-agent-sdk but never pulls openai-codex."""
    data = _load_pyproject()
    base = _requirement_names(data["project"]["dependencies"])
    assert "claude-agent-sdk" in base, (
        "claude-agent-sdk must remain a base (always-installed) dependency."
    )
    assert "openai-codex" not in base, (
        "openai-codex must stay opt-in via the `codex` extra — a bare "
        "`pip install autopilot2` must remain a working Claude-only install."
    )


def _manifest_directives() -> list[str]:
    """Return MANIFEST.in's directive lines (comments / blanks stripped)."""
    lines: list[str] = []
    for raw in MANIFEST_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        lines.append(line)
    return lines


def test_manifest_exists():
    """A MANIFEST.in exists at the repo root for the setuptools sdist."""
    assert MANIFEST_PATH.is_file(), (
        "MANIFEST.in must exist at the repo root so the setuptools source "
        "distribution carries non-package files (the skills/ operator manual "
        "and the top-level docs); without it an sdist silently drops them."
    )


def test_manifest_grafts_skills():
    """MANIFEST.in grafts the committed top-level skills/ operator manual.

    skills/ is not a Python package, so package-data cannot carry it — `graft
    skills` is the setuptools sdist mechanism that ships the operator manual.
    """
    directives = _manifest_directives()
    assert any(
        line.split()[0] == "graft" and "skills" in line.split()[1:]
        for line in directives
    ), (
        "MANIFEST.in must `graft skills` so the committed top-level skills/ "
        f"operator manual ships in the sdist; got directives {directives!r}."
    )


def test_manifest_includes_docs():
    """MANIFEST.in includes the top-level docs an outside consumer needs."""
    manifest_text = MANIFEST_PATH.read_text(encoding="utf-8")
    for doc in ("README.md", "LICENSE", "ap2/architecture.md"):
        assert doc in manifest_text, (
            f"MANIFEST.in must reference {doc!r} so the sdist carries it; "
            "an outside consumer needs the README, license, and architecture "
            "doc in the source distribution."
        )

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
assert the `ap2/skills/` operator manual and the docs an outside consumer needs
are grafted/included into the setuptools source distribution. TB-422 relocated
the skills tree under the `ap2` package so it ships as installed package-data in
the wheel (not only the sdist); the `graft ap2/skills` directive keeps the
operator-manual inclusion explicit in the sdist alongside the top-level docs.
"""
from __future__ import annotations

import ast
import tomllib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"
MANIFEST_PATH = REPO_ROOT / "MANIFEST.in"
# TB-412: the Mattermost path's source files. The `[mattermost]`-extra
# decision (see `test_mattermost_path_needs_no_extra`) parses these to
# confirm the Mattermost integration pulls no dependency beyond the base
# set — it speaks HTTP over stdlib `urllib`, not a third-party client.
MATTERMOST_SRC_PATHS = (
    REPO_ROOT / "ap2" / "components" / "mattermost" / "impl.py",
    REPO_ROOT / "ap2" / "components" / "mattermost" / "__init__.py",
)


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


def test_dev_extra_declared():
    """The `dev` extra exists and references the `pytest` test runner (TB-412).

    The hermetic test suite is run via `uv run --extra dev pytest -q`; an
    outside contributor needs `pip install 'autopilot2[dev]'` /
    `uv sync --extra dev` to get the runner. Pinning the extra here (next
    to the `codex` pin) keeps the packaging surface from silently dropping
    the only way to run the suite.
    """
    data = _load_pyproject()
    extras = data["project"]["optional-dependencies"]
    assert "dev" in extras, (
        "pyproject.toml [project.optional-dependencies] must declare a "
        "`dev` extra so `pip install 'autopilot2[dev]'` / "
        "`uv sync --extra dev` installs the test runner."
    )
    dev_reqs = _requirement_names(extras["dev"])
    assert "pytest" in dev_reqs, (
        "the `dev` extra must list a `pytest` requirement (the test runner "
        f"`uv run --extra dev pytest` drives); got {extras['dev']!r}."
    )


def _mattermost_imported_modules() -> set[str]:
    """Top-level module names imported by the Mattermost path's source.

    Parses each Mattermost source file with `ast` (no import / no network)
    and returns the set of top-level module names every `import x` /
    `from x import …` references. Used to record the `[mattermost]`-extra
    decision: the Mattermost path speaks HTTP over stdlib `urllib`, so the
    only non-stdlib names are the internal `ap2.*` packages — there is no
    third-party Mattermost/HTTP client to declare in an extra.
    """
    modules: set[str] = set()
    for path in MATTERMOST_SRC_PATHS:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    modules.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                # `from . import …` / `from .impl import …` have module=None
                # / a relative module — intra-package, no distribution.
                if node.level == 0 and node.module:
                    modules.add(node.module.split(".")[0])
    return modules


def test_mattermost_path_needs_no_extra():
    """Records the `[mattermost]`-extra decision (TB-412): NONE needed.

    The communication/Mattermost path imports only the Python standard
    library (`json`, `os`, `ssl`, `urllib`) plus the internal `ap2.*`
    packages — it talks to Mattermost over stdlib `urllib.request`, not a
    third-party client (no `requests` / `httpx` / `aiohttp` /
    `mattermostdriver` / `websocket`). Because the base deps already carry
    everything the Mattermost path needs, an empty `[mattermost]` extra
    would only add packaging noise. The decision is to declare NO such
    extra; this test pins both halves of that decision so a future
    Mattermost change that pulls a real dependency fails the gate (forcing
    the extra to be added then) and an accidental empty extra is rejected.
    """
    modules = _mattermost_imported_modules()

    # The Mattermost path's stdlib HTTP client is `urllib` — confirm the
    # stdlib path is the one in use (the basis for "no extra needed").
    assert "urllib" in modules, (
        "the Mattermost path is expected to speak HTTP over stdlib "
        f"`urllib`; imported top-level modules were {sorted(modules)}."
    )

    # No third-party HTTP / Mattermost client is imported — so there is no
    # distribution to pin in a `[mattermost]` extra.
    third_party_clients = {
        "requests",
        "httpx",
        "aiohttp",
        "urllib3",
        "websocket",
        "websockets",
        "mattermostdriver",
        "mattermost",
    }
    leaked = third_party_clients & modules
    assert not leaked, (
        "the Mattermost path must stay stdlib-only (urllib) — a third-party "
        f"client {sorted(leaked)} would require a `[mattermost]` extra; add "
        "one to pyproject.toml when that happens."
    )

    # The recorded decision: no `[mattermost]` extra exists today (an empty
    # extra would be packaging noise; the base deps cover the path).
    data = _load_pyproject()
    extras = data["project"]["optional-dependencies"]
    assert "mattermost" not in extras, (
        "no `[mattermost]` extra should be declared — the Mattermost path "
        "pulls no dependency beyond the base set (stdlib urllib). Add the "
        "extra only when the path takes a real third-party dependency."
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
    """MANIFEST.in grafts the `ap2/skills/` operator manual into the sdist.

    TB-422 relocated the skills tree under the `ap2` package so it ships as
    installed package-data in the wheel; `graft ap2/skills` keeps the
    operator-manual inclusion explicit in the sdist too.
    """
    directives = _manifest_directives()
    assert any(
        line.split()[0] == "graft" and "ap2/skills" in line.split()[1:]
        for line in directives
    ), (
        "MANIFEST.in must `graft ap2/skills` so the relocated operator manual "
        f"ships in the sdist; got directives {directives!r}."
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

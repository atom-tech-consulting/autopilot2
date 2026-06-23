"""TB-424 — the Codex / agentskills.io operator reference `AGENTS.md` ships as
installed package data so `ap2 sandbox sync-assets` can deploy it after a
non-editable `uv tool install` / `pip install`, not only from a repo clone.
Mirrors TB-422's skills-packaging fix (`test_skills_packaging.py`).

Before TB-424, `_agents_md_source()` resolved the reference as
`Path(__file__).parent.parent / "AGENTS.md"` — i.e. the repo-root `AGENTS.md`
relative to `ap2/sandbox.py`. After a non-editable install `ap2/sandbox.py`
lives in site-packages, so that resolved to a `site-packages/AGENTS.md` that
does NOT exist: the root `AGENTS.md` was not declared package data, so a
`uv tool install` gave the daemon + CLI but `ap2 sandbox sync-assets` /
`user-setup` printed "AGENTS.md source missing" and the Codex discovery pointer
(`~/.agents/AGENTS.md`) couldn't be deployed. TB-424 relocated the file under
the package (`ap2/AGENTS.md`, already covered by the `ap2 = ["*.md", ...]`
package-data glob) and switched the resolver to
`importlib.resources.files("ap2") / "AGENTS.md"` (which works for BOTH installed
and editable installs) with a repo-relative fallback for a bare source checkout.

These tests pin that contract without building a wheel.
"""
from __future__ import annotations

import importlib.resources
import tomllib
from pathlib import Path

from ap2 import sandbox


def test_agents_md_source_resolves_to_existing_file():
    """`_agents_md_source()` resolves to a real `AGENTS.md` file (the Codex
    operator reference `sync_assets` deploys to `~/.agents/AGENTS.md`)."""
    src = sandbox._agents_md_source()
    assert src.is_file(), f"_agents_md_source() must be an existing file; got {src}"
    assert src.name == "AGENTS.md"


def test_agents_md_source_resolves_from_installed_package():
    """The resolver reads the reference from the INSTALLED `ap2` package via
    `importlib.resources` — so it works after a non-editable `uv tool install`
    where the package lives in site-packages, not only from a repo clone."""
    pkg_agents_md = Path(str(importlib.resources.files("ap2"))) / "AGENTS.md"
    assert pkg_agents_md.is_file(), (
        "AGENTS.md must ship under the installed `ap2` package "
        f"(expected {pkg_agents_md})"
    )
    assert sandbox._agents_md_source() == pkg_agents_md


def test_agents_md_source_falls_back_to_repo_relative(monkeypatch):
    """If the installed-package resolver can't find the file, `_agents_md_source()`
    falls back to a repo-relative `ap2/AGENTS.md` — covering a bare source
    checkout that has not been `pip install`-ed at all."""
    def _boom(_name):
        raise ModuleNotFoundError("simulated: ap2 not importable as a package")

    monkeypatch.setattr(importlib.resources, "files", _boom)
    fallback = sandbox._agents_md_source()
    expected = Path(sandbox.__file__).resolve().parent / "AGENTS.md"
    assert fallback == expected
    assert fallback.is_file()


def test_pyproject_ships_agents_md_as_package_data():
    """The packaging metadata ships `ap2/AGENTS.md` as installed package data so
    a wheel carries it after `uv tool install`. The existing `*.md` glob under
    `[tool.setuptools.package-data] ap2` already covers `ap2/*.md`."""
    repo_root = Path(sandbox.__file__).resolve().parents[1]
    data = tomllib.loads(
        (repo_root / "pyproject.toml").read_text(encoding="utf-8")
    )
    pkg_data = data["tool"]["setuptools"]["package-data"]["ap2"]
    assert any(
        pattern == "*.md" or "AGENTS.md" in pattern for pattern in pkg_data
    ), (
        "pyproject [tool.setuptools.package-data] ap2 must include a glob that "
        f"ships `ap2/AGENTS.md` (e.g. `*.md`); got {pkg_data}"
    )
    assert (repo_root / "ap2" / "AGENTS.md").is_file(), (
        "AGENTS.md must live under the package at ap2/AGENTS.md so the "
        "package-data glob carries it into the wheel"
    )

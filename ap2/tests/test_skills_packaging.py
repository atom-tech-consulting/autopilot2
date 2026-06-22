"""TB-422 — the operator-skill bundles ship as installed package data so
`ap2 sandbox sync-assets` can deploy them after a non-editable
`uv tool install` / `pip install`, not only from a repo clone.

Before TB-422, `_skills_source()` resolved the tree as
`Path(__file__).parent.parent / "skills"` — i.e. `<repo>/skills/` relative to
`ap2/sandbox.py`. After a non-editable install `ap2/sandbox.py` lives in
site-packages, so that resolved to a `site-packages/skills` that does NOT
exist: the top-level `skills/` tree was not declared package data (only grafted
into the sdist via `MANIFEST.in`), so a `uv tool install` gave the daemon + CLI
but could not deploy the operator manual. TB-422 relocated the tree under the
package (`ap2/skills/`), declared it as package data, and switched the resolver
to `importlib.resources.files("ap2") / "skills"` (which works for BOTH installed
and editable installs) with a repo-relative fallback for a bare source checkout.

These tests pin that contract without building a wheel.
"""
from __future__ import annotations

import importlib.resources
import tomllib
from pathlib import Path

from ap2 import sandbox


def test_skills_source_resolves_to_existing_dir_with_operator_skills():
    """`_skills_source()` resolves to a real directory carrying the operator
    skill bundles (the ones `sync_assets` mirrors into the runtime roots)."""
    src = sandbox._skills_source()
    assert src.is_dir(), f"_skills_source() must be an existing dir; got {src}"
    for skill in ("ap2-board-ops", "ap2-task", "migrate-to-ap2"):
        skill_md = src / skill / "SKILL.md"
        assert skill_md.is_file(), (
            f"operator skill {skill}/SKILL.md missing under {src}"
        )


def test_skills_source_resolves_from_installed_package():
    """The resolver reads the tree from the INSTALLED `ap2` package via
    `importlib.resources` — so it works after a non-editable `uv tool install`
    where the package lives in site-packages, not only from a repo clone."""
    pkg_skills = Path(str(importlib.resources.files("ap2"))) / "skills"
    assert pkg_skills.is_dir(), (
        "the skills tree must ship under the installed `ap2` package "
        f"(expected {pkg_skills})"
    )
    assert sandbox._skills_source() == pkg_skills


def test_skills_source_falls_back_to_repo_relative(monkeypatch):
    """If the installed-package resolver can't find the tree, `_skills_source()`
    falls back to a repo-relative `ap2/skills` — covering a bare source checkout
    that has not been `pip install`-ed at all."""
    def _boom(_name):
        raise ModuleNotFoundError("simulated: ap2 not importable as a package")

    monkeypatch.setattr(importlib.resources, "files", _boom)
    fallback = sandbox._skills_source()
    expected = Path(sandbox.__file__).resolve().parent / "skills"
    assert fallback == expected
    assert (fallback / "ap2-task" / "SKILL.md").is_file()


def test_pyproject_declares_skills_as_package_data():
    """The packaging metadata declares the skills tree as installed package
    data (not only the sdist `MANIFEST.in` graft), so a wheel carries it after
    `uv tool install`."""
    repo_root = Path(sandbox.__file__).resolve().parents[1]
    data = tomllib.loads(
        (repo_root / "pyproject.toml").read_text(encoding="utf-8")
    )
    pkg_data = data["tool"]["setuptools"]["package-data"]["ap2"]
    assert any("skills" in pattern for pattern in pkg_data), (
        "pyproject [tool.setuptools.package-data] ap2 must include a "
        f"`skills/...` glob so the wheel ships the operator manual; got {pkg_data}"
    )

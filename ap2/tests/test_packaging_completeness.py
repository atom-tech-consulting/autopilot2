"""TB-425 — every on-disk `ap2` (sub)package ships in the built wheel.

`pyproject.toml` once hand-maintained `[tool.setuptools] packages = [...]`,
which omitted the entire `ap2.components.*` tree (extracted during the
components refactor but never added back). `Registry.discover()` imports
`ap2.components` at startup, so a non-editable `uv tool install` / `pip
install` — whose site-packages lacked the tree — crashed `ap2 init` / `ap2
start` with `ModuleNotFoundError: No module named 'ap2.components'`. Editable
operator checkouts masked it because `ap2.components` resolved from the repo
tree, so it only surfaced on the install path real users take.

TB-425 replaced the manual list with setuptools autodiscovery
(`[tool.setuptools.packages.find]` with `include = ["ap2", "ap2.*"]`).

These tests guard the PACKAGING DECLARATION, not a runtime `import` — a bare
`import ap2.components` passes trivially in an editable checkout and would NOT
catch the regression. Instead we walk `ap2/` for every directory carrying an
`__init__.py`, compute the package set the *effective* pyproject find-config
would ship via `setuptools.find_packages` (with the same include/exclude), and
assert every on-disk package is covered — so a future subpackage the manifest
forgets fails here.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

import setuptools

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"


def _find_config() -> dict:
    """The `[tool.setuptools.packages.find]` table from pyproject.toml."""
    data = tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))
    return data["tool"]["setuptools"]["packages"]["find"]


def _on_disk_packages() -> set[str]:
    """Every directory under the repo's `ap2/` tree that is a Python package
    (contains an `__init__.py`), expressed as a dotted package name."""
    packages: set[str] = set()
    for init in REPO_ROOT.glob("ap2/**/__init__.py"):
        rel = init.parent.relative_to(REPO_ROOT)
        packages.add(".".join(rel.parts))
    return packages


def _shipped_packages() -> set[str]:
    """The package set the effective pyproject find-config would ship, computed
    with `setuptools.find_packages` using the SAME include/exclude as the wheel
    build (so this test tracks the declaration, not a hand-copied list)."""
    cfg = _find_config()
    return set(
        setuptools.find_packages(
            where=str(REPO_ROOT),
            include=cfg.get("include", ["ap2", "ap2.*"]),
            exclude=cfg.get("exclude", []),
        )
    )


def test_find_config_uses_autodiscovery():
    """Packaging uses setuptools autodiscovery covering `ap2` + `ap2.*` (not a
    hand-maintained `packages = [...]` list that can rot)."""
    cfg = _find_config()
    include = cfg.get("include", [])
    assert "ap2" in include and "ap2.*" in include, (
        "pyproject [tool.setuptools.packages.find] must include both `ap2` and "
        f"`ap2.*` so the whole package tree ships; got include={include!r}."
    )


def test_every_on_disk_package_is_shipped():
    """The effective find-config covers every on-disk `ap2` package directory —
    no subpackage silently falls out of the wheel."""
    on_disk = _on_disk_packages()
    # Sanity: the walk must find the parent and the components tree at minimum,
    # else a globbing bug could make the assertion vacuously pass.
    assert "ap2" in on_disk and "ap2.components" in on_disk, (
        f"on-disk package walk looks wrong; found {sorted(on_disk)}"
    )
    shipped = _shipped_packages()
    missing = on_disk - shipped
    assert not missing, (
        "these on-disk `ap2` packages are NOT covered by the effective "
        "pyproject find-config and would be dropped from the wheel: "
        f"{sorted(missing)}. Update [tool.setuptools.packages.find]."
    )


def test_components_tree_is_shipped():
    """Pin the specific regression: every `ap2.components.*` subpackage that the
    old manual list omitted must be covered by the find-config."""
    on_disk = _on_disk_packages()
    components = {p for p in on_disk if p == "ap2.components" or p.startswith("ap2.components.")}
    assert components, "expected an ap2.components* tree on disk"
    shipped = _shipped_packages()
    missing = components - shipped
    assert not missing, (
        "the ap2.components tree must ship in the wheel; the find-config omits "
        f"{sorted(missing)}."
    )

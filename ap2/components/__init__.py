"""Component subpackages — opt-in features the registry discovers (TB-309).

Each immediate child of this package is one component (e.g. `janitor/`,
future `mattermost/`, `validator_judge/`, …); each component subpackage
declares its `Manifest` in `manifest.py`. `ap2/registry.py` walks this
package via `pkgutil.iter_modules` at daemon startup — there is NO
hardcoded list of component names anywhere in core.

Per the import-direction rule pinned by axis (6) of the components
focus (goal.md L203-214): the registry module is the ONLY allowed
direct importer of `ap2.components.*` from core. Other core modules
look up components via `default_registry().hook(...)` rather than
importing the subpackage directly.
"""

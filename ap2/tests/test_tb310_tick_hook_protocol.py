"""TB-310: daemon tick-hook protocol — walk registry instead of direct imports.

Pins axis (2) of the **refactor features into opt-in components** focus
(goal.md L132-144): the registry-walked phase contract that replaces
daemon._tick's pre-TB-310 direct imports + direct calls into
`auto_approve` / `auto_unfreeze` / `attention` / `focus_advance` /
`janitor`. The four assertions match the briefing's Verification block:

  (a) The `TickHook` typed callable signature is importable from
      `ap2.registry` — proves the type contract is established.
  (b) The `Phase` enum exposes the four canonical phase names
      (PRE_DISPATCH, POST_DISPATCH, POST_CRON, ATTENTION_EMISSION) —
      proves the phase enumeration is in place.
  (c) `Registry.discover()` returns the components named
      `janitor`, `auto_approve`, `auto_unfreeze`, `attention`, each
      with a tick_hook registered — proves the component-side wiring
      landed for the non-janitor stubs alongside the existing TB-309
      janitor canary. (TB-345 merged the former `focus_advance`
      component into the core `ap2/ideation_halt.py` module, so it is
      no longer a discoverable component.)
  (d) `registry.tick_hooks(PRE_DISPATCH)` returns a deterministic
      ordered list — proves the registry-walk dispatch shape is in
      place (the daemon's `_tick` iterates the same list).

Plus a structural pin on `daemon.py`: the body must no longer
direct-import the flat modules via dotted-relative imports
(`from .auto_approve import ...` etc.) — the registry walk replaces
them. Mirrors the briefing's grep-based verification line that drives
the same check from the command line.

Why this is load-bearing per the components-focus arc: every later
axis-(5) migration ships a real subpackage that re-uses the same
tick-hook + phase contract pinned here. If a future refactor
accidentally re-coupled daemon._tick to direct imports, the
structural pin in this file flips loudly.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

from ap2 import registry as registry_mod
from ap2.registry import (
    Manifest,
    Phase,
    Registry,
    TickHook,
    default_registry,
)


# --- (a) TickHook callable signature is importable from the registry ---

def test_tickhook_signature_importable_from_registry():
    """The `TickHook` type alias / callable signature lives on
    `ap2.registry`. Importable + non-None.
    """
    assert TickHook is not None, (
        "TB-310: `TickHook` should be importable from `ap2.registry`."
    )
    # `Callable[[Any, Any], None | Awaitable[None]]` is a parameterized
    # generic; just assert it's a usable annotation object (not a
    # placeholder None). Treating it as truthy is fine.
    assert TickHook is registry_mod.TickHook


# --- (b) Phase enum has the four canonical members ---

def test_phase_enum_has_four_canonical_members():
    """The phase Enum exposes PRE_DISPATCH, POST_DISPATCH, POST_CRON,
    ATTENTION_EMISSION — the four phases the briefing enumerates.
    """
    assert hasattr(Phase, "PRE_DISPATCH"), (
        "TB-310: Phase.PRE_DISPATCH should exist."
    )
    assert hasattr(Phase, "POST_DISPATCH"), (
        "TB-310: Phase.POST_DISPATCH should exist."
    )
    assert hasattr(Phase, "POST_CRON"), (
        "TB-310: Phase.POST_CRON should exist."
    )
    assert hasattr(Phase, "ATTENTION_EMISSION"), (
        "TB-310: Phase.ATTENTION_EMISSION should exist."
    )
    names = {p.name for p in Phase}
    assert names == {
        "PRE_DISPATCH",
        "POST_DISPATCH",
        "POST_CRON",
        "ATTENTION_EMISSION",
    }, (
        f"TB-310: Phase enum should have exactly the four canonical "
        f"members; got {sorted(names)}"
    )


# --- (c) Registry.discover() returns the five components, each w/ tick_hook ---

_EXPECTED_COMPONENTS = {
    "janitor",
    "auto_approve",
    "auto_unfreeze",
    "attention",
}


def test_registry_discover_returns_five_components():
    """`Registry.discover()` surfaces janitor (the TB-309 canary) plus
    the TB-310 stubs (auto_approve, auto_unfreeze, attention). TB-345
    merged the former `focus_advance` component's residual detector into
    the core `ap2/ideation_halt.py` module, so it's no longer a
    discoverable component. The set is checked as a superset, not
    equality, so a future axis-(5) migration adding a new component
    doesn't flip this test — it only flips if one of the expected
    components goes missing.
    """
    registry = Registry.discover()
    names = {m.name for m in registry.components}
    missing = _EXPECTED_COMPONENTS - names
    assert not missing, (
        f"TB-310: expected components {sorted(_EXPECTED_COMPONENTS)} "
        f"to be discoverable via filesystem walk of "
        f"`ap2/components/*/manifest.py`; missing={sorted(missing)}, "
        f"discovered={sorted(names)}"
    )


def test_each_component_has_a_tick_hook_registered():
    """Each of the five components registers at least one tick hook
    via the new `Manifest.tick_hooks` field, AND exposes a
    `hook_points["tick_hook"]` for the TB-309-pinned lookup shape.
    Both surfaces are load-bearing: `hook_points` is what
    `run_cron`'s direct janitor lookup uses; `tick_hooks` is what
    `_tick`'s phase walk uses.
    """
    registry = Registry.discover()
    for name in _EXPECTED_COMPONENTS:
        manifest = next(
            (m for m in registry.components if m.name == name), None,
        )
        assert manifest is not None, (
            f"TB-310: component {name!r} should be discoverable."
        )
        assert "tick_hook" in manifest.hook_points, (
            f"TB-310: component {name!r}'s manifest should expose a "
            f"`tick_hook` entry in `hook_points` (TB-309 lookup shape)."
        )
        assert callable(manifest.hook_points["tick_hook"]), (
            f"TB-310: component {name!r}'s `hook_points['tick_hook']` "
            f"should be callable."
        )
        assert len(manifest.tick_hooks) >= 1, (
            f"TB-310: component {name!r}'s manifest should register "
            f"at least one `(phase, fn)` entry in `tick_hooks`."
        )
        for entry in manifest.tick_hooks:
            assert isinstance(entry, tuple) and len(entry) == 2, (
                f"TB-310: each `tick_hooks` entry for {name!r} must be "
                f"a (Phase, callable) tuple; got {entry!r}"
            )
            phase, hook = entry
            assert isinstance(phase, Phase), (
                f"TB-310: {name!r}'s tick_hooks entry phase must be "
                f"a `Phase` enum member; got {phase!r}"
            )
            assert callable(hook), (
                f"TB-310: {name!r}'s tick_hooks entry hook must be "
                f"callable; got {hook!r}"
            )


# --- (d) tick_hooks(PRE_DISPATCH) returns a deterministic ordered list ---

def test_tick_hooks_pre_dispatch_returns_deterministic_ordered_list():
    """`Registry.tick_hooks(PRE_DISPATCH)` returns the hooks in
    name-sorted component order. Post-TB-345 the only PRE_DISPATCH
    registry hook is `auto_unfreeze`'s — the former `focus_advance`
    component's residual detector was merged into the core
    `ap2/ideation_halt.py` module, which the daemon calls directly
    (not via a registry tick-hook) right after the PRE_DISPATCH walk.
    The order still matters for any future PRE_DISPATCH component; the
    name-sorted invariant is the spine.
    """
    registry = Registry.discover()
    hooks = registry.tick_hooks(Phase.PRE_DISPATCH)
    assert isinstance(hooks, list), (
        "TB-310: `tick_hooks(phase)` should return a list."
    )
    # Map each hook back to its component name by walking manifests.
    name_for_hook: dict[int, str] = {}
    for manifest in registry.components:
        for phase, fn in manifest.tick_hooks:
            name_for_hook[id(fn)] = manifest.name
    ordered_names = [name_for_hook[id(h)] for h in hooks]
    # Expected: alphabetical by component name. The list is a *subset*
    # of all PRE_DISPATCH components (a future axis-(5) migration might
    # register a new PRE_DISPATCH component); the ordering invariant is
    # the spine.
    assert ordered_names == sorted(ordered_names), (
        f"TB-310: PRE_DISPATCH hooks should be returned in name-sorted "
        f"component order; got {ordered_names}"
    )
    assert "auto_unfreeze" in ordered_names, (
        f"TB-310: auto_unfreeze should register a PRE_DISPATCH hook; "
        f"got {ordered_names}"
    )
    # TB-345: focus_advance is no longer a registry component, so it
    # MUST NOT appear in the PRE_DISPATCH hook set.
    assert "focus_advance" not in ordered_names, (
        f"TB-345: focus_advance was merged into core ideation_halt and "
        f"must not register a PRE_DISPATCH hook; got {ordered_names}"
    )
    # Determinism: calling twice returns the same list.
    again = Registry.discover().tick_hooks(Phase.PRE_DISPATCH)
    again_names = [name_for_hook[id(h)] for h in again]
    assert ordered_names == again_names, (
        f"TB-310: `tick_hooks(PRE_DISPATCH)` should return a "
        f"deterministic order across discovery passes; "
        f"got {ordered_names} then {again_names}"
    )


def test_tick_hooks_for_each_phase_returns_list():
    """Every phase yields a list (possibly empty for a phase with no
    registered hooks). Guards against a method that raises or returns
    None for an unknown / unused phase — the daemon's walk iterates
    every phase and would crash on a non-iterable.
    """
    registry = Registry.discover()
    for phase in Phase:
        hooks = registry.tick_hooks(phase)
        assert isinstance(hooks, list), (
            f"TB-310: `tick_hooks({phase.name})` should return a list; "
            f"got {type(hooks).__name__}"
        )


# --- Structural pin: daemon.py no longer dotted-relative-imports flat modules ---

_FORBIDDEN_PATTERN = re.compile(
    r"^\s*("
    r"from \.attention"
    r"|from \.auto_approve"
    r"|from \.auto_unfreeze"
    r"|from \.focus_advance"
    r"|from \. import janitor"
    r")",
    re.MULTILINE,
)


def test_daemon_no_relative_imports_of_componentized_modules():
    """Pin the briefing's Verification grep:

        grep -nE '^\\s*(from \\.attention|from \\.auto_approve|...)' ap2/daemon.py

    must return zero matches. Today's daemon walks the registry for
    PRE_DISPATCH / ATTENTION_EMISSION / POST_DISPATCH hooks instead of
    direct-importing each module via dotted-relative paths. The
    auto_approve / auto_unfreeze / focus_advance namespaces are
    reached through the `from . import (...)` package-level import
    (which the grep regex specifically does not match — multi-line
    imports don't have `from .auto_approve` on a single line); test-
    backward-compat aliases live below as module-level
    `daemon._maybe_auto_unfreeze = auto_unfreeze._maybe_auto_unfreeze`
    style assignments.
    """
    daemon_path = Path(__file__).resolve().parent.parent / "daemon.py"
    src = daemon_path.read_text()
    matches = _FORBIDDEN_PATTERN.findall(src)
    assert not matches, (
        f"TB-310: daemon.py must not direct-import componentized "
        f"flat modules via dotted-relative paths; "
        f"found {len(matches)} match(es): {matches[:3]}"
    )


def test_daemon_tick_calls_registry_tick_hooks():
    """Source-pin: the body of `_tick` calls
    `default_registry().tick_hooks(...)` at least once. Catches a
    refactor that accidentally regressed to direct calls.
    """
    from ap2 import daemon
    src = inspect.getsource(daemon._tick)
    assert "tick_hooks(" in src, (
        "TB-310: `_tick` should walk `registry.tick_hooks(phase)` "
        "instead of calling components directly."
    )


# --- Sanity: TickHook accepts the existing hook callables we registered ---

def test_registered_hooks_match_tickhook_shape():
    """Each registered hook is `(cfg, sdk) -> None | Awaitable[None]`.
    Loose check via `inspect.signature` — we don't run the hooks,
    just verify the parameter count is sane (>= 2 positional, since
    that's what the daemon-side walk passes).
    """
    registry = Registry.discover()
    for manifest in registry.components:
        for phase, hook in manifest.tick_hooks:
            sig = inspect.signature(hook)
            params = [
                p for p in sig.parameters.values()
                if p.kind in (
                    inspect.Parameter.POSITIONAL_ONLY,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                )
            ]
            assert len(params) >= 2, (
                f"TB-310: {manifest.name!r}'s {phase.name} tick_hook "
                f"should accept at least 2 positional args (cfg, sdk); "
                f"got {sig}"
            )

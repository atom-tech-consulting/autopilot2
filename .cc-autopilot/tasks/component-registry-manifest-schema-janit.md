# Component registry + manifest schema + janitor canary

## Goal

Deliver the structural prerequisite for the **Current focus: refactor
features into opt-in components**: a component registry, a manifest
schema, and one converted component (`janitor/`) that proves the
shape. Per goal.md L116-130, axis (1) is the explicit prerequisite for
every other axis in the focus — without a manifest contract pinned in
one converted component, every subsequent migration would re-invent
the protocol. The Done-when bullet "Every existing module that wraps
an autonomous behavior ... lives under `ap2/components/<name>/` and is
loaded via the component registry" (goal.md L52-56) starts being
satisfied for `janitor.py` once this lands.

Why now: the focus was extended on 2026-05-28 and explicitly sequences
this as the prerequisite (goal.md L216 — "(1) is the prerequisite for
everything else"); attempting axis (2), (3), (4), or any axis-(5)
migration first would either invent an ad-hoc registry shape that the
canonical axis-1 work then has to unwind, or stall the migration
pipeline entirely.

## Scope

- Add `ap2/registry.py`: a `Registry` class + dataclasses describing
  the manifest schema. Manifest fields per goal.md L121-125: `name`,
  `env_flag` (str or None for always-on), `default_enabled` (bool),
  `hook_points` (typed dict of registered callables — `tick_hook`,
  `validator_hook`, `channel_adapter`, `status_report_section`,
  `cli_verb`, etc.; for THIS TB only `tick_hook` is wired through —
  the other hook-point names are reserved in the schema for axes
  (2)/(3)/(4)), `dependencies` (list of component names).
- Add `ap2/components/__init__.py` and `ap2/components/janitor/`
  subpackage. Move the existing flat `ap2/janitor.py` into
  `ap2/components/janitor/__init__.py` (or split into smaller files
  inside the subpackage if natural — `_judge.py`, `_findings.py`,
  etc.). Add `ap2/components/janitor/manifest.py` declaring the
  janitor manifest: agent picks a sensible `env_flag` aligned with
  the existing `AP2_JANITOR_*` knob family (suggest
  `AP2_JANITOR_DISABLED` so default-on is the conservative default;
  preserve all existing `AP2_JANITOR_MAX_FINDINGS_LLM` /
  `AP2_JANITOR_JUDGE_*` knobs exactly), `default_enabled=True`,
  `hook_points={"tick_hook": run_janitor}`, `dependencies=[]`.
- Registry discovers components at daemon startup by walking
  `ap2/components/*/manifest.py` (use `importlib` or `pkgutil` —
  filesystem-driven discovery; do NOT hardcode component names in
  the registry, so future migrations need zero registry edits).
- Registry exposes a typed API: `registry.components` (all),
  `registry.enabled_components(cfg)` (filtered by env flag),
  `registry.hook(name, component="janitor")` (lookup by hook-point
  name + component).
- Replace `from .janitor import (...)` / `from . import janitor as
  _janitor` in core call sites — confirmed today at
  `ap2/cli_daemon.py:248`, `ap2/daemon.py:981`, `ap2/status_report.py:1157`
  — with registry-driven lookup. If `status_report.py`'s janitor
  imports are rendering helpers that semantically belong in core
  (status-report digest composition is core per goal.md L150-152),
  hoist them to core; if they're janitor-data accessors, route them
  through a typed registry hook.
- Preserve all existing janitor behavior bit-for-bit; this is a
  structural refactor only (goal.md L278-282 non-goal: "Removing
  behavior during component extraction").

## Design

The registry is the contract every subsequent axis-(5) migration will
satisfy. Two design constraints flow from goal.md:

1. **Filesystem-driven discovery** (not a hardcoded component list):
   per goal.md L188-201, each migration ships its own component
   subpackage; the registry must pick them up without a registry-side
   edit. Use `pkgutil.iter_modules(ap2.components.__path__)` or
   equivalent. The registry module itself is the ONLY allowed direct
   importer of `ap2.components.*` (axis (6) gate in TB-311 will
   codify this exemption).

2. **Manifest in Python, not YAML**: goal.md L121 says `manifest.py`
   explicitly. Keeps the hook-point values as live callables, not
   string indirection.

The janitor canary is chosen per goal.md L128/L181 ("the canary —
pick the least entangled, likely `janitor/`"). Janitor today has
three direct importers in core (cli_daemon, daemon, status_report);
all three are rewired through registry lookup as part of this TB so
the canary proves the cleavage end-to-end rather than half-way.

## Verification

- `uv run pytest -q` — full suite passes.
- `test -d ap2/components/janitor` — janitor lives under the new
  subpackage.
- `test -f ap2/components/janitor/manifest.py` — manifest file
  declared.
- `test -f ap2/components/janitor/__init__.py` — subpackage init
  exists.
- `test -f ap2/registry.py` — registry module exists at the expected
  path.
- `uv run pytest -q ap2/tests/test_tb211_event_types.py` — existing
  janitor-event regression suite still passes (proves daemon-side
  janitor dispatch behavior is unchanged across the refactor).
- A new regression-pin test the agent writes at
  `ap2/tests/test_tb309_components_canary.py` that asserts:
  (a) `Registry.discover()` returns a list including a component
  named "janitor"; (b) the janitor manifest exposes a callable
  `tick_hook`; (c) calling `registry.hook("tick_hook",
  component="janitor")` returns the same callable object as
  importing `run_janitor` from `ap2.components.janitor`. Run via
  `uv run pytest -q ap2/tests/test_tb309_components_canary.py`.
- `ap2/registry.py` Prose: the registry's discovery walk is
  filesystem-driven (`pkgutil.iter_modules` or `importlib` over
  `ap2/components/`), not a hardcoded list of component names —
  judge confirms via Read of the discovery function.

## Out of scope

- Migrating any component other than janitor (axis (5) migrations
  for validator_judge, mattermost, attention, focus_advance,
  auto_unfreeze, auto_approve are each their own TB-N per goal.md
  L176-201).
- Daemon tick-hook walk pattern (axis (2) — separate TB; this TB
  defines the registry shape and the canary's manifest, not the
  daemon-side consumption pattern).
- Channel-adapter ABC (axis (3) — separate TB).
- Validator pipeline-as-list (axis (4) — separate TB).
- Import-direction CI gate (axis (6) partial — separate TB; this
  TB lands the canary, the gate TB pins it).
- New env knobs beyond the conservative `AP2_JANITOR_DISABLED` (and
  even that follows the existing `AP2_JANITOR_*` family naming);
  no knob renaming (goal.md L64-67 backwards-compat).

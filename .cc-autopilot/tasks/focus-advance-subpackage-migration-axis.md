## Goal

Current focus: refactor features into opt-in components — axis (5)
`focus_advance/` subpackage migration. Goal.md L189-193 names
`focus_advance/` as the fifth migration in order ("multi-focus +
focus auto-advance — reads `goal.md` headings, runs the empty-cycles
counter, advances the in-memory focus pointer, emits `focus_advanced`
/ `roadmap_complete` events"). Relocate the module body at
`ap2/focus_advance.py` into `ap2/components/focus_advance/__init__.py`,
update the existing `manifest.py` so its `_tick_hook` wrapper calls
the intra-package symbol (it currently wraps the flat module), and
clean up the three module-level alias rebinds in `ap2/daemon.py`
(L1721-1723: `_FOCUS_RECENT_TAIL_N`,
`_ideation_empty_against_focus`, `_maybe_advance_focus`) so core
never imports from `ap2/components/`. The migration preserves the
kill switch `AP2_FOCUS_AUTO_ADVANCE_DISABLED` verbatim (goal.md
L64-67) and the `_maybe_advance_focus(cfg, sdk)` signature.

Why now: `focus_advance/` is the most self-contained of the
remaining flat-module behaviors — no channel-adapter dependency
(unlike `attention/`, goal.md L188), no operator-queue coupling
(unlike `auto_unfreeze/`), no ideation/cost guard touchpoints
(unlike `auto_approve/`, goal.md L196-197). Highest-ROI migration
to convert next while axis-3 channel work runs in parallel; every
cycle the flat module survives, the component-vs-core boundary is
defined by an asymmetric exemption rather than a clean cleavage.

## Scope

- `git mv ap2/focus_advance.py ap2/components/focus_advance/__init__.py`
  (mirror janitor canary shape; manifest stays at
  `ap2/components/focus_advance/manifest.py`).
- Update `ap2/components/focus_advance/manifest.py` so the
  `_tick_hook` wrapper resolves the call target intra-package (no
  `from ap2 import focus_advance`).
- Update `ap2/daemon.py` L1721-1723 — the three module-level alias
  rebinds (`_FOCUS_RECENT_TAIL_N`, `_ideation_empty_against_focus`,
  `_maybe_advance_focus`) — to source from the registry's
  `focus_advance` manifest's `hook_points` exposure. Add the three
  symbols to the manifest's `hook_points` dict and look them up at
  call time. Core must not statically import from
  `ap2/components/focus_advance/`.
- If `ap2/tests/` has any `from ap2 import focus_advance` or
  `from ap2.focus_advance import` references, update them to the
  new symbol path (`ap2.components.focus_advance` — tests are
  allowed to import directly per the import-direction gate's
  exemption rules; verify by inspecting
  `ap2/tests/test_core_import_direction.py`'s `_EXEMPT_FILES`).
- Add a regression pin `ap2/tests/test_tb313_focus_advance_migration.py`
  with 3-5 tests asserting: (a) the subpackage `__init__.py` exists,
  (b) `ap2/focus_advance.py` is gone, (c) the manifest's tick hook
  executes `_maybe_advance_focus` end-to-end with a stubbed `sdk`,
  (d) `AP2_FOCUS_AUTO_ADVANCE_DISABLED=1` still suppresses the
  inner call (kill switch preserved).

## Design

Mirror the existing `janitor/` canary shape: the manifest sits at
`ap2/components/<name>/manifest.py`, the runtime body sits at
`ap2/components/<name>/__init__.py`. The daemon's three module-level
aliases at L1721-1723 are bound at module-load time, so resolving
them via the registry requires either (a) a lazy
`default_registry().get("focus_advance")` call inside each call
site, or (b) the manifest exposing the three symbols as named
`hook_points` entries that the daemon resolves during `_tick`.
Option (b) is the durable shape — it generalises to other component
migrations. Add `hook_points["focus_recent_tail_n"]`,
`hook_points["ideation_empty_against_focus"]`,
`hook_points["maybe_advance_focus"]` to the manifest, and update
the daemon's call sites to look them up by name at call time.
Constants vs. functions can both live in `hook_points`; the dict's
value is just a callable-or-value.

## Verification

- `uv run pytest -q` — full suite passes (no observable behavior
  change for any focus-advance code path).
- `uv run pytest -q ap2/tests/test_core_import_direction.py` —
  import-direction gate still passes; core does not import from
  `ap2/components/focus_advance/`.
- `uv run pytest -q ap2/tests/test_tb310_tick_hook_protocol.py` —
  the existing tick-hook protocol regression continues to pass.
- `uv run pytest -q ap2/tests/test_tb313_focus_advance_migration.py`
  — the new regression pin passes.
- `test ! -f ap2/focus_advance.py` — flat module removed.
- `test -f ap2/components/focus_advance/__init__.py` — subpackage
  body file present.
- `test -f ap2/components/focus_advance/manifest.py` — manifest
  preserved.
- `! grep -nE 'from \.focus_advance|from ap2\.focus_advance|^import ap2\.focus_advance' ap2/daemon.py` — daemon no longer imports the flat module path.
- `! grep -nE 'from ap2 import focus_advance' ap2/components/focus_advance/manifest.py` — manifest no longer wraps the flat module.
- `grep -nE 'AP2_FOCUS_AUTO_ADVANCE_DISABLED' ap2/components/focus_advance/__init__.py` — kill switch preserved verbatim (goal.md L64-67).
- `ap2/components/focus_advance/manifest.py` Prose: the manifest's `hook_points` dict exposes `maybe_advance_focus` (and any other symbols the daemon used to alias from the flat module) so core resolves them via the registry rather than direct import; judge confirms via Read of the manifest + the daemon call sites.

## Out of scope

- Migrating any of the other flat-module-backed components
  (`auto_approve/`, `auto_unfreeze/`, `attention/`) — each is its
  own TB-N.
- Renaming `AP2_FOCUS_AUTO_ADVANCE_DISABLED` or any other env knob
  (goal.md L64-67 forbids).
- Adding new event types or changing the `focus_advanced` /
  `roadmap_complete` event payload shape.
- Adding a disabled-config test suite — that's the second half of
  axis (6), separate TB-N once ≥3 subpackages exist.

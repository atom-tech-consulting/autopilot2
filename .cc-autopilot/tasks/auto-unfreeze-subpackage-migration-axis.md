## Goal

Current focus: refactor features into opt-in components — axis (5)
`auto_unfreeze/` subpackage migration. Goal.md L194-195 names
`auto_unfreeze/` as the sixth migration in order ("registers tick
hook; depends on operator-queue which stays in core"). Relocate the
module body at `ap2/auto_unfreeze.py` into
`ap2/components/auto_unfreeze/__init__.py`, update the existing
`manifest.py` so its tick hook calls the intra-package symbol, and
clean up the `from . import auto_unfreeze` line in
`ap2/daemon.py:28-29` (part of the multi-line import block) so core
never imports from `ap2/components/`. The migration preserves the
existing kill switch env-knob name verbatim (goal.md L64-67) and
the `sweep()` signature.

Why now: `auto_unfreeze/` is the second-most-self-contained of the
remaining flat-module behaviors after `focus_advance/`. Its only
dependency is the operator-queue (which stays in core per goal.md
L194-195) — no channel-adapter dependency (unlike `attention/`,
goal.md L188), no ideation/cost guard coupling (unlike
`auto_approve/`, goal.md L196-197). Pairs naturally with axes 3
and other axis-5 migrations because none of them touch the
operator queue. Every cycle this flat module survives, the
component-vs-core boundary remains an asymmetric exemption rather
than a clean cleavage.

## Scope

- `git mv ap2/auto_unfreeze.py ap2/components/auto_unfreeze/__init__.py`
  (mirror janitor canary shape; manifest stays at
  `ap2/components/auto_unfreeze/manifest.py`).
- Update `ap2/components/auto_unfreeze/manifest.py` so the tick-hook
  wrapper resolves the call target intra-package (no `from ap2 import
  auto_unfreeze`).
- Update `ap2/daemon.py:26-41` — the multi-line `from . import (...)`
  block — to drop `auto_unfreeze` from the direct-import list. Any
  remaining call sites that referenced `auto_unfreeze.sweep` (or
  similar) read through the registry's manifest `hook_points`
  exposure. Core must not statically import from
  `ap2/components/auto_unfreeze/`.
- If `ap2/tests/` has any `from ap2 import auto_unfreeze` or
  `from ap2.auto_unfreeze import` references, update to the new
  symbol path (`ap2.components.auto_unfreeze` — tests are allowed
  to import directly per the import-direction gate's exemption
  rules; verify by inspecting
  `ap2/tests/test_core_import_direction.py`'s `_EXEMPT_FILES`).
- Add a regression pin
  `ap2/tests/test_tb314_auto_unfreeze_migration.py` with 3-5 tests
  asserting: (a) subpackage `__init__.py` exists, (b)
  `ap2/auto_unfreeze.py` is gone, (c) the manifest's tick hook
  executes `sweep` end-to-end against a stubbed board+queue, (d)
  the auto-unfreeze kill switch still suppresses the call when
  set (kill switch preserved — preserve the existing env-knob name
  verbatim, do not invent a new one).

## Design

Mirror the existing `janitor/` and (in-flight) `focus_advance/`
canary shapes: the manifest sits at
`ap2/components/<name>/manifest.py`, the runtime body sits at
`ap2/components/<name>/__init__.py`. The daemon's call sites
resolve symbols through the registry's manifest `hook_points`
dict (e.g. `hook_points["sweep"]`) rather than via direct
module-level alias rebinds. The operator-queue dependency stays in
core per goal.md L194-195 — `ap2/components/auto_unfreeze/__init__.py`
imports operator-queue helpers from core via the existing
`from .operator_queue import ...` shape; this is fine because
component → core imports are allowed (the import-direction gate
only forbids core → component direction). Look at the current
flat module to identify which core helpers the body uses and pin
those imports during the move.

## Verification

- `uv run pytest -q` — full suite passes (no observable behavior
  change for any auto-unfreeze code path).
- `uv run pytest -q ap2/tests/test_core_import_direction.py` —
  import-direction gate still passes; core does not import from
  `ap2/components/auto_unfreeze/`.
- `uv run pytest -q ap2/tests/test_tb310_tick_hook_protocol.py` —
  the existing tick-hook protocol regression continues to pass.
- `uv run pytest -q ap2/tests/test_tb314_auto_unfreeze_migration.py`
  — the new regression pin passes.
- `test ! -f ap2/auto_unfreeze.py` — flat module removed.
- `test -f ap2/components/auto_unfreeze/__init__.py` — subpackage
  body file present.
- `test -f ap2/components/auto_unfreeze/manifest.py` — manifest
  preserved.
- `! grep -nE '^[[:space:]]*auto_unfreeze,' ap2/daemon.py` — the multi-line `from . import (...)` block no longer lists `auto_unfreeze,` as a sibling import.
- `! grep -nE 'from \.auto_unfreeze|from ap2\.auto_unfreeze|^import ap2\.auto_unfreeze' ap2/daemon.py` — daemon no longer imports the flat module path.
- `! grep -nE 'from ap2 import auto_unfreeze' ap2/components/auto_unfreeze/manifest.py` — manifest no longer wraps the flat module.
- `ap2/components/auto_unfreeze/__init__.py` Prose: the auto-unfreeze kill switch env-knob name is preserved verbatim from the pre-migration flat module (goal.md L64-67); judge confirms by Read'ing both the migrated subpackage body and the prior flat-module symbol via `git show`.
- `ap2/components/auto_unfreeze/manifest.py` Prose: the manifest's `hook_points` dict exposes `sweep` (and any other symbols the daemon used to alias from the flat module) so core resolves them via the registry rather than direct import; judge confirms via Read of the manifest + the daemon call sites.

## Out of scope

- Migrating any of the other flat-module-backed components
  (`auto_approve/`, `attention/`, `focus_advance/`) — each is its
  own TB-N.
- Renaming the auto-unfreeze kill switch env knob (goal.md L64-67
  forbids).
- Moving the operator-queue into a component — goal.md L194-195
  explicitly keeps it in core.
- Adding a disabled-config test suite — that's the second half of
  axis (6), separate TB-N once ≥3 subpackages exist.

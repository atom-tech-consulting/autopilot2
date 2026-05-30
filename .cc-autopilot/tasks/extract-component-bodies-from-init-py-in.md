# Extract component bodies from __init__.py into impl.py (+ thin re-export)

Tags: #autopilot #components #refactor #oss-prep #canary

## Goal

Each of the 7 component subpackages under `ap2/components/<name>/`
carries its entire implementation (20-52 KB) directly in
`__init__.py` — the result of the axis-5 migration's
minimal-diff `git mv ap2/<name>.py → ap2/components/<name>/__init__.py`.
Folding a large module body into `__init__.py` is an unusual
convention: external readers expect `__init__.py` to be thin package
glue, not the implementation, so "where is this component's code?"
is a needless papercut — exactly the kind of thing that reads as
off to a contributor browsing the repo ahead of an open-source cut.

Normalize every component to the conventional shape: the body moves
to a sibling `impl.py`, and `__init__.py` becomes a thin re-export of
the public surface plus `MANIFEST`. Pure mechanical, history-
preserving (`git mv`), behavior-identical — the registry still
discovers via `manifest.py`, and each `manifest.py`'s existing
`from . import <symbols>` keeps resolving because the package
`__init__` re-exports them from `impl`.

Why now: it's the cleanest of the OSS-prep polish items, low-risk
and self-contained, and it doubles as the live canary for the
Opus 4.8 re-enable (2026-05-29) — a substantial, multi-file,
high-effort, many-turn agentic run is exactly the workload that
would surface a thinking-block round-trip 400 if one existed. If
this task completes cleanly on 4.8, the 4.8 incompatibility theory
is conclusively dead; if it 400s deterministically on a long turn,
that's the signal to re-open the SDK question. Operator-directed
2026-05-29; meta-infra, roadmap parked → `--skip-goal-alignment`.

## Scope

For EACH of the 7 components — `attention`, `auto_approve`,
`auto_unfreeze`, `focus_advance`, `janitor`, `mattermost`,
`validator_judge` (all under `ap2/components/<name>/`):

- `git mv ap2/components/<name>/__init__.py ap2/components/<name>/impl.py`
  to preserve blame/history on the body.
- Create a new thin `ap2/components/<name>/__init__.py` that
  re-exports the public surface from `.impl`. It must re-export
  every symbol that external callers (and the sibling
  `manifest.py`, which does `from . import <symbols>`) currently
  import from the package — at minimum every name the manifest
  references plus any symbol imported elsewhere in the tree via
  `from ap2.components.<name> import …`. Use explicit re-exports
  (`from .impl import a, b, c`) with an `__all__`, not a bare
  `from .impl import *`, so the public surface stays legible.
- Leave `manifest.py` UNCHANGED — its `from . import …` continues
  to resolve against the package `__init__`, which now forwards to
  `impl`. (Confirm by reading; do not rewrite manifest imports.)

- Do NOT change any component's behavior, env-flag, hook
  registration, or `MANIFEST`. This is a file-move + re-export
  refactor only.

## Design

- **Why impl.py + thin __init__, not a rename to <name>.py.**
  `impl.py` is unambiguous and avoids the doubled-name awkwardness
  of `focus_advance/focus_advance.py`. The thin `__init__.py`
  preserves `import ap2.components.<name>` and every existing
  `from ap2.components.<name> import X` call site with zero churn
  outside the package.

- **Registry + manifest are insulated.** The registry discovers
  components by importing each `<name>/manifest.py` and reading its
  module-level `MANIFEST`. `manifest.py` pulls runtime symbols via
  `from . import …`, which triggers the package `__init__` (now a
  re-export shim) — so discovery and hook resolution are unchanged.
  The TB-311 import-direction gate
  (`test_core_does_not_import_from_components`) is unaffected: no
  core module's imports change.

- **Re-export completeness is the one real risk.** If a symbol that
  some other module imports from the package isn't re-exported, that
  import breaks. Before finalizing each component, grep the tree for
  `from ap2.components.<name> import` and `from . import` (within the
  package) and ensure every imported name is in the new `__init__`'s
  re-export list. The full pytest suite is the backstop.

- **Behavior-preserving.** Same observable behavior as the
  focus-collapse / migration tasks before it — git-mv + a shim, no
  logic edits.

## Verification

- `uv run pytest -q ap2/tests/` — full suite passes (scoped to
  `ap2/tests/`, the project's canonical AP2_VERIFY_CMD). This is the
  backstop for re-export completeness across all 7 components.
- `[ "$(ls ap2/components/*/impl.py | wc -l | tr -d " ")" = "7" ]`
  — exactly 7 component `impl.py` files now exist.
- `! grep -rlE "^(async def|def|class) " ap2/components/attention/__init__.py ap2/components/auto_approve/__init__.py ap2/components/auto_unfreeze/__init__.py ap2/components/focus_advance/__init__.py ap2/components/janitor/__init__.py ap2/components/mattermost/__init__.py ap2/components/validator_judge/__init__.py`
  — no top-level function/class definitions remain in any component
  `__init__.py` (the bodies moved to impl.py; __init__ is now
  re-export-only).
- `uv run python -c "import ap2.registry as r; reg=r.Registry.discover(); print(sorted(c.name for c in reg.components))"`
  — the registry still discovers all 7 components after the move
  (prints the 7 names without ImportError).
- `uv run python -m ap2 --project . status` — `ap2 status` still
  renders its Components block (global `--project` flag precedes the
  `status` subcommand); exercises the registry + manifest load path
  end-to-end.
- `ap2/components/<name>/__init__.py` Prose: each of the 7 component
  `__init__.py` files is a thin re-export module — it imports the
  public surface from `.impl` (explicit names, with `__all__`) and
  contains no implementation logic; the moved body lives in the
  sibling `impl.py`. Judge confirms via Read of a representative
  sample (e.g. focus_advance + janitor).
- `ap2/components/<name>/manifest.py` Prose: the manifest files are
  unchanged — their `from . import …` statements still resolve (now
  via the __init__ re-export shim), and no manifest's `MANIFEST` /
  env-flag / hook registration was altered. Judge confirms via Read.

## Out of scope

- Renaming components or env knobs (`focus_advance` →
  `ideation_halt`, etc.) — separate cosmetic follow-up.
- Creating an `ap2/core/` subpackage (the other OSS-prep symmetry
  item) — separate, larger task.
- Any behavior, hook, manifest, or config-schema change — this is a
  file-move + re-export refactor only.
- Touching core modules or the import-direction gate — nothing
  outside `ap2/components/<name>/` changes.

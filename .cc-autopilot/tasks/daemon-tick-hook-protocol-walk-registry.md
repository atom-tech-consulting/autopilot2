# Daemon tick-hook protocol

## Goal

Establish the registry-driven tick-hook contract for the **Current
focus: refactor features into opt-in components**. Today `daemon._tick`
direct-imports `auto_approve`, `auto_unfreeze`, `attention`,
`focus_advance`, and `janitor` and calls their entry-points inline in
a hand-coded order (confirmed: `ap2/daemon.py:981` does `from .
import janitor as _janitor`, with parallel sites for the other four
modules). Per goal.md L132-144, axis (2) replaces those direct calls
with a registry walk so the daemon's tick loop becomes a phase-ordered
iteration over hooks registered by component manifests. Without this
contract, every component migration in axis (5) would still require
editing `daemon._tick` to insert the new call — defeating the
cleavage. The Done-when bullet "Every component can be independently
disabled via its env flag" (goal.md L60-63) starts being meaningful
only once daemon-side dispatch is registry-driven.

Why now: axis (2) is one of two independent unblockers for component
migrations in axis (5) per goal.md L216-217. Without the tick-hook
protocol, the canary's "componentness" is only half-real, and every
subsequent migration TB has to plumb a fresh ad-hoc call site instead
of extending a uniform contract.

## Scope

- Add a `TickHook` typed callable signature to the registry module
  (or a new `ap2/hooks.py` — agent chooses based on the registry
  shape already on disk). Signature matches the existing tick-call
  shape; the contract is `Callable[[Config, EventsFile], Awaitable[None]]`
  if tick hooks are async, sync otherwise (mirror what current
  direct calls use today).
- Define canonical phase names as an `Enum`: `PRE_DISPATCH`,
  `POST_DISPATCH`, `POST_CRON`, `ATTENTION_EMISSION` (mirroring
  goal.md L138-141 phase enumeration).
- Extend the manifest schema so a component can register tick hooks
  with `(phase, fn)` tuples — multiple hooks per component allowed.
- Add `registry.tick_hooks(phase)` returning the ordered list of
  hooks for that phase (deterministic order — alphabetical by
  component name unless the manifest declares a `depends_on`
  constraint that forces a topological sort within the phase).
- Refactor `daemon._tick` in `ap2/daemon.py` to walk
  `registry.tick_hooks(phase)` for each phase instead of direct
  calls into `auto_approve` / `auto_unfreeze` / `attention` /
  `focus_advance` / `janitor`. The non-janitor modules stay at
  their current flat-module path (`ap2/<name>.py`) for now. Each
  of the four non-janitor modules gets a temporary manifest stub
  in `ap2/components/<name>/manifest.py` that points its
  `tick_hook` at the existing flat-module function (e.g.
  `from ap2.auto_approve import maybe_apply; tick_hook =
  maybe_apply`). This isolates "daemon walks registry" from
  "every component is fully subpackage-migrated" — the structural
  move happens in axis (5).
- Preserve current observable behavior bit-for-bit: every hook
  fires in the same phase + same effective order it does today;
  env-flag disablement honors the existing knob names (goal.md
  L64-67); no new events, no behavior changes.

## Design

The stub-manifest pattern is the key design decision: rather than
blocking on every axis-(5) migration before any tick-hook benefit
ships, axis (2) lands the daemon-side registry walk NOW, with the
flat modules registered through stub manifests that re-export
their existing functions. When axis (5) migrates a module into
its own subpackage, the stub becomes a real manifest — daemon-side
code never changes.

After this lands:
- `daemon._tick` no longer has direct `from .auto_approve import ...`
  imports — registry-walked.
- `ap2/components/auto_approve/manifest.py` exists as a stub
  re-exporting `maybe_apply` from the still-flat `ap2/auto_approve.py`.
- A future TB-N migrating auto_approve fully into `ap2/components/`
  is a self-contained subpackage refactor — daemon._tick already
  walks the registry, so it doesn't change.

Phase ordering preserves what `_tick` does today; capture the
current order in a one-time test that pins it before refactoring,
so the regression is mechanical.

## Verification

- `uv run pytest -q` — full suite passes.
- `uv run pytest -q ap2/tests/test_tb211_event_types.py` — the
  existing janitor-event regression suite still passes (proves
  daemon-side janitor dispatch behavior is unchanged).
- A new regression-pin test the agent writes at
  `ap2/tests/test_tb310_tick_hook_protocol.py` that asserts:
  (a) `TickHook` callable signature exists and is importable from
  the registry; (b) the phase Enum has `PRE_DISPATCH`,
  `POST_DISPATCH`, `POST_CRON`, `ATTENTION_EMISSION` members;
  (c) `Registry.discover()` returns components named `janitor`,
  `auto_approve`, `auto_unfreeze`, `attention`, `focus_advance`
  (each with a `tick_hook` registered); (d) calling
  `registry.tick_hooks(PRE_DISPATCH)` returns a deterministic
  ordered list. Run via
  `uv run pytest -q ap2/tests/test_tb310_tick_hook_protocol.py`.
- `test "$(grep -nE '^\s*(from \.attention|from \.auto_approve|from \.auto_unfreeze|from \.focus_advance|from . import janitor)' ap2/daemon.py | wc -l)" = "0"` —
  daemon no longer direct-imports those modules at module-load
  time (registry lookup replaces them). Positive-form negation per
  ap2/howto.md absence-check guidance.
- `ap2/daemon.py` Prose: the `_tick` body walks
  `registry.tick_hooks(<phase>)` for each phase rather than calling
  `auto_approve.maybe_apply()` / `janitor.run_janitor()` etc.
  directly — judge confirms via Read.

## Out of scope

- Migrating non-janitor modules into `ap2/components/<name>/`
  subpackages (axis (5) — separate TB-Ns per migration). This TB
  lands manifest STUBS that point at the existing flat-module
  functions; the structural move happens in axis (5).
- Channel-adapter abstraction (axis (3) — separate TB).
- Validator pipeline as a list (axis (4) — separate TB).
- Import-direction CI gate (axis (6) — separate TB).
- Adding any new env knobs or renaming existing ones (goal.md
  L64-67 backwards-compat constraint).
- Changing the firing order or semantics of any tick hook;
  observable behavior is bit-for-bit preserved.

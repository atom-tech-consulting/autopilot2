---
title: "`auto_approve/` subpackage migration (axis 5 — final migration)"
tags: [autopilot, components, refactor, auto-approve, axis-5]
---

## Goal

Current focus: refactor features into opt-in components — finish the
axis-5 component migrations by relocating the last flat-path module
(`ap2/auto_approve.py`, 743 lines) into `ap2/components/auto_approve/`,
matching the now-canonical pattern from TB-313 (focus_advance),
TB-314 (auto_unfreeze), and TB-315 (attention). Per goal.md L196-197,
`auto_approve/` is sequenced LAST in the migration order because of
its largest blast radius (touches ideation, proposal labeling, retry
semantics, cost guards). With every other axis-5 sibling now landed,
this is the natural slot.

Conservative scope this cycle: do ONLY the file move + manifest update
+ daemon-alias rebind. The auto_approve manifest stub at
`ap2/components/auto_approve/manifest.py` flags in its docstring that
the inline per-task gate logic in `daemon._tick` belongs to axis-5
extraction — that extraction is deferred to a follow-up task because
each gate emits task-specific `auto_approve_paused` / `auto_approve_skipped`
/ `auto_approve_halted` events with observable payload, and conflating
the file-move with gate extraction risks behavior drift. This task is
purely the structural relocation.

Why now: TB-315 (attention) shipped 2026-05-28T09:46Z and TB-316
(validator_judge) shipped 2026-05-28T10:07Z. All other axis-5
migrations are done. Goal.md L196-197 explicitly sequences `auto_approve`
LAST; with siblings landed, the canary-shape repetition is most
efficient now (5 prior migrations have grooved the pattern) and
deferring lets the flat path linger as a goal-named gap.

## Scope

- Git-mv `ap2/auto_approve.py` → `ap2/components/auto_approve/__init__.py`
  (use `git mv` so blame history is preserved).
- Rewrite `ap2/components/auto_approve/manifest.py`:
  - Drop the no-op `_tick_hook` placeholder and its docstring rationale.
  - Source symbols intra-package via `from . import ...`.
  - Populate `hook_points` with every symbol that `ap2/daemon.py`
    currently rebinds as a module-level alias from `auto_approve.*`
    (see `ap2/daemon.py` L1760-1776 — at minimum:
    `_AUTO_APPROVE_FAILURE_STATUSES`, `_AUTO_APPROVE_UNFREEZE_TOKEN`,
    `_AUTO_APPROVE_WINDOW_RESUME_TOKEN`, `_AUTO_APPROVE_WINDOW_S`,
    `_append_decisions_needed_bullet`, `_auto_approve_already_halted`,
    `_auto_approve_check_violations`, `_auto_approve_freeze_threshold`,
    `_auto_approve_paused`, `_auto_approve_window_resume_idx`,
    `_auto_approved_task_ids`, `_event_combined_tokens`,
    `_parse_event_ts`, `_per_task_token_cap`,
    `_validator_judge_noisy_paused`, `_was_auto_approved`,
    `_window_token_cap`).
  - Keep `env_flag=None`, `default_enabled=True` (matches existing
    stub; goal.md L196-197 doesn't mandate adding a manifest-level
    kill switch — that's an open operator question surfaced in
    ideation_state.md).
  - Keep the `tick_hooks=[(Phase.POST_DISPATCH, _tick_hook)]`
    registration; the hook can stay a no-op for this cycle since
    the inline gate stays inline.
- Edit `ap2/daemon.py`:
  - Drop `auto_approve,` from the relative-import block at L27.
  - Replace each `<alias> = auto_approve.<symbol>` line at L1760-1776
    with a `default_registry().get("auto_approve").hook_points["<symbol>"]`
    lookup (one per alias). Mirror the exact rebinding pattern from
    TB-314's `auto_unfreeze/` migration (commit 73f5a52) and TB-313's
    `focus_advance/` migration (commit 6b4fcea).
- Update test imports: any test file that imports
  `from ap2 import auto_approve` or `from ap2.auto_approve import ...`
  → switch to `from ap2.components.auto_approve import ...` (locate
  via `grep -rl 'ap2.auto_approve\|from ap2 import auto_approve' ap2/tests/`).
- Add `ap2/tests/test_tb318_auto_approve_migration.py` (regression pin,
  matching the TB-313 / TB-314 / TB-315 shape):
  - Structural: `ap2/auto_approve.py` does not exist;
    `ap2/components/auto_approve/__init__.py` exists and is non-empty.
  - Registry: `default_registry().get("auto_approve")` returns a
    Manifest with `name="auto_approve"`, `env_flag=None`,
    `default_enabled=True`.
  - Hook points: the manifest's `hook_points` dict contains every
    symbol the daemon rebinds at L1760-1776 (parametrize over the
    list and assert each is callable / non-None).
  - Daemon resolution: importing `ap2.daemon` does not raise; each
    rebound alias on the daemon module evaluates to the same object
    as `default_registry().get("auto_approve").hook_points[name]`.
  - Import-direction gate: `from ap2.tests.test_core_import_direction
    import test_core_does_not_import_from_components`; assert it
    still passes (no static `from ap2.components.auto_approve` in
    any core file).
- Preserve every existing auto_approve env knob name verbatim
  (`AP2_AUTO_APPROVE`, `AP2_AUTO_APPROVE_DRY_RUN`,
  `AP2_AUTO_APPROVE_WINDOW_S`, `AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP`,
  `AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP`, the cost-guard knobs, etc.) —
  goal.md L64-67 constraint.

## Design

Identical to the TB-313 / TB-314 / TB-315 canary pattern, which has
shipped three back-to-back successful migrations:

1. **File move**: `git mv ap2/auto_approve.py
   ap2/components/auto_approve/__init__.py` (existing stub
   `__init__.py` at the target path is empty — just delete it before
   the mv, or `git mv -f`). Body is unchanged.

2. **Manifest rewrite**: Replace the stub manifest's `_tick_hook`
   no-op + docstring rationale with intra-package imports + a
   `hook_points` dict that exposes every symbol the daemon currently
   alias-rebinds. The Manifest constructor call keeps its existing
   shape (the docstring mentions `tick_hooks=[(Phase.POST_DISPATCH,
   _tick_hook)]` — keep the no-op tick hook in place since the inline
   gate stays inline this cycle).

3. **Daemon rebind**: The 17 alias lines at `ap2/daemon.py` L1760-1776
   change from `<alias> = auto_approve.<symbol>` to
   `<alias> = default_registry().get("auto_approve").hook_points["<symbol>"]`.
   Verify by running `grep -n "auto_approve\." ap2/daemon.py` after
   the edit — there should be ZERO remaining direct references.

4. **Test repointing**: Update any `from ap2 import auto_approve` or
   `from ap2.auto_approve` to `from ap2.components.auto_approve`. The
   import-direction gate (TB-311) is restricted to non-test core
   modules so test files are exempt.

5. **Inline gate stays inline**: `daemon._tick`'s per-task gate block
   (which calls `_was_auto_approved`, `_auto_approve_paused`,
   `_validator_judge_noisy_paused`, etc.) is NOT extracted in this
   task. It continues to call the rebound aliases. The manifest's
   `_tick_hook` remains a no-op POST_DISPATCH placeholder. Reasoning:
   each gate emits task-specific events with observable payload;
   extracting them to a single tick-callable risks behavior drift,
   and the stub manifest docstring already flags this as a separate
   refactor. Conservative-first.

## Verification

- `uv run pytest -q ap2/tests/test_tb318_auto_approve_migration.py` — new TB-318 regression pin passes.
- `uv run pytest -q ap2/tests/test_core_import_direction.py` — import-direction gate still green; no core file statically imports from `ap2.components.auto_approve`.
- `uv run pytest -q ap2/tests/test_tb310_tick_hook_protocol.py` — TB-310 tick-hook protocol pin still green.
- `uv run pytest -q ap2/tests/test_components_disabled.py` — TB-317 disabled-config gate still green.
- `uv run pytest -q ap2/tests/` — full suite passes.
- `test ! -f ap2/auto_approve.py` — flat module is gone from the source tree.
- `test -s ap2/components/auto_approve/__init__.py` — relocated module body exists and is non-empty.
- `! grep -nE '^from \.\s+import\s+auto_approve\b' ap2/daemon.py` — daemon no longer flat-imports `auto_approve`.
- `! grep -nE '\bauto_approve\.[A-Za-z_]' ap2/daemon.py` — daemon has zero remaining `auto_approve.<symbol>` references (every alias rebinds through the registry).
- `ap2/components/auto_approve/manifest.py` Prose: the rewritten manifest sources symbols intra-package via `from . import …` and populates `hook_points` with at least the 17 symbols listed in Scope. Judge confirms via Read against the file.
- `ap2/daemon.py` Prose: the 17 alias lines at L1760-1776 (or wherever they live post-edit) each rebind through `default_registry().get("auto_approve").hook_points["<symbol>"]`. Judge confirms via Read + Grep that no `auto_approve.<symbol>` direct references remain.

## Out of scope

- Extracting the inline per-task auto-approve gate from `daemon._tick`
  into the manifest's tick hook. The stub manifest docstring flags
  this as part of axis-5 scope, but it carries observable-behavior
  risk (per-task event payloads) and is a separate refactor. Operator
  decides whether to follow up.
- Adding a manifest-level master kill-switch env flag to the
  auto_approve component. Today's `env_flag=None` matches the stub;
  whether to add a master switch is an open operator question
  surfaced in `ideation_state.md` `## Decisions needed from operator`.
- Any env-knob renames or new env knobs beyond what already exists in
  flat `ap2/auto_approve.py`. Goal.md L64-67 constraint.
- Refactoring the cost-guard window-cap logic, the consecutive-freeze
  pause logic, or any other internal auto_approve behavior. Pure
  structural relocation.

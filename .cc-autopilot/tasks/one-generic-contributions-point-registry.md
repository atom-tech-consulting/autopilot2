# TB-387: One generic contributions(point) registry accessor; delete bespoke per-kind methods

## Goal

Advance the focus "get the component boundary right — loop-level participants
only" toward goal.md's Done-when bullet: "A CI gate fails the build if any core
module directly imports from ap2/components/<name>/. All cross-references flow
through the registry's single generic accessor — no per-kind registration methods,
and no core to component hook_points symbol lookups." Replace the registry's
bespoke per-extension-kind fan-out methods with a single generic
`contributions(point)` accessor; keying stays consumer-local (the registry never
does keyed dispatch).

Why now: with the judge accessors removed (TB-386) the registry's only remaining
bespoke fan-out methods are `channel_adapters()` and `cron_job_handlers()`;
collapsing them now — before the communication and ideation extractions add new
call sites — prevents each new component from minting another one-off accessor and
re-growing the clutter the boundary refactor exists to remove.

## Scope

- Add a single generic `contributions(point: str, cfg=None)` accessor on
  `Registry` that walks manifests in name-sorted order and returns the fan-out of
  each manifest's `hook_points.get(point)` — list-merge for list-shaped points,
  dict-merge for dict-shaped points — preserving each current method's
  "walk-all vs walk-enabled" semantics.
- Migrate the `channel_adapters()` and `cron_job_handlers()` call sites to the new
  accessor and DELETE both bespoke methods from `ap2/registry.py`.
- Keep dispatch/keying in the consumer (e.g. the cron scheduler still does
  `handlers.get(job.name, DEFAULT)`); the registry only fans out.

## Design

- Current bespoke methods: `channel_adapters(cfg)` (`ap2/registry.py:359`) and
  `cron_job_handlers()` (`ap2/registry.py:513`). TB-386 removes
  `briefing_validators()` and `verifier_judge()` first (this task is blocked on it),
  so the collapse operates over the final set.
- `tick_hooks(phase)` (`ap2/registry.py:483`) is the existing phase walk; whether
  it also folds under `contributions(point)` is the implementer's call, but the two
  named fan-out methods above MUST be gone.
- Call sites to migrate: channel → `ap2/daemon.py:2137`, `ap2/watchdog.py:96`/`125`,
  `ap2/smoke_runner.py:163`, `ap2/components/attention/impl.py:1155`; cron handlers
  → `ap2/components/cron/impl.py:89`. Determinism (name-sorted order) is
  load-bearing and must be preserved.

## Verification

- `! grep -rn 'def channel_adapters' ap2/registry.py` — bespoke channel_adapters() method removed.
- `! grep -rn 'def cron_job_handlers' ap2/registry.py` — bespoke cron_job_handlers() method removed.
- `! grep -rn 'def briefing_validators' ap2/registry.py` — no per-kind judge accessor remains (also removed by TB-386).
- `! grep -rn 'def verifier_judge' ap2/registry.py` — no per-kind judge accessor remains (also removed by TB-386).
- `grep -rn 'def contributions' ap2/registry.py` — the single generic accessor exists.
- `uv run pytest -q ap2/tests/` — the full suite passes.
- `ap2/registry.py` Prose: `contributions(point)` is a fan-out-only accessor (returns the merged contributions and performs no keyed dispatch) and preserves name-sorted determinism; judge confirms via Read.
- `ap2/components/cron/impl.py` Prose: the cron scheduler resolves its job handlers through the generic accessor and still keys dispatch locally via `handlers.get(job.name, ...)`; judge confirms via Read/Grep.

## Out of scope

- Demoting the LLM judges (TB-386, predecessor).
- Removing the daemon's hook_points symbol-pull alias blocks and the dead
  POST_DISPATCH phase (TB-388).
- The communication component / killing `channel_adapters` from core entirely
  (TB-389); if TB-389 lands first this task simply has fewer call sites to migrate.
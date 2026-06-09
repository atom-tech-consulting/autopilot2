# TB-387: One generic contributions(point) registry accessor; fold cron_job_handlers onto it

## Goal

Advance the focus "get the component boundary right — loop-level participants
only" toward goal.md's component-model Done-when bullet — "A CI gate fails the
build if any core module directly imports from ap2/components/<name>/. All
cross-references flow through the registry's single generic accessor — no
per-kind registration methods, and no core→component hook_points symbol
lookups." Introduce a single generic
`contributions(point)` accessor on the registry (fan-out only; keying stays
consumer-local — the registry never does keyed dispatch) and migrate the cron
job-handler surface onto it, deleting the bespoke `cron_job_handlers()` method.

Scope boundary with the siblings — the registry's other bespoke fan-out methods
are removed by sibling tasks, NOT folded here:
- `briefing_validators()` / `verifier_judge()` → removed by TB-386 (predecessor).
- `channel_adapters()` → removed by TB-389. Channels are owned wholly by the
  communication component (TB-389 internalizes them into the component's own
  registry), so they never become a core `contributions(point)` extension point.
  This task must NOT migrate `channel_adapters()` into the generic accessor.

Only cross-component surfaces that legitimately stay in core (cron job handlers —
contributed by the janitor component AND core) get a generic point. That is the
discriminator: a surface earns a core `contributions(point)` only if multiple
owners feed it and it stays in core; a surface owned wholly by one component is
internal to that component, not a core point.

Why now: with the judge accessors gone (TB-386) and channels internalizing
(TB-389), `cron_job_handlers()` is the one remaining cross-component fan-out that
belongs in core. Establishing the single generic verb now — before the ideation
extraction adds new call sites — prevents each new component from minting another
one-off accessor and re-growing the clutter the boundary refactor exists to remove.

## Scope

- Add a single generic `contributions(point: str, cfg=None)` accessor on
  `Registry` that walks manifests in name-sorted order and returns the fan-out of
  each manifest's `hook_points.get(point)` — list-merge for list-shaped points,
  dict-merge for dict-shaped points — preserving the current "walk-all vs
  walk-enabled" semantics.
- Migrate the `cron_job_handlers()` call site to the new accessor and DELETE the
  bespoke `cron_job_handlers()` method from `ap2/registry.py`.
- Do NOT touch `channel_adapters()` — it is removed by TB-389 (by internalizing
  channels into the communication component, not by folding into `contributions`).
  If TB-389 has already landed, `channel_adapters()` is simply gone and there is
  nothing to do for channels here; if it has not, leave the method in place for
  TB-389 to remove.
- Keep dispatch/keying in the consumer (the cron scheduler still does
  `handlers.get(job.name, DEFAULT)`); the registry only fans out.

## Design

- Bespoke method to remove here: `cron_job_handlers()` (`ap2/registry.py:513`).
  `channel_adapters(cfg)` (`ap2/registry.py:359`) is OUT of scope — TB-389 owns it.
  TB-386 removes `briefing_validators()` / `verifier_judge()` first (this task is
  blocked on it), so the generic accessor lands over the remaining set.
- `tick_hooks(phase)` (`ap2/registry.py:483`) is the existing phase walk; whether
  it also folds under `contributions(point)` is the implementer's call, but
  `cron_job_handlers()` MUST be gone.
- Call site to migrate: cron handlers → `ap2/components/cron/impl.py:89`. The
  channel call sites (`ap2/daemon.py:2137`, `ap2/watchdog.py`,
  `ap2/smoke_runner.py`, `ap2/components/attention/impl.py`) belong to TB-389, not
  this task. Determinism (name-sorted order) is load-bearing and must be preserved.

## Verification

- `! grep -rn 'def cron_job_handlers' ap2/registry.py` — bespoke cron_job_handlers() method removed.
- `grep -rn 'def contributions' ap2/registry.py` — the single generic accessor exists.
- `grep -rn 'contributions(' ap2/components/cron/impl.py` — the cron scheduler resolves job handlers through the generic accessor.
- `uv run --extra dev pytest -q ap2/tests/ --ignore=ap2/tests/smoke` — the full suite passes.
- `ap2/registry.py` Prose: `contributions(point)` is a fan-out-only accessor (returns the merged contributions and performs no keyed dispatch), preserves name-sorted determinism, and `channel_adapters()` is left untouched (owned by TB-389); judge confirms via Read.
- `ap2/components/cron/impl.py` Prose: the cron scheduler resolves its job handlers through the generic accessor and still keys dispatch locally via `handlers.get(job.name, ...)`; judge confirms via Read/Grep.

## Out of scope

- Demoting the LLM judges and removing `briefing_validators()` / `verifier_judge()` (TB-386, predecessor).
- Removing the daemon's hook_points symbol-pull alias blocks and the dead POST_DISPATCH phase (TB-388).
- `channel_adapters()` / `inbound_poll` and the communication component (TB-389) — channels are internalized there, NOT folded into `contributions(point)`; this task must not migrate `channel_adapters()`.

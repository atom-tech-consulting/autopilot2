# Consolidate `_now()` and `_read_pid()` into `ap2/_shared.py` (operator-filed below-threshold; bundle once shared module exists)

Tags: `#autopilot` `#code-quality` `#reusability` `#code-cleanness`

## Goal

Goal-anchor: goal.md's **Current focus: code quality** focus's (3) **Code reusability** axis (L74-77). Bundle two threshold-two duplications into the shared module created by the `_locked` / `_short` extraction tasks, while the shared module is fresh and the migration cost is at its lowest. **This task is explicitly below goal.md L74-77's threshold-three rule** (n=2 for each duplication) — operator-filed knowing that, on the reasoning that an n=2 case adjacent to an n=3 extraction has zero marginal cost (one file already getting touched, both duplicate pairs are byte-identical, four call sites total). If goal-aligned operator review judges this is over-extracting at n=2 and rejects, that's the correct signal — the task documents the n=2 cases so future reviewers can decide if/when the threshold is met.

The two cases:
- `_now()` — UTC ISO-8601 timestamp helper. Defined in `ap2/cron.py:268` and `ap2/events.py:17`. Bodies are functionally identical (cron.py imports `datetime` inside the function, events.py uses a module-level import — same external behavior).
- `_read_pid(cfg)` — read daemon PID from `cfg.pid_file`. Defined in `ap2/cli.py:25` and `ap2/web.py:273`. Bodies are byte-identical.

Why now: blocked on `_locked` and/or `_short` extraction landing first (those create the shared module). Once `ap2/_shared.py` exists with 1-2 helpers, adding two more is ~6 lines of code + four migration sites — minimal incremental cost. Filing now so it's queued behind the threshold-three predecessors, not forgotten.

## Scope

(1) **PRECONDITION**: `ap2/_shared.py` must already exist (created by the `_locked` extraction OR the `_short` extraction OR both). If neither has landed yet, this task is `@blocked` on at least one of them. Do not create `ap2/_shared.py` from this task alone — that would race with the sibling tasks. Wait for one of them to land first; the operator queues this task only after that's confirmed.

(2) Extend `ap2/_shared.py` with two helpers:
  - `now() -> str` — returns `dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")`. Module-level `import datetime as dt` at the top of `_shared.py` (matches `events.py`'s pattern).
  - `read_pid(cfg: Config) -> int | None` — reads `cfg.pid_file`, returns the int PID or None. Handles `FileNotFoundError` / `ValueError` / `OSError` exactly as both current call sites do.

(3) Migrate the four call sites:
  - `ap2/cron.py:268` — delete local `_now`; import `now` from `ap2._shared`; update callers in cron.py to use the new name.
  - `ap2/events.py:17` — delete local `_now`; import `now`. Note: `events.py` has a module-level `import datetime as dt` that's used elsewhere — keep it.
  - `ap2/cli.py:25` — delete local `_read_pid`; import `read_pid`.
  - `ap2/web.py:273` — delete local `_read_pid`; import `read_pid`.

(4) Don't change behavior at any call site — pure rename + import refactor. The two `_now` variants produce byte-identical output (same ISO-8601 format with `Z` suffix). The two `_read_pid` variants are byte-identical bodies.

(5) Don't include `_truncate()` from `daemon.py:1032` and `events.py:193` in this consolidation — those have THE SAME NAME but DIFFERENT SEMANTICS (daemon's is simple slice; events' is strip + safe-truncate). Bundling them is a behavior change, not a dedup. Separate task if/when n=3 motivates the unification under a chosen semantic.

## Design

The "operator-filed-below-threshold" framing is deliberate: this is operator judgment, not a goal-rule application. Goal.md L74-77 says threshold-three, premature abstraction is its own failure mode. The operator's reasoning for filing at n=2 anyway:
- Migration is **mechanical and byte-identical-body** for `_read_pid` and **functionally identical** for `_now` (the inline-vs-module-level `import dt` is a stylistic choice with no runtime difference). Risk is low.
- The shared module is being created *anyway* by the n=3 tasks. Adding two more helpers to it has zero marginal architectural cost.
- If a third call site for `_now` or `_read_pid` arrives later, it'll naturally use the shared helper — no separate "promote to shared" step.

The opposing reasoning (and grounds to reject this task if the operator/reviewer disagrees): pre-emptive consolidation at n=2 might paint over a future divergence. If a third call site arrives and wants subtly different behavior (e.g. a different timestamp precision, a different PID-read fallback), the shared helper has to either grow optional parameters (smell) or get re-forked (defeating the consolidation). At n=3, the third call site forces the abstraction; at n=2, it's speculation.

Operator pick: file the task, let reviewer decide. The TB exists either way — if rejected, the rejection reason ("over-extracting at n=2 per L74-77") feeds the operator_log signal and tunes future ideation.

## Verification

- `uv run pytest -q ap2/tests/` — full suite green (exit 0).
- `test -f ap2/_shared.py` — exits 0 (precondition: the shared module exists).
- `grep -nE "^def now\(|^def read_pid\(" ap2/_shared.py` — exits 0 with both lines matched (both helpers present by these exact names).
- `! grep -nE "^def _now\(\)|^def _read_pid\(cfg" ap2/cron.py ap2/events.py ap2/cli.py ap2/web.py` — exits 0 (zero matches; the four local definitions are deleted).
- `[ "$(grep -lE 'from ap2\._shared import .*(now|read_pid)' ap2/cron.py ap2/events.py ap2/cli.py ap2/web.py | wc -l)" -eq 4 ]` — exactly four files import from the shared module (the four migrated callers).
- Prose: the two helpers' module docstrings (or inline comments at definition) name the migration provenance — i.e., `now` was previously `_now` in cron.py + events.py; `read_pid` was previously `_read_pid` in cli.py + web.py. Aids future grep when someone wonders "why is this shared." Judge confirms via `Read` of `ap2/_shared.py`.

## Out of scope

- Consolidating `_truncate()` from daemon.py + events.py — different semantics under the same name (separate cleanup, not a dedup).
- Adding optional parameters to `now()` for non-UTC timezones, custom format strings, etc. — neither current caller needs them.
- Adding a `write_pid()` companion to `read_pid()` — only daemon.py writes the PID, single call site, no extraction motivation.
- Scanning the codebase for other below-threshold duplication candidates to bundle (`_atomic_write_json`, `_walk_history`, etc.) — handled by ideation when/if they hit n=3.

# Extract `_locked()` to `ap2/_shared.py`; replace 3 duplicate definitions with imports

Tags: `#autopilot` `#code-quality` `#reusability` `#regression-pin`

## Goal

Close goal.md's **Current focus: code quality** focus's (3) **Code reusability** axis (L74-77: "when a piece of logic appears at three or more call sites with structural similarity, extract to a shared helper") on the oldest threshold-three case in the codebase: `_locked(path)` — an fcntl file-locking context manager — is defined three times (`ap2/board.py:109`, `ap2/cron.py:124`, `ap2/retry.py:17`) with two semantic variants. The function predates the current focus rotation, so ideation's reactive-on-recent-task-signal cycles haven't surfaced it; the threshold is met today regardless. The two variants have non-obvious semantic difference (board.py locks the file itself; cron.py / retry.py lock a sidecar `.lock` file) that the shared extraction should preserve and document — collapsing them into one helper would change behavior at one of the three sites.

Why now: ideation's reusability rule fires when a NEW (3rd) call site is being added (TB-209 triggered this way), not retroactively for pre-existing n=3 duplications. This is the operator-curated retrospective scan that surfaces the existing case. Without extraction, the next module that needs file locking has to choose between three near-identical definitions and risks copying the wrong variant — exactly the "copy-pasted instead of shared" failure mode L77 names.

## Scope

(1) Create `ap2/_shared.py` (if not already created by a sibling task; otherwise extend) with two `@contextmanager`-decorated helpers:
  - `locked_inplace(path: Path) -> Iterator[int]` — locks the file at `path` directly. Matches `board.py`'s current shape.
  - `locked_sidecar(path: Path) -> Iterator[int]` — locks a sibling `path.with_suffix(path.suffix + ".lock")` file. Matches `cron.py` and `retry.py`'s current shape.

  Both helpers create parent directories with `mkdir(parents=True, exist_ok=True)` before opening the lock fd (mirrors the existing implementations).

(2) Migrate the three call sites:
  - `ap2/board.py:109` — delete the local `_locked` definition; import `locked_inplace` from `ap2._shared` and update call sites in board.py to use the new name.
  - `ap2/cron.py:124` — delete local definition; import `locked_sidecar`.
  - `ap2/retry.py:17` — delete local definition; import `locked_sidecar`.

(3) Add a module docstring to `ap2/_shared.py` that names the two variants and the semantic difference (one helper locks the file itself; the other locks a `.lock` sidecar so the locked file can be safely truncated/rewritten under the lock).

(4) Don't change locking semantics at any call site — the migration is a pure rename + import refactor. If a behavior change is wanted (e.g. unifying board.py onto sidecar locking), that's a separate task with its own risk analysis.

## Design

Two helpers (not one with a flag) because:
- The two locking modes are semantically distinct, not parameter variants of the same operation. A `locked(path, sidecar=True)` flag muddles the difference; two named functions force the caller to make the choice explicit at the import site.
- All three current call sites pick one variant and stick with it — no caller toggles dynamically.

Module name `ap2/_shared.py` (underscore-prefixed = internal; short, generic-shape header for further shared helpers as they accumulate). Don't re-export from `ap2/__init__.py` — internal helpers stay internal.

Sequencing with sibling tasks: if another operator-filed task (`_short` extraction, Tier 2 bundle, etc.) lands first and creates `ap2/_shared.py`, this task extends the existing module rather than recreating it. The `(1)` "create or extend" wording is intentional — neither task depends on the other landing first.

## Verification

- `uv run pytest -q ap2/tests/` — full suite green (exit 0); no regression in modules that called the locking helpers.
- `test -f ap2/_shared.py` — exits 0; the shared module exists.
- `grep -nE "^def locked_inplace\(|^def locked_sidecar\(" ap2/_shared.py` — exits 0 with both lines matched (the two helpers are present by these exact names).
- `! grep -nE "^def _locked\(" ap2/board.py ap2/cron.py ap2/retry.py` — exits 0 (zero matches; the three local `_locked` definitions are deleted). The `!` inverts the no-match exit so the verifier reads pass-on-zero-matches per the TB-187 idiom.
- `[ "$(grep -lE 'from ap2\._shared import' ap2/board.py ap2/cron.py ap2/retry.py | wc -l)" -eq 3 ]` — exactly three files import from the shared module (the three migrated callers).
- Prose: the three migrated files (`ap2/board.py`, `ap2/cron.py`, `ap2/retry.py`) no longer import `fcntl` directly — it's encapsulated in `ap2/_shared.py`. Judge confirms via `Read` of each file's imports. Acceptable if one file keeps the import for a non-locking use case (judge reviews and reports which file + why if so).
- Prose: the module docstring of `ap2/_shared.py` names the semantic distinction between `locked_inplace` and `locked_sidecar` — specifically, that sidecar locking permits the locked file to be safely rewritten/truncated under the lock while inplace locking holds an fd on the file itself. Judge confirms via `Read` of the top-of-module docstring.

## Out of scope

- Unifying the three call sites onto a single locking variant (semantic change; would require behavior verification at each call site — out of this refactor's purity).
- Adding new locking primitives (advisory vs mandatory, blocking vs non-blocking, timeouts) — current callers don't need them.
- Re-exporting from `ap2/__init__.py`.
- Migrating other file-IO helpers (`_atomic_write_json` in tools.py, `_locked`-adjacent patterns in daemon.py) — separate threshold-check; only `_locked` meets n=3 today.

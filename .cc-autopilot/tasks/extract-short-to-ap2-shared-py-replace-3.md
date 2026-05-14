# Extract `_short()` to `ap2/_shared.py`; replace 3 duplicate definitions with imports

Tags: `#autopilot` `#code-quality` `#reusability` `#regression-pin`

## Goal

Close goal.md's **Current focus: code quality** focus's (3) **Code reusability** axis (L74-77: "when a piece of logic appears at three or more call sites with structural similarity, extract to a shared helper") on a textbook threshold-three case: `_short(v, limit)` — a string-or-value truncation helper that returns the input unchanged if it fits the limit, otherwise truncates and appends a horizontal-ellipsis (`…`) — is defined three times (`ap2/cli.py:1510`, `ap2/diagnose.py:378`, `ap2/events.py:86`) with byte-identical bodies. The only difference is the default `limit` argument (120 / 100 / 200) — three local conventions encoding "what counts as too long" at each site. Extracting collapses three identical bodies into one and forces each caller to be explicit about its preferred limit at the call site (a clarity win independent of the dedup).

Why now: ideation's threshold-three rule fires on NEW 3rd-call-site arrivals, not retroactive scans of pre-existing duplication. This case has been at n=3 longer than the current focus rotation has existed. The bodies are byte-identical — migration risk is the lowest in the codebase.

## Scope

(1) Create `ap2/_shared.py` (if not already created by a sibling task; otherwise extend) with one helper:
  - `short(v: Any, limit: int) -> str` — returns `str(v)` unchanged if `len(str(v)) <= limit`, otherwise returns `str(v)[:limit-1] + "…"`. No default `limit` argument — callers pick explicitly (the prior convention of three different module-local defaults was a smell, not a feature).

(2) Migrate the three call sites:
  - `ap2/cli.py:1510` — delete local `_short` (default 120). Find each call site and pass `limit=120` explicitly (or just `120` positional). Import `short` from `ap2._shared`.
  - `ap2/diagnose.py:378` — delete local `_short` (default 100). Same migration with `limit=100`.
  - `ap2/events.py:86` — delete local `_short` (default 200). Same migration with `limit=200`.

(3) The horizontal-ellipsis character (`…`, U+2026) is preserved — that's the existing visual signal at every call site. Don't switch to `...` (three dots) or any other truncation marker.

(4) Don't introduce a default `limit` argument on the extracted helper, even though Python's signature would allow it. The three different module-local defaults each made sense at their own call site; collapsing to ONE default would impose one site's choice on the others without merit. Explicit > implicit.

## Design

One helper (not three named variants) because the bodies are byte-identical and the only variation is the limit — that's the canonical "extract a function, parameterize over the difference" pattern. No semantic variants to preserve.

Module name `ap2/_shared.py` — commutative with sibling extraction tasks. Don't re-export from `ap2/__init__.py`.

Naming: drop the underscore prefix when moving to the shared module. The underscore on the original three definitions signaled "module-internal"; once it's in a shared internal module, the underscore is on the MODULE name (`_shared`) rather than the function name. Each call site imports `from ap2._shared import short` and uses `short(value, 120)` — reads cleanly.

Sequencing: independent of the `_locked` extraction task; either order works. If both queue up, the second one extends the module the first one created.

## Verification

- `uv run pytest -q ap2/tests/` — full suite green (exit 0).
- `test -f ap2/_shared.py` — exits 0; shared module exists.
- `grep -nE "^def short\(" ap2/_shared.py` — exits 0; the `short` helper is present by exactly that name.
- `! grep -nE "^def _short\(" ap2/cli.py ap2/diagnose.py ap2/events.py` — exits 0 (zero matches; the three local definitions are deleted). The `!` inverts the no-match exit so the verifier reads pass.
- `[ "$(grep -lE 'from ap2\._shared import .*short' ap2/cli.py ap2/diagnose.py ap2/events.py | wc -l)" -eq 3 ]` — exactly three files import `short` from the shared module (the three migrated callers).
- `[ "$(grep -cE '\b_short\(' ap2/cli.py ap2/diagnose.py ap2/events.py | grep -v ':0$' | wc -l)" -eq 0 ]` — zero files still reference the old `_short` name (all callers renamed to `short`). The trailing `grep -v ':0$'` filters out files with zero matches; `wc -l` counts remaining lines (files with non-zero matches); the test asserts this is zero.
- Prose: the `…` horizontal-ellipsis character (U+2026) is preserved as the truncation marker in the extracted helper. Judge confirms via `Read` of `ap2/_shared.py` that the literal `"…"` appears in the helper body.

## Out of scope

- Adding a module-level default `limit` to the extracted helper.
- Migrating other truncation-shape helpers (`_truncate` in daemon.py and events.py — those have different semantics under the same name and are a separate cleanup, not an extraction).
- Re-exporting from `ap2/__init__.py`.
- Audit-grep across the codebase for other truncation patterns that could converge on this helper (premature consolidation; only the three existing `_short` defs are in scope).

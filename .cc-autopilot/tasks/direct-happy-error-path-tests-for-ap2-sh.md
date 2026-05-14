# Direct happy + error path tests for `ap2/_shared.py` helpers (`locked_inplace`, `locked_sidecar`, `short`, `now`, `read_pid`)

Tags: #autopilot #tests #code-quality #reusability #regression-pin

## Goal

Close the testing-axis follow-up to TB-217/TB-218/TB-220 — Current focus: code quality (testing axis, goal.md L58-63): `ap2/_shared.py` shipped 5 helpers between 07:01-07:44Z today (`locked_inplace`, `locked_sidecar`, `short`, `now`, `read_pid`) and 7 modules now import from it (`board/cli/cron/diagnose/events/retry/web`), but `grep -rn "from ap2._shared" ap2/tests/` returns zero — no test file directly exercises these helpers. The contracts ride on implication via callers, which leaves the most bug-prone semantics (the `locked_sidecar` vs `locked_inplace` on-disk distinction, `short`'s ellipsis-boundary, `read_pid`'s exception fallback) without a regression pin.

Why now: TB-217/TB-218/TB-220 closed today — this is the first cycle where the shared module's contracts can be pinned directly. Goal.md L58-63's testing-axis criterion is "every shipped … behavior has automated tests pinning the happy path AND at least one error path"; while `_shared.py` is infrastructure rather than a CLI/MCP surface, 7 modules now depend on it and a future tweak to either lock helper could silently corrupt fenced state files (`cron.yaml`, `retry_state.json`) before any caller fails. Pinning the contract now — while the module is fresh and the call-site shape is fully captured by TB-217/218/220 commits — is cheaper than backfilling after the third incident.

## Scope

Add `ap2/tests/test_shared.py` with happy + error path coverage for each helper:

1. **`locked_inplace(path)`**: happy path holds an exclusive lock; reading/writing the same path under the lock works (since the helper opens the file itself, the caller must not truncate — pin a test that demonstrates the lock is held across the `with` block); parent-directory auto-creation works (pass a path whose parent doesn't exist).

2. **`locked_sidecar(path)`**: happy path locks `.lock` sidecar; pin the critical semantic difference vs `locked_inplace` — under the lock, the `with` body can rewrite/truncate `path` itself (write-to-temp + os.replace is the canonical pattern) without disturbing the lock; sidecar file lands at `path.with_suffix(path.suffix + ".lock")`; parent-directory auto-creation works.

3. **`short(v, limit)`**: returns `str(v)` unchanged when `len(str(v)) <= limit`; truncates with U+2026 marker when exceeding limit (specifically: `s[: limit - 1] + "…"` — pin the exact boundary so a future off-by-one doesn't silently truncate a char early/late); non-string inputs round-trip through `str()`; limit=0 / limit=1 edge cases produce expected output.

4. **`now()`**: returns string matching `YYYY-MM-DDTHH:MM:SSZ` pattern; the value is UTC (pin via `datetime.now(tz=utc).isoformat()` proximity check or by monkey-patching `_shared.dt.datetime` to a known fixed time).

5. **`read_pid(cfg)`**: returns int when `cfg.pid_file` contains a valid integer; returns None when the file doesn't exist (FileNotFoundError); returns None when the file body isn't parseable (ValueError); returns None when the file is unreadable (OSError on permissions). Use a tmp-path Config stub matching the call-site shape from `ap2/cli.py` + `ap2/web.py`.

No changes to `ap2/_shared.py` itself, no changes to existing callers, no changes to drift gates.

## Design

- Mirror TB-205 (`test_env_knobs.py`) and TB-210 (`test_tb210_env_knobs.py`) shapes: one focused test module, parametrized where natural, each test names the function under test and the specific contract being pinned (e.g. `test_locked_sidecar_permits_safe_rewrite_under_lock`, `test_short_truncates_with_ellipsis_at_limit_minus_one`).
- Reusability note: the module docstring at `ap2/_shared.py` L1-50 already names the threshold-three convention; tests-as-documentation reinforce that contract by example. A separate `architecture.md` paragraph documenting `_shared.py` is folded into this task's design rather than spun off as a 3rd proposal (delete-test on the doc paragraph alone is weak; tests carry the contract regardless).
- The `locked_sidecar` "safe rewrite under lock" test is the highest-leverage assertion in the file — it pins the exact semantic difference vs `locked_inplace` that motivated TB-217's two-named-function design. A bug there would silently invalidate the fd-bound lock for any future opener of cron.yaml / retry_state.json.
- No changes to TB-208 `test_coverage_drift.py` — the drift gate tracks public surfaces (MCP / env / event / CLI), not internal helpers; `_shared.py` doesn't fit any existing gate axis.

## Verification

- `test -f ap2/tests/test_shared.py` — file exists.
- `uv run pytest -q ap2/tests/test_shared.py` — exits 0; all tests in the new module pass.
- `uv run pytest -q ap2/tests/` — full suite green (exit 0, ≥1357 tests); no regression in existing modules.
- `grep -cE "^from ap2._shared import|^import ap2._shared" ap2/tests/test_shared.py` — at least 1 import line.
- `grep -cE "def test_" ap2/tests/test_shared.py` — at least 10 test functions across the 5 helpers (≥1 happy + ≥1 error per helper = 10 minimum; sidecar's safe-rewrite-under-lock counts toward sidecar coverage).
- `grep -nE "locked_sidecar|locked_inplace|read_pid|short|now" ap2/tests/test_shared.py` — every helper name appears at least once in the test file.
- `ap2/tests/test_shared.py` Prose: at least one test in the file pins the `locked_sidecar` vs `locked_inplace` semantic distinction — specifically that `locked_sidecar`'s `with` body can rewrite or truncate the protected path without invalidating the lock, and that `locked_inplace`'s fd points at the protected file itself. Judge confirms via Read.
- `ap2/tests/test_shared.py` Prose: at least one test pins `short()`'s truncation boundary as `s[: limit - 1] + "…"` (U+2026), not `s[: limit] + "…"` or `s[:limit-3] + "..."` — boundary regression is the most likely silent failure mode for this helper. Judge confirms via Read.
- `ap2/tests/test_shared.py` Prose: the `read_pid` tests cover at least three error branches (FileNotFoundError via missing file, ValueError via non-integer body, and either OSError or a stale/empty file returning None). Judge confirms via Read.

## Out of scope

- Migrating the `_shared.py` import to `ap2/shared.py` (drop the leading underscore) — the module is intentionally private; renaming is a separate convention decision.
- Adding `_shared.py` to TB-208 `test_coverage_drift.py`'s drift gates — internal helpers aren't a public-surface axis.
- Documenting `_shared.py` in `ap2/architecture.md` — folded into this task's design notes; standalone delete-test on the doc paragraph is weak.
- Refactoring callers — TB-217/TB-218/TB-220 already migrated 7 modules; no further call-site work here.
- Speculative additional helpers in `_shared.py` (e.g. promoting other 2-call-site helpers prematurely) — goal.md L74-77 threshold-three rule defers those until a third reader appears.
## Attempts

### 2026-05-14 — verification_failed
(no summary)
- **kind:** per_task
- **failed_criteria:** [fail] `ap2/tests/test_shared.py` Prose: at least one test in the file pins the `locked_sidecar` vs `locked_inplace` semantic d; [fail] `ap2/tests/test_shared.py` Prose: at least one test pins `short()`'s truncation boundary as `s[: limit - 1] + "…"` (U+20; [fail] `ap2/tests/test_shared.py` Prose: the `read_pid` tests cover at least three error branches (FileNotFoundError via missin
- **Debug dumps:** `prompt: .cc-autopilot/debug/20260514T214334Z-TB-222.prompt.md`, `stream: .cc-autopilot/debug/20260514T214334Z-TB-222.stream.jsonl`, `messages: .cc-autopilot/debug/20260514T214334Z-TB-222.messages.jsonl`

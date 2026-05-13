## Goal

Close four of the twelve CLI-verb coverage-debt names that TB-209's `ap2/tests/test_coverage_drift.py` docstring (L401-413) explicitly enumerates as a follow-up to TB-209's drift-gate landing. The four daemon-lifecycle verbs — `ap2 pause`, `ap2 resume`, `ap2 stop`, `ap2 unfreeze` — are registered in `ap2/cli.py`'s `build_parser()` (verified via `_collect_cli_verbs` and the live CLI's `--help`) but the only substring reference under `ap2/tests/` is the comment-block shim itself (`Grep ap2/tests/` returns one file: `test_coverage_drift.py`). The substring drift gate passes by gate-satisfaction shim, not by any real assertion of CLI behavior. Direct mirror of TB-205's approved pattern (4 SDK-cost env knobs → focused per-name contract tests) and TB-210's closure shape (4 env knobs + comment-block shim removal), rotated onto the CLI-verb axis. Goal anchor: **Current focus: code quality** (goal.md L38) — Testing coverage axis (goal.md L58-63: "every shipped CLI verb, MCP tool, control-agent path, and env-knob-flagged behavior has automated tests pinning the happy path AND at least one error path").

Why now: TB-209's docstring names this gap as a closed-set follow-up at exactly 12 entries that bifurcate cleanly along verb-family — 4 daemon-lifecycle (this TB) + 4 sandbox install-* + 4 sandbox audit/setup. The daemon-lifecycle verbs are the operator's primary control surface (pause/resume the daemon, stop it cleanly, thaw a frozen task), with the highest blast-radius if a refactor silently breaks them. Without real assertions, every future refactor of `cli.cmd_pause` / `cmd_resume` / `cmd_stop` / `cmd_unfreeze` or the daemon-control IPC they wrap can silently break the operator UX while the drift gate stays green via the shim. Replacing the shim with real tests closes the testing-axis gap that TB-209 explicitly tagged as "coverage debt, not exemptions" on the highest-priority CLI subset in one shot.

## Scope

- Add `ap2/tests/test_tb213_daemon_lifecycle_verbs.py` (or extend `ap2/tests/test_cli.py` — implementer's call; the substring drift gate doesn't care which file). For each of the four daemon-lifecycle verbs, add at least:
  - One happy-path test invoking the CLI handler with valid args (use `runpy` / `subprocess` against `ap2 <verb>` OR the in-process pattern existing `test_cli.py` tests use — invoke `cli.main(["<verb>", ...])` or `cli.cmd_<verb>(...)` directly with a parsed `argparse.Namespace`).
  - One error-path test (e.g. `ap2 pause` when the daemon isn't running; `ap2 unfreeze TB-N` when TB-N isn't Frozen; `ap2 stop` when no PID file exists; `ap2 resume` when the daemon isn't paused — each verb has a documented error branch).
- Remove the 4 matching rows from the discovered-at-landing comment block in `ap2/tests/test_coverage_drift.py` (currently L402-403 for pause/resume, L412 for stop, L413 for unfreeze — or whatever the exact line numbers are; identify by matching the `#   - ap2 <verb>` prefix). Leave the 8 sandbox-verb rows untouched — separate follow-up TBs close those.
- Do NOT change the CLI handlers' contracts; pure test addition + shim-row removal.

## Design

- Mirror `ap2/tests/test_cli.py`'s existing pattern for CLI-verb tests: invoke the handler in-process (e.g. via `cli.cmd_pause(cli.build_parser().parse_args(["pause"]))` or whatever signature the handler uses), capture stdout/stderr via `capsys` / `caplog`, assert on exit code + side-effects.
- For `ap2 pause`: handler at `ap2/cli.py` `cmd_pause` (or equivalent — `Grep "def cmd_pause" ap2/cli.py` to confirm). Happy path writes a pause sentinel or signals the daemon; error path covers "daemon not running" (no PID file or PID dead).
- For `ap2 resume`: dual of pause; happy path clears the pause sentinel; error path covers "daemon not paused".
- For `ap2 stop`: happy path sends SIGTERM to the daemon PID and waits for clean shutdown; error path covers "daemon not running" / "PID file missing".
- For `ap2 unfreeze`: handler routes through `operator_queue_append` per TB-131; happy path queues an `unfreeze` op for a Frozen TB-N; error path covers "TB-N not Frozen" / "TB-N doesn't exist".
- Test-function naming convention: `def test_cmd_<verb>_<aspect>(...)` — e.g. `test_cmd_pause_happy_path`, `test_cmd_pause_daemon_not_running`, `test_cmd_unfreeze_not_frozen`. This convention is what the auto-verify bullets grep for.
- The substring drift gate's CLI-verb walk reads `_collect_cli_verbs()` from `ap2/tests/_source_registry.py` (extracted in TB-209), so the test file's reference to the verb name (e.g. `"ap2 pause"` as a string literal OR `cmd_pause` as a symbol) suffices for the gate's `name in blob` check. Pin the string literal `"ap2 pause"` somewhere — docstring, test data, or commented justification — for the most robust reference.
- Removing the 4 comment-block rows is a 4-line deletion in `test_coverage_drift.py` (keep sandbox-verb lines). The drift gate continues to pass because the new test module references the verb names.

## Verification

- `uv run pytest -q ap2/tests/` — full suite passes after the change (exit 0).
- `uv run pytest -q ap2/tests/test_coverage_drift.py` — drift gate stays green; the 4 verbs still resolve (now via the new test module, not the comment block).
- `[ "$(grep -hE 'def test_cmd_(pause|resume|stop|unfreeze)' ap2/tests/test_tb213_daemon_lifecycle_verbs.py ap2/tests/test_cli.py 2>/dev/null | wc -l)" -ge 4 ]` — at least 4 test functions exist across candidate test files (one happy-path per verb minimum), regardless of which file the implementer chose. `2>/dev/null` swallows missing-file errors.
- `[ "$(grep -lE '(ap2 pause|ap2 resume|ap2 stop|ap2 unfreeze)' ap2/tests/*.py | wc -l)" -ge 2 ]` — at least 2 test files reference these 4 verbs (test_coverage_drift.py + the new/extended module).
- `[ "$(grep -cE '^#\s+- ap2 (pause|resume|stop|unfreeze)' ap2/tests/test_coverage_drift.py)" -le 0 ]` — the 4 daemon-lifecycle comment-block shim rows have been removed from `test_coverage_drift.py` (the `^#  - ap2 <verb>` line pattern matches the existing shim format).
- `grep -q "ap2 sandbox install-channel" ap2/tests/test_coverage_drift.py` — the 8 sandbox-verb rows are deliberately left in the shim (separate follow-up TBs close those); their presence confirms only the daemon-lifecycle subset was removed.
- Prose: the new tests assert on actual CLI handler behavior — not just synthetic argparse parsing — for each of the four verbs. Judge confirms by reading the new test bodies and checking that each test invokes a `cli.cmd_<verb>` symbol or `cli.main([...])` with the verb argument, rather than just constructing an `argparse.Namespace` without invoking the handler.

## Out of scope

- The 8 sandbox CLI-verb rows (`ap2 sandbox install-channel/-howto/-mm/-statusline`, `ap2 sandbox project-audit/-setup`, `ap2 sandbox user-audit/-setup` — L404-411) — two separate follow-up TBs (one per sandbox subset) close those.
- The 8 event-type coverage-debt rows (L392-399) — sibling TBs (one per emitter module) close those.
- Tightening the substring drift gate to AST-walk semantics — TB-209's docstring defers this.
- Cross-process / spawned-daemon integration testing — in-process handler invocation with stubbed IPC seams suffices; no need to spawn a live daemon.
- Adding new CLI verbs OR changing handler contracts — pure test additions + shim-row removal.

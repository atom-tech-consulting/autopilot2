## Goal

Close four of the twelve CLI-verb coverage-debt names that TB-209's `ap2/tests/test_coverage_drift.py` docstring (L401-413) explicitly enumerates as a follow-up to TB-209's drift-gate landing. The four sandbox install-* verbs — `ap2 sandbox install-channel`, `ap2 sandbox install-howto`, `ap2 sandbox install-mm`, `ap2 sandbox install-statusline` — are registered in `ap2/cli.py`'s `build_parser()` sandbox subcommand group (verified via `_collect_cli_verbs` in `ap2/tests/_source_registry.py` extracted by TB-209) but the only substring reference under `ap2/tests/` is the comment-block shim itself (`Grep ap2/tests/` for any of the four returns one file: `test_coverage_drift.py`). The substring drift gate passes by gate-satisfaction shim, not by any real assertion of CLI behavior. Direct mirror of TB-205's approved pattern (4 SDK-cost env knobs → focused per-name contract tests) and TB-210/TB-213's closure shape (4 names + comment-block shim removal), rotated onto the sandbox install-* subset of the CLI-verb axis. Goal anchor: **Current focus: code quality** (goal.md L38) — Testing coverage axis (goal.md L58-63: "every shipped CLI verb, MCP tool, control-agent path, and env-knob-flagged behavior has automated tests pinning the happy path AND at least one error path").

Why now: TB-209's docstring names this gap as a closed-set follow-up at exactly 12 entries that bifurcate cleanly along verb-family — 4 daemon-lifecycle (TB-213, in flight) + 4 sandbox install-* (this TB) + 4 sandbox audit/setup (sibling TB). The install-* verbs are the operator's first-touch surface on a fresh project (statusline + howto + MM creds + MM channel — each is the only path to wire one of those subsystems), with high blast-radius if a refactor silently breaks them. Without real assertions, every future refactor of `cli.cmd_sandbox_install_*` handlers or the underlying sandbox helpers they wrap can silently break operator onboarding while the drift gate stays green via the shim. Replacing the shim with real tests closes the sandbox install-* subset of the testing-axis gap that TB-209 explicitly tagged as "coverage debt, not exemptions" in one shot.

## Scope

- Add `ap2/tests/test_tb214_sandbox_install_verbs.py` (or extend `ap2/tests/test_sandbox.py` / `test_cli.py` — implementer's call; the substring drift gate doesn't care which file). For each of the four sandbox install-* verbs, add at least:
  - One happy-path test invoking the CLI handler with valid args (use the in-process pattern existing `test_sandbox.py` / `test_cli.py` tests use — invoke `cli.main(["sandbox", "<verb>", ...])` or call `cli.cmd_sandbox_<verb>(...)` / the sandbox module function it dispatches to directly with a parsed `argparse.Namespace`; stub filesystem / subprocess seams as those tests already do).
  - One error-path test per verb (each install-* handler has documented error branches — e.g. missing required argument, target path already exists / not writable, missing parent prerequisite like absent project dir for install-howto, missing creds env for install-mm). Implementer picks the branch with the cleanest stubbing surface per verb.
- Remove the 4 matching rows from the discovered-at-landing comment block in `ap2/tests/test_coverage_drift.py` (currently L404-407 — the four `#   - ap2 sandbox install-*` lines). Leave the 4 daemon-lifecycle rows (already being removed by TB-213 in flight) and the 4 sandbox audit/setup rows (separate sibling TB) untouched.
- Do NOT change the CLI handlers' contracts; pure test addition + shim-row removal.

## Design

- Mirror `ap2/tests/test_sandbox.py`'s existing pattern for sandbox handler tests: invoke the handler in-process (e.g. via `cli.main(["sandbox", "install-channel", ...])` with the relevant args, OR the underlying `sandbox.install_channel(...)` function with parsed kwargs), capture stdout/stderr via `capsys` / `caplog`, assert on exit code + side-effects (file written, mock subprocess called with expected argv, etc.).
- For `ap2 sandbox install-channel`: handler in `ap2/cli.py` dispatches to the sandbox module (`Grep "install-channel\|install_channel" ap2/cli.py ap2/sandbox.py` to locate the exact symbol). Happy path: handler invokes the MM-channel-create flow with expected args; error path: missing creds / channel name conflict / MM API error.
- For `ap2 sandbox install-howto`: writes a sandbox-user copy of `ap2/howto.md` (or the equivalent install target — implementer reads the handler). Happy path asserts the write; error path covers missing source / target unwritable / target already exists without --force.
- For `ap2 sandbox install-mm`: installs Mattermost credentials into the sandbox user's `~/.zshenv`-equivalent. Happy path stubs the write target; error path covers missing creds in env / write failure.
- For `ap2 sandbox install-statusline`: installs the statusline into the sandbox user's Claude Code config. Happy path stubs the write target; error path covers missing source template / target unwritable.
- Test-function naming convention: `def test_cmd_sandbox_<verb_underscored>_<aspect>(...)` — e.g. `test_cmd_sandbox_install_channel_happy_path`, `test_cmd_sandbox_install_mm_missing_creds`. This convention is what the auto-verify bullets grep for.
- The substring drift gate's CLI-verb walk reads `_collect_cli_verbs()` from `ap2/tests/_source_registry.py` (extracted in TB-209), so the test file's reference to the verb name (e.g. `"ap2 sandbox install-channel"` as a string literal OR `cmd_sandbox_install_channel` as a symbol) suffices for the gate's `name in blob` check. Pin the four full verb strings (`"ap2 sandbox install-channel"`, etc.) somewhere — docstring, test data, or commented justification — for the most robust reference.
- Removing the 4 comment-block rows (L404-407) is a 4-line deletion in `test_coverage_drift.py`. The drift gate continues to pass because the new test module references the verb names.

## Verification

- `uv run pytest -q ap2/tests/` — full suite passes after the change (exit 0).
- `uv run pytest -q ap2/tests/test_coverage_drift.py` — drift gate stays green; the 4 install-* verbs still resolve (now via the new test module, not the comment block).
- `[ "$(grep -hE 'def test_cmd_sandbox_install_(channel|howto|mm|statusline)' ap2/tests/test_tb214_sandbox_install_verbs.py ap2/tests/test_sandbox.py ap2/tests/test_cli.py 2>/dev/null | wc -l)" -ge 4 ]` — at least 4 test functions exist across candidate test files (one happy-path per verb minimum), regardless of which file the implementer chose. `2>/dev/null` swallows missing-file errors.
- `[ "$(grep -lE 'ap2 sandbox install-(channel|howto|mm|statusline)' ap2/tests/*.py | wc -l)" -ge 2 ]` — at least 2 test files reference these 4 verb strings (test_coverage_drift.py + the new/extended module).
- `[ "$(grep -cE '^#\s+- ap2 sandbox install-(channel|howto|mm|statusline)' ap2/tests/test_coverage_drift.py)" -le 0 ]` — the 4 sandbox install-* comment-block shim rows have been removed from `test_coverage_drift.py` (the `^#   - ap2 sandbox install-<verb>` line pattern matches the existing shim format).
- `grep -q "ap2 sandbox project-audit" ap2/tests/test_coverage_drift.py` — the 4 sandbox audit/setup rows are deliberately left in the shim (separate follow-up TB closes those); their presence confirms only the install-* subset was removed.
- Prose: the new tests assert on actual CLI handler behavior — not just synthetic argparse parsing — for each of the four sandbox install-* verbs. Judge confirms by reading the new test bodies and checking that each test invokes a `cli.cmd_sandbox_<verb>` or `sandbox.<install_fn>` symbol or `cli.main(["sandbox", "<verb>", ...])` with the verb argument, rather than just constructing an `argparse.Namespace` without invoking the handler.

## Out of scope

- The 4 daemon-lifecycle CLI-verb rows (L402-403 pause/resume, L412 stop, L413 unfreeze) — TB-213 in flight closes those.
- The 4 sandbox audit/setup CLI-verb rows (L408-411 project-audit/-setup, user-audit/-setup) — separate sibling TB closes those.
- The 8 event-type coverage-debt rows (L392-399) — TB-211/212 in flight close those.
- Tightening the substring drift gate to AST-walk semantics — TB-209's docstring defers this.
- Cross-process / spawned-daemon integration testing for the install handlers — in-process invocation with stubbed filesystem/subprocess seams suffices; no need to spawn a live sandbox.
- Adding new CLI verbs OR changing handler contracts — pure test additions + shim-row removal.

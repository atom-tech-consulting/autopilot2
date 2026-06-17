# Scrub residual sandbox-path leak in ap2/tests + regression-gate shipped source against sandbox-local absolute paths

Tags: #autopilot #distribution #packaging #identity-scrub #regression-pin #tests

## Goal

Close a residual gap in the **Current focus: cut a public source-available
distribution** axis-1 identity scrub. goal.md's first Progress signal requires "a
clean checkout installs and runs the test suite green with no sandbox-specific
paths or identity baked into source", and the axis-1 delete-test fails if "a fresh
install from a clean checkout leaks the sandbox's local paths/identity". One leak
survives: `ap2/tests/test_json_extract_util.py` hard-codes the absolute path
`/Users/claude-agent/repos/post-train/.cc-autopilot/debug/...` as `_TB89_CAPTURED_RESPONSE`.
`ap2.tests` is a declared package (pyproject `[tool.setuptools] packages`), so this
path ships in the sdist/wheel. TB-409's scrub swept `ap2/*.py` (a non-recursive
glob) and never reached the test tree. This task scrubs that one path and adds a
recursive regression gate over the whole shipped distribution surface so the leak
cannot silently reappear.

Why now: TB-409 (landed today) claimed the identity scrub was done, but its
non-recursive `ap2/*.py` sweep left a sandbox-local absolute path baked into a
shipped test module — Progress signal 1 is violated right now, and nothing prevents
the next copy-pasted debug path from re-introducing the same leak.

## Scope

- Fix `ap2/tests/test_json_extract_util.py`: replace the hard-coded
  `/Users/claude-agent/repos/post-train/.cc-autopilot/debug/...` `_TB89_CAPTURED_RESPONSE`
  path with a sandbox-neutral source — either an env-overridable lookup
  (`os.environ.get("AP2_TB89_CAPTURED_RESPONSE")`) or drop the file-dependent test
  branch entirely. The branch is already skip-guarded when the file is absent and
  the synthetic brace-shadowing cases above it cover the same shape, so coverage is
  preserved either way.
- Add a regression gate `ap2/tests/test_no_sandbox_path_leak.py` that scans the
  shipped distribution surface — `ap2/` recursively (including `ap2/tests/`),
  `skills/`, and the top-level docs (`README.md`, `CHANGELOG.md`,
  `ap2/architecture.md`) — and FAILS if any file contains an absolute path under the
  sandbox operator's local checkout root (the `/Users/<sandbox-user>/repos/...`
  shape TB-409 scrubbed from `ap2/json_extract.py`).

## Design

- The gate pins ONE invariant: no shipped file carries an absolute path under the
  sandbox operator's local repo root. It is NOT an enumerated-case linter — it does
  not try to classify many path shapes.
- Scope the forbidden pattern to the absolute-PATH leak only (e.g. the literal
  `/Users/claude-agent/repos` prefix). Do NOT forbid the bare project name
  `post-train`: TB-409 deliberately kept narrative `post-train` mentions (cost /
  bug-repro provenance comments in `ap2/json_extract.py`, `ap2/cli_review.py`,
  `ap2/operator_queue.py`) as sandbox-neutral references — the gate must not fight
  that decision.
- Allowlist the legitimate generic/parameterized paths the sweep surfaced so they
  do not false-fail: the `/Users/{user}` template in `ap2/sandbox.py`, the
  `/Users/fakeuser/...` fixture in `ap2/tests/test_doctor.py`, and the generic
  `/tmp/proj` / `/home/user/...` example paths — none are the sandbox operator's
  real checkout root.
- Self-match avoidance: the gate file itself is inside the scanned tree, so it must
  construct the forbidden pattern WITHOUT embedding the contiguous leak literal
  (e.g. build it from parts, or read the sandbox-user token at runtime), otherwise
  the test matches its own source. Keep the construction obvious and commented.

## Verification

- `! grep -rn "/Users/claude-agent/repos" ap2/ skills/ README.md CHANGELOG.md ap2/architecture.md` — no shipped source or doc carries the sandbox operator's local checkout-root absolute path (passes iff absent).
- `uv run --extra dev pytest -q ap2/tests/test_no_sandbox_path_leak.py` — the new regression gate passes.
- `uv run --extra dev pytest -q ap2/tests/test_json_extract_util.py` — the scrubbed json-extract test module stays green after the path fix.
- `ap2/tests/test_no_sandbox_path_leak.py` Prose: the gate scans the shipped distribution surface (`ap2/` recursively incl. `ap2/tests/`, `skills/`, the top-level docs) for absolute paths under the sandbox operator's local repo root, allowlists the parameterized/generic example paths named in Design, and constructs the forbidden pattern non-literally so the gate file is not a self-match; judge confirms via Read.
- `ap2/tests/test_json_extract_util.py` Prose: the hard-coded `/Users/claude-agent/repos/post-train/...` captured-response path is gone, replaced by an env-overridable or repo-relative lookup (or the file-dependent branch dropped), with the existing skip-guard and synthetic brace-shadowing coverage preserved; judge confirms via Read.

## Out of scope

- The narrative `post-train` project-name comments TB-409 chose to keep (not paths,
  not credentials, sandbox-neutral references).
- The documented overridable sandbox-user defaults (`DEFAULT_USER = "claude-agent"`
  in `ap2/sandbox.py`, the `--user` CLI default) — these are overridable defaults,
  not baked-in leaks, per goal.md's explicit "vs. a documented overridable default
  like AP2_SANDBOX_USER" carve-out.
- The LICENSE / pyproject license-coherence regression gate (sibling proposal this
  cycle).
- Any `python -m build --sdist` build smoke (deferred — non-hermetic).
## Attempts

### 2026-06-17 — blocked
Scrub + gate are done and green (committed 94394a8): test_json_extract_util.py's baked /Users/<sandbox-user>/repos/post-train/... captured-response path is now an env-overridable AP2_TB89_CAPTURED_RESPONSE lookup (skip-guard + synthetic coverage preserved), and the new recursive gate test_no_sandbox_path_leak.py passes (15 passed, 1 skipped). BLOCKER: verification bullet 0 `! grep -rn "/Users/claude-agent/repos" ap2/ ...` is unsatisfiable as written — `grep -rn` matches 614 binary __pycache__/*.pyc files whose co_filename embeds the absolute build path (written by the running daemon AND by the project-wide verify suite that runs BEFORE the per-task bullets), so it exits 0 and `! grep` fails regardless of any source fix; the identical command with `-I` (skip binary) finds 0 text matches and passes. No durable in-repo fix exists (relocating pyc out of the tree needs PYTHONDONTWRITEBYTECODE/PYTHONPYCACHEPREFIX in the daemon's own env, which is operator-owned), so the grep bullet must add `-I`. The new gate test already encodes the correct invariant durably (skips binary, scans only shipped text source).
BriefingFix: grep_recursive_needs_binary_skip at .cc-autopilot/tasks/scrub-residual-sandbox-path-leak-in-ap2.md:64: grep -rn -> grep -rnI
- **commit:** 94394a8
- **Debug dumps:** `prompt: .cc-autopilot/debug/20260617T214449Z-TB-415.prompt.md`, `stream: .cc-autopilot/debug/20260617T214449Z-TB-415.stream.jsonl`, `messages: .cc-autopilot/debug/20260617T214449Z-TB-415.messages.jsonl`
### 2026-06-17 — blocked
Scope is fully done + green in 94394a8 (re-verified this run): test_json_extract_util.py's baked /Users/<sandbox-user>/repos/post-train/... captured-response path is now an env-overridable AP2_TB89_CAPTURED_RESPONSE lookup (skip-guard + synthetic brace-shadowing coverage preserved, 13 passed/1 skipped), and the new recursive gate test_no_sandbox_path_leak.py passes (2 passed); git grep over TRACKED source/docs for the leak = 0 matches, so shipped source is genuinely clean. BLOCKER: verification bullet 0 `! grep -rn "/Users/claude-agent/repos" ...` is unsatisfiable as written — plain grep -rn matches 614 gitignored binary __pycache__/*.pyc whose co_filename embeds the repo's absolute build path (the running daemon itself imports ap2 so ap2/__pycache__ always exists; the project-wide verify suite regenerates more before the bullets run), so it exits 0 and `! grep` fails regardless of any source fix. The identical command with -I (skip binary) finds 0 text matches and passes; -I is exactly the binary-skip the gate test already does internally, so bullet 0 is redundant with the durable bullet-1 gate. No in-repo durable fix exists (relocating pyc out of the tree needs PYTHONPYCACHEPREFIX/PYTHONDONTWRITEBYTECODE in the daemon's operator-owned env), and auto-unfreeze is inert here (AP2_AUTO_UNFREEZE_FIX_SHAPES unset), so this needs a manual operator briefing edit. BriefingFix: grep_recursive_needs_binary_skip at .cc-autopilot/tasks/scrub-residual-sandbox-path-leak-in-ap2.md:64: grep -rn -> grep -rnI
- **commit:** 94394a8
- **Debug dumps:** `prompt: .cc-autopilot/debug/20260617T220028Z-TB-415.prompt.md`, `stream: .cc-autopilot/debug/20260617T220028Z-TB-415.stream.jsonl`, `messages: .cc-autopilot/debug/20260617T220028Z-TB-415.messages.jsonl`

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
### 2026-06-17 — blocked
Scope is fully implemented + green in 94394a8 (reachable from HEAD, re-verified this 3rd run): test_json_extract_util.py's baked /Users/<sandbox-user>/repos/post-train/... captured-response path is now an env-overridable AP2_TB89_CAPTURED_RESPONSE lookup (skip-guard + synthetic brace-shadowing coverage preserved) and the recursive gate test_no_sandbox_path_leak.py passes; bullets 1+2 = 15 passed/1 skipped, bullets 3+4 (prose) match the committed files. git grep + grep -rnI over tracked source/docs = 0 leak matches, so shipped source is genuinely clean. BLOCKER (unchanged, environmental): verification bullet 0 `! grep -rn "/Users/claude-agent/repos" ...` at line 64 is unsatisfiable as written — plain grep -rn matches gitignored binary __pycache__/*.pyc whose co_filename embeds the sandbox checkout's absolute build path (the daemon imports ap2 so ap2/__pycache__ always exists; the project-wide verify suite regenerates more before the bullets run), so it exits 0 and `! grep` fails regardless of any SOURCE fix. The verifier runs bullets in the live working tree (verify.py _run_shell_bullet cwd=project_root), not a clean checkout, so the .pyc are always present. No durable in-repo fix exists (relocating .pyc needs PYTHONDONTWRITEBYTECODE/PYTHONPYCACHEPREFIX in the daemon's operator-owned env). Adding -I (binary-skip) is the fix; -I is exactly what the durable bullet-1 gate already does internally, making bullet 0 redundant. Auto-unfreeze cannot self-heal this: the task is not yet Frozen, the fix_shapes allowlist is unset, AND this shape is not one of the 4 published bootstrap shapes — so the operator must either manually add -I to bullet 0 OR add grep_recursive_needs_binary_skip to AP2_AUTO_UNFREEZE_FIX_SHAPES once it freezes. I did not edit the briefing (fenced) or queue an operator op to patch my own verification (out of role). BriefingFix: grep_recursive_needs_binary_skip at .cc-autopilot/tasks/scrub-residual-sandbox-path-leak-in-ap2.md:64: grep -rn -> grep -rnI
- **commit:** 94394a8
- **Debug dumps:** `prompt: .cc-autopilot/debug/20260617T220617Z-TB-415.prompt.md`, `stream: .cc-autopilot/debug/20260617T220617Z-TB-415.stream.jsonl`, `messages: .cc-autopilot/debug/20260617T220617Z-TB-415.messages.jsonl`
### 2026-06-18 — blocked
CODE IS DONE & CORRECT in 94394a8 (reachable from HEAD) — zero further code work needed. test_json_extract_util.py's hard-coded /Users/claude-agent/repos/post-train/... path is now an env-overridable AP2_TB89_CAPTURED_RESPONSE lookup (skip-guard + synthetic coverage preserved: 13 passed/1 skipped), and the recursive gate test_no_sandbox_path_leak.py passes (2 passed). `grep -rnI` over the whole shipped surface = 0 text matches, so source is genuinely clean. SOLE BLOCKER is a verification-mechanism bug I am hard-fenced from fixing: bullet 0 at line 64 runs `grep -rn` (no -I), which matches 618 binary __pycache__/*.pyc files whose co_filename embeds the in-tree editable install's absolute build path (the daemon imports ap2 so ap2/__pycache__ always exists), so grep exits 0 and `! grep` fails regardless of any source fix. No durable in-repo fix exists (relocating pyc needs PYTHONPYCACHEPREFIX/PYTHONDONTWRITEBYTECODE in the daemon's operator-owned env; disabling bytecode in ap2/__init__.py would be an invasive library-behavior change to satisfy a test grep — rejected). OPERATOR ACTION REQUIRED — auto-unfreeze CANNOT self-heal this right now (AP2_AUTO_UNFREEZE_FIX_SHAPES is empty AND it only sweeps Frozen tasks; this task was just manually unfrozen at 03:03Z without editing bullet 0). Pick ONE: (a) simplest — edit line 64 `grep -rn` -> `grep -rnI`, verify goes green immediately; (b) delete bullet 0 entirely — bullet 1's gate test is the durable, binary-safe encoding of the same invariant; (c) set AP2_AUTO_UNFREEZE_FIX_SHAPES=grep_recursive_needs_binary_skip and let it re-freeze, then the BriefingFix line below auto-patches+unfreezes. BriefingFix: grep_recursive_needs_binary_skip at .cc-autopilot/tasks/scrub-residual-sandbox-path-leak-in-ap2.md:64: grep -rn -> grep -rnI
- **commit:** 94394a8
- **Debug dumps:** `prompt: .cc-autopilot/debug/20260618T031444Z-TB-415.prompt.md`, `stream: .cc-autopilot/debug/20260618T031444Z-TB-415.stream.jsonl`, `messages: .cc-autopilot/debug/20260618T031444Z-TB-415.messages.jsonl`
### 2026-06-18 — blocked
CODE COMPLETE & CORRECT in 94394a8 (ancestor of HEAD) — no further agent code work is possible or warranted. Re-verified this run: test_json_extract_util.py's baked /Users/claude-agent/repos/post-train/... captured-response path is now an env-overridable AP2_TB89_CAPTURED_RESPONSE lookup (13 passed/1 skipped; skip-guard + synthetic brace-shadowing coverage preserved), and the recursive gate test_no_sandbox_path_leak.py passes (2 passed) — it is the durable, binary-safe encoding of the EXACT invariant bullet 0 checks. grep -rnI over the whole shipped surface = 0 text leaks; source is genuinely clean. SOLE BLOCKER is operator-only and unchanged from the 4 prior runs: verification bullet 0 at line 64 runs `! grep -rn` (no -I); the verifier executes it in the LIVE working tree (confirmed verify.py:443, cwd=project_root), where plain grep -rn matches 618 binary __pycache__/*.pyc whose co_filename embeds THIS checkout's own absolute path /Users/claude-agent/repos/autopilot2/..., so grep exits 0 and `! grep` fails regardless of any source fix. No durable in-repo fix exists: the daemon imports ap2 so ap2/__pycache__ is always present, and relocating/suppressing .pyc needs PYTHONDONTWRITEBYTECODE/PYTHONPYCACHEPREFIX in the daemon's operator-owned env. Auto-unfreeze CANNOT self-heal this: AP2_AUTO_UNFREEZE_FIX_SHAPES is empty (feature off) AND grep_recursive_needs_binary_skip is NOT one of the four bootstrap shapes (grep_missing_r_on_dir/literal_backtick_in_shell_bullet/bare_python_to_uv_run/bare_path_to_test_f), so the BriefingFix below is doubly inert until an operator both enables the knob and allowlists the shape. MANUAL OPERATOR ACTION REQUIRED — pick ONE: (a) simplest — edit line 64 `grep -rn` -> `grep -rnI`, verify goes green immediately; (b) delete bullet 0 entirely — bullet 1's gate test is the durable, binary-safe encoding of the identical invariant, making bullet 0 strictly redundant AND broken; (c) set AP2_AUTO_UNFREEZE_FIX_SHAPES=grep_recursive_needs_binary_skip so the BriefingFix auto-patches+unfreezes on next freeze. I did not edit the briefing (fenced) or queue an operator op to patch my own verification (out of role). BriefingFix: grep_recursive_needs_binary_skip at .cc-autopilot/tasks/scrub-residual-sandbox-path-leak-in-ap2.md:64: grep -rn "/Users/claude-agent/repos" -> grep -rnI "/Users/claude-agent/repos"
- **commit:** 94394a8
- **Debug dumps:** `prompt: .cc-autopilot/debug/20260618T032419Z-TB-415.prompt.md`, `stream: .cc-autopilot/debug/20260618T032419Z-TB-415.stream.jsonl`, `messages: .cc-autopilot/debug/20260618T032419Z-TB-415.messages.jsonl`
### 2026-06-18 — blocked
ONE OPERATOR ACTION UNBLOCKS THIS (6th run, code unchanged & correct): edit briefing line 64 `grep -rn` -> `grep -rnI`. Manual `ap2 unfreeze` WITHOUT that edit just re-runs the same broken bullet -> re-blocks -> re-freezes (the loop you've seen). Code in 94394a8 (ancestor of HEAD) fully satisfies scope and 4/5 bullets pass: test_json_extract_util.py's /Users/claude-agent/repos/post-train/... path is now an env-overridable AP2_TB89_CAPTURED_RESPONSE lookup (13 passed/1 skipped; skip-guard+synthetic coverage preserved); test_no_sandbox_path_leak.py gate passes (2 passed); bullets 3+4 prose match. SOLE FAIL = bullet 0 `! grep -rn "/Users/claude-agent/repos" ...`, empirically exits 0 on 618 binary __pycache__/*.pyc whose co_filename embeds this checkout's absolute path (grep -rnI and git grep over tracked source = 0 matches -> source is genuinely clean). No durable in-repo fix: daemon+crons import ap2 so __pycache__ is always present; a conftest dont_write_bytecode+rmtree hack is racy (crons regenerate pyc) and invasive, rejected. Auto-unfreeze is inert (AP2_AUTO_UNFREEZE_FIX_SHAPES empty AND grep_recursive_needs_binary_skip not a bootstrap shape). Options: (a) add -I to bullet 0 [1 char, goes green immediately]; (b) delete bullet 0 — bullet 1's gate is the durable binary-safe encoding of the identical invariant; (c) set AP2_AUTO_UNFREEZE_FIX_SHAPES=grep_recursive_needs_binary_skip so the BriefingFix below auto-applies on next freeze. BriefingFix: grep_recursive_needs_binary_skip at .cc-autopilot/tasks/scrub-residual-sandbox-path-leak-in-ap2.md:64: grep -rn "/Users/claude-agent/repos" -> grep -rnI "/Users/claude-agent/repos"
- **commit:** 94394a8
- **Debug dumps:** `prompt: .cc-autopilot/debug/20260618T033121Z-TB-415.prompt.md`, `stream: .cc-autopilot/debug/20260618T033121Z-TB-415.stream.jsonl`, `messages: .cc-autopilot/debug/20260618T033121Z-TB-415.messages.jsonl`

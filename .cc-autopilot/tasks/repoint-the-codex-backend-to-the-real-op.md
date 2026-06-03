# Repoint the codex backend to the real OpenAI `openai-codex` SDK and reimplement CodexAdapter against its actual API

Tags: #autopilot #agent-adapter #codex #bug #sdk #axis-4 #axis-7

## Goal

This advances **Current focus: codex support through an agent adaptor
layer** — turning codex from "code-complete against a phantom SDK" into a
backend that can actually drive an agent kind (the focus's delete-test).

The codex adapter was built against the **wrong** Python package and a
**fabricated API**. Verified 2026-06-02 against PyPI and the installed
SDKs:

- The PyPI distribution `codex-sdk` / `codex_sdk` (pip treats `-`/`_` as
  the same dist) is **Cleanlab's** "Internal SDK used within
  cleanlab-codex" — a RAG/eval tool, import name `codex`. It is unrelated
  to OpenAI Codex. TB-371 declared this as the `[codex]` extra, and
  `ap2/adapters/codex.py` does `import codex_sdk`.
- OpenAI's real Codex SDK (https://developers.openai.com/codex/sdk) is
  **`openai-codex`** (import `openai_codex`), author OpenAI, repo
  github.com/openai/codex; it bundles `openai-codex-cli-bin`. It is
  already installed in this venv (`openai-codex==0.1.0b2`).
- Beyond the name, `CodexAdapter` calls `codex.CodexOptions(**opt_kwargs)`
  (`ap2/adapters/codex.py:387`) and `async for env in codex.run_streamed(...)`
  (`ap2/adapters/codex.py:393`). **Neither symbol exists** in
  `openai_codex` (nor in Cleanlab's `codex`). The real surface is
  `Codex()` → `thread = codex.thread_start(model=..., sandbox=Sandbox.<mode>)`
  → `result = thread.run(prompt)` → `result.final_response`; `AsyncCodex`
  for async; `Codex` methods include
  `thread_start/thread_resume/thread_fork/thread_list/run/login_chatgpt/
  login_chatgpt_device_code/login_api_key/account/models/metadata/close`.

Because the codex real-SDK smoke does `importorskip("codex_sdk")`, it has
always skipped — masking the fact that the adapter targets an API that was
never real.

Why now: TB-371 made codex "installable" in name only (it declares an
unrelated package). The focus's delete-test ("a second backend actually
drives an agent kind") cannot be met while the adapter imports a
nonexistent module and calls nonexistent methods — this is the single
blocker between "codex scaffolding exists" and "codex is a usable
backend." OpenAI ships an official Python SDK whose `login_chatgpt` path
matches the ChatGPT-login `~/.codex/auth.json` the daemon-start gate
(TB-370) already accepts, so the auth story already lines up; only the
package + adapter implementation are wrong. Operator-directed 2026-06-02.

## Scope

- **Fix the `[codex]` extra** (`pyproject.toml`): replace
  `codex = ["codex-sdk"]` with `codex = ["openai-codex"]`, and update the
  surrounding comment (it currently says the dist name matches
  `uv pip install codex-sdk`).
- **Reimplement the adapter handle + dispatch** (`ap2/adapters/codex.py`):
  - `load_codex_sdk()` must import the real module `openai_codex` (not
    `codex_sdk`). Keeping the helper name is fine; the injected-handle test
    seam (`sys.modules` lookup) must be preserved, keyed on `openai_codex`.
  - Replace the nonexistent `codex.CodexOptions(...)` / `codex.run_streamed(...)`
    calls with the real `openai_codex` API: construct an `(Async)Codex`
    client, start a thread (`thread_start`, mapping ap2's `AgentOptions` —
    model, sandbox/approval, cwd — onto the SDK's real parameters), run the
    prompt, and translate the SDK's turn/streaming events into ap2's
    `AgentEvent` stream and a normalized `AgentResult` (reuse
    `usage_from_summary` / the AgentResult shape from `ap2/adapters/base`).
    Introspect the installed `openai_codex` package and follow the official
    docs for the exact streaming/turn surface; do NOT invent symbols —
    every attribute used must exist on the installed module.
- **Update the daemon-start codex handle gate**
  (`ap2/daemon._require_codex_handle_if_referenced`, ~`daemon.py:2885-2906`):
  probe `openai_codex` via `load_codex_sdk()` and fix the diagnostic
  message (it currently says `uv pip install codex-sdk`) to name the real
  install (`uv pip install openai-codex` / the `[codex]` extra).
- **Update the smoke** (`ap2/tests/smoke/test_codex_real_sdk.py`): the
  `importorskip` and its comment must target `openai_codex`, and the test
  body must exercise the real `Codex`/`thread_start`/`run` surface so that,
  when run with credentials, it validates the adapter end-to-end.
- **Update stale name references in comments/docstrings**: the
  `codex_sdk` mentions in `ap2/cli_daemon.py` (auth-gate comments ~L88/113/139)
  and `ap2/daemon.py` (~L1936/2875-2877) should say `openai_codex`.
- **Parity/adapter tests** (`ap2/tests/`): update any test that injects a
  fake `codex_sdk` to inject `openai_codex` instead, and extend the
  adapter-contract parity coverage so the CodexAdapter is exercised against
  a hermetic fake of the **real** API shape (a stub exposing
  `Codex`/`thread_start`/`run`/turn-events), not the invented one. These
  hermetic tests must pass without network or credentials.
- **No secret handling**: do not log token contents; the gate stays a
  presence-only pre-flight (TB-370).

## Design

- **Use the official Python SDK, not CLI-scraping.** OpenAI ships
  `openai-codex` (which bundles the CLI binary); driving the typed
  `Codex`/`thread_start`/`run` API is cleaner and more stable than parsing
  `codex exec --json`, and it exposes `login_chatgpt` so the existing
  ChatGPT-login session works directly.
- **One handle loader, one truth.** `load_codex_sdk()` remains the single
  relocation point both the adapter and the daemon-start gate import
  through (mirrors `claude_code.load_claude_sdk`), so they agree on "codex
  is available." Only its import target changes (`openai_codex`).
- **Ground every symbol in the installed package.** The prior
  implementation failed precisely because it used symbols that don't
  exist. The reimplementation must be written against the actually-installed
  `openai_codex` (introspect it / read its docs); the hermetic parity stub
  must mirror that real shape so a future regression to invented symbols
  fails the suite.
- **Auth unchanged.** TB-370's gate (OPENAI_API_KEY OR chatgpt
  `auth.json`) already matches the real SDK's two login modes; this task
  does not touch the auth gate's logic, only the stale `codex_sdk`
  comments around it.

## Verification

- `uv run --extra dev pytest -q ap2/tests/ --ignore=ap2/tests/smoke` — full suite passes, including the updated adapter/parity tests built against the real `openai_codex` API shape.
- `grep -q "openai-codex" pyproject.toml` — the `[codex]` extra declares the real OpenAI package.
- `! grep -qE "codex[_-]sdk" pyproject.toml` — the wrong Cleanlab package name is gone from pyproject.
- `grep -q "openai_codex" ap2/adapters/codex.py` — the adapter imports the real module.
- `! grep -qE "CodexOptions|run_streamed" ap2/adapters/codex.py` — the fabricated API calls are gone.
- `grep -q "openai_codex" ap2/tests/smoke/test_codex_real_sdk.py` — the smoke gates on the real module.
- `ap2/adapters/codex.py` Prose: `load_codex_sdk` imports the real `openai_codex` module (preserving the `sys.modules` injected-handle test seam), and `CodexAdapter`'s dispatch drives the real SDK surface — constructing a `Codex`/`AsyncCodex` client and using `thread_start` + a run/turn call to produce ap2's `AgentEvent` stream and a normalized `AgentResult` — with no reference to the nonexistent `CodexOptions` or `run_streamed`. Judge confirms via Read.
- `ap2/daemon.py` Prose: the daemon-start codex handle gate probes `openai_codex` (via `load_codex_sdk`) and its failure diagnostic names the real install (`openai-codex` / the `[codex]` extra), not `codex-sdk`. Judge confirms via Read.

## Out of scope

- **Running the live credentialed smoke** (`AP2_REAL_SDK=1 … test_codex_real_sdk.py`) — operator-owned, cannot be verified unattended (would be a forbidden `Manual:` bullet, the TB-122 trap). This task makes the code correct + hermetically tested; the operator runs the live smoke against the ChatGPT session afterward.
- The daemon-start auth gate's logic (TB-370) — unchanged; only its stale `codex_sdk` comments are corrected.
- Per-message backend routing or a third backend — respects goal.md's backend constraints.
- Changing the AgentAdapter contract or the Claude adapter — only the codex side is wrong.

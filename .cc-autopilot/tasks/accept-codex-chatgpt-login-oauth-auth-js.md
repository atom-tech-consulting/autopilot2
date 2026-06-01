# Accept codex ChatGPT-login OAuth (auth.json), not only OPENAI_API_KEY, in the backend-aware auth gate

Tags: #autopilot #agent-adapter #codex #auth #oauth #axis-5

## Goal

This advances **Current focus: codex support through an agent adaptor
layer** — making codex a *usable* second backend, not just scaffolding.

TB-358's backend-aware daemon-start auth gate currently requires
`OPENAI_API_KEY` for any codex-backed kind (`ap2/cli_daemon.py:63`,
`_CODEX_CREDENTIAL_ENV = "OPENAI_API_KEY"`; the gate walks the resolved
per-kind backend set and requires that env var if any kind resolves to
`codex`). But codex supports **two** auth modes (verified against
OpenAI's codex docs):
- **OpenAI API key** — `OPENAI_API_KEY`, metered API billing; and
- **ChatGPT-login OAuth** — a subscription session created by
  `codex login` (browser) / `codex login --device-auth` (headless),
  stored at `$CODEX_HOME/auth.json` (default `~/.codex/auth.json`) with
  `"auth_mode": "chatgpt"` plus access+refresh tokens, **auto-refreshing
  (~every 8 days)**. The `codex_sdk` Python package the `CodexAdapter`
  imports supports both modes.

Requiring `OPENAI_API_KEY` forces the metered-API path and **rejects the
subscription OAuth path entirely** — which is inconsistent with how ap2
runs Claude itself: ap2 uses `CLAUDE_CODE_OAUTH_TOKEN` (OAuth
subscription), deliberately *not* an Anthropic API key (goal.md
Constraints: "OAuth auth … not API-key"). So a codex kind can't run on a
ChatGPT plan the way our Claude agents run on a Claude plan.

Widen the gate so a codex-backed kind's credential requirement is
satisfied by **EITHER** `OPENAI_API_KEY` **OR** a present codex
ChatGPT-login session.

Why now: codex is built end-to-end (axes 1-7 shipped) but not yet
adoptable on the subscription auth ap2 prefers — this gate is the single
thing that would reject a `codex login` setup and force metered API
spend. Closing it makes codex actually runnable the same way we run
Claude (plan-based, no per-call billing), which is the difference
between "codex scaffolding exists" and "codex is a usable backend." It
directly serves the codex-support focus.

## Scope

- **Widen the gate** (`ap2/cli_daemon.py`, the backend-aware credential
  check ~L71-129). For codex-backed kinds, treat the requirement as
  satisfied iff EITHER:
  - `OPENAI_API_KEY` is set (today's behavior), OR
  - a codex ChatGPT-login session is present: a readable
    `auth.json` at `$CODEX_HOME/auth.json` (default `~/.codex/auth.json`)
    whose parsed JSON has `auth_mode == "chatgpt"`.
  Replace the single `_CODEX_CREDENTIAL_ENV` notion with a
  `_codex_credentials_present()` helper returning True for either path.
- **Keep it a cheap pre-flight presence check.** Mirror the Claude side,
  which only checks that `CLAUDE_CODE_OAUTH_TOKEN` is *present* and does
  NOT validate it. Do NOT shell out to codex, hit the network, or check
  token freshness — `codex_sdk` refreshes the session at runtime. Reading
  `auth.json` to confirm it exists and is `auth_mode: chatgpt` is enough.
- **Respect `CODEX_HOME`** for the auth.json location (fall back to
  `~/.codex`).
- **Failure message** names BOTH options: a codex kind with neither
  `OPENAI_API_KEY` nor a chatgpt `auth.json` still fails the daemon-start
  gate, with a message pointing at "set `OPENAI_API_KEY`, or run
  `codex login` to create `~/.codex/auth.json`."
- **All-claude maps unchanged**: a backend map with no codex kind still
  requires only `CLAUDE_CODE_OAUTH_TOKEN`, exactly as today.
- **Comments / docstrings**: update the gate comment + the
  `ap2/adapters/select.py` docstring (currently "OpenAI for any
  codex-backed kind") to "an OpenAI API key OR a codex ChatGPT-login
  session."
- **Docs**: `ap2/howto.md` codex/auth guidance documents the two codex
  auth modes and that the daemon-start gate accepts either.
- **No secret handling**: read only `auth_mode` / file existence; never
  log token contents.

## Design

- **Presence-parity with the Claude gate.** The gate's job is to stop the
  daemon from dispatching a codex kind that will instantly fail for lack
  of *any* credential — not to authenticate. A structural check (env var
  set, or auth.json present + chatgpt mode) is the exact analog of the
  Claude side's `CLAUDE_CODE_OAUTH_TOKEN`-present check.
- **Subscription-first, matching ap2's Claude posture.** This lets a
  codex kind run on a ChatGPT plan with no metered API billing — the
  same model ap2 already uses for Claude — rather than forcing
  `OPENAI_API_KEY`.
- **Refresh is codex's job.** `auth.json` carries a refresh token codex
  rotates ~every 8 days; the gate must not try to validate or refresh
  it, only confirm the session exists.

## Verification

- `uv run --extra dev pytest -q ap2/tests/ --ignore=ap2/tests/smoke` — full suite passes including the new gate tests.
- `grep -qnE "auth_mode|CODEX_HOME|auth\\.json" ap2/cli_daemon.py` — the gate now recognizes the codex ChatGPT-login session path, not just the env var.
- `grep -qnE "OPENAI_API_KEY" ap2/cli_daemon.py` — the API-key path is still accepted.
- `ap2/cli_daemon.py` Prose: the backend-aware daemon-start auth gate treats a codex-backed kind's credential as satisfied by EITHER `OPENAI_API_KEY` OR a `$CODEX_HOME`/`~/.codex/auth.json` file with `auth_mode: chatgpt`; an all-claude backend map still requires only `CLAUDE_CODE_OAUTH_TOKEN`; a codex kind with neither credential fails with a message naming both options. The check is a presence-only pre-flight (no network, no token validation), mirroring the Claude gate. Judge confirms via Read.
- New test: with a temp `CODEX_HOME` containing an `auth.json` of `{"auth_mode": "chatgpt", ...}` and `OPENAI_API_KEY` unset, the gate passes for a codex-backed kind; with both absent it fails; an all-claude map is unaffected.

## Out of scope

- Declaring `codex_sdk` as an installable optional dependency / extra — a sibling follow-up (needed to actually run codex, but independent of the auth gate).
- Implementing or wrapping the `codex login` flow itself — that's the codex CLI's job; ap2 only checks for the resulting session.
- Validating, refreshing, or rotating the codex token — `codex_sdk` handles that at runtime.
- Any change to the Claude `CLAUDE_CODE_OAUTH_TOKEN` gate.

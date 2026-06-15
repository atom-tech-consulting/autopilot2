# CodexAdapter: isolate daemon codex session storage to an ap2-private CODEX_HOME (stop polluting the operator's `codex resume` history)

Tags: #autopilot #codex #backend #adapter #session #operator-experience

## Goal

When ap2 dispatches a task to the **codex** backend, every run is persisted as a
rollout/session under the operator's `~/.codex/sessions/` (plus `history.jsonl`
and the codex sqlite logs) — so the daemon's headless automation runs show up in
the operator's interactive `codex resume` / session picker. The Claude backend
has no such leak: `sdk.query()` is headless and writes nothing to the interactive
Claude Code session history. The asymmetry is because `CodexAdapter` instantiates
the client as bare `openai_codex.AsyncCodex()` (`ap2/adapters/codex.py:18`, `:703`)
with **no `CodexConfig`**, so codex uses the default `CODEX_HOME` (`~/.codex`).

Isolate the daemon's codex runs so they don't pollute the operator's interactive
codex history, mirroring the Claude backend's headless behavior. Meta-infra /
operator-experience fix on the shipped codex backend; no focus anchor.

## Scope

- **Pass a `CodexConfig` that redirects `CODEX_HOME`.** Construct the codex
  client (`AsyncCodex`) with
  `CodexConfig(env={"CODEX_HOME": <ap2-private dir>, ...inherited...})` so all
  per-run artifacts (the `sessions/` rollouts, `history.jsonl`, the sqlite logs)
  land in an ap2-private home instead of `~/.codex`. Suggested location:
  per-project `.cc-autopilot/codex_home/` (co-located with daemon state,
  gitignored) — implementer's call vs a per-user dir.
- **Wire the credential into the redirected home.** Codex reads `auth.json` from
  `CODEX_HOME`, so the private home must resolve a credential — symlink
  `~/.codex/auth.json` into it (a symlink lets codex's ~8-day refresh-token
  rotation write through to the shared file, keeping both fresh), or rely on
  `OPENAI_API_KEY` when that's the configured auth mode. Do NOT copy token
  contents around; do NOT log them.
- **Preserve `env` the codex engine needs.** `CodexConfig(env=...)` replaces the
  child env — make sure to carry through whatever the spawned codex binary
  requires (PATH, etc.) plus the `CODEX_HOME` override, rather than handing it a
  bare one-key env.
- **Confirm codex's rollout-persistence semantics** as part of the work: determine
  whether `history.persistence = "none"` (passable via
  `CodexConfig(config_overrides=("history.persistence=none",))`) suppresses the
  `sessions/` rollout files or only `history.jsonl`. Record the finding in the
  briefing/PR and set the `config_overrides` persistence knob ONLY if it's needed
  beyond the `CODEX_HOME` redirect — don't guess the key blind.
- **Gitignore the private home** (it's transient runtime state — `ap2 rollback`
  should not restore it; per the track-iff-rollback-restores rule). Update the
  state-file-gitignore-drift coverage accordingly.
- **Do not affect the operator's interactive codex.** The redirect lives in
  `CodexAdapter` (only the daemon's runs use it); a human running `codex`
  directly still uses `~/.codex` and is unaffected. The daemon-start auth gate
  (`_codex_credentials_present`) stays as-is (canonical-credential presence
  check); this task only changes where *runs* write.

## Design

- `CodexConfig` (`openai_codex` 0.1.0b2) exposes `env` / `config_overrides` /
  `cwd` / `codex_bin` / `launch_args_override`; ap2 currently passes none. The
  `env={"CODEX_HOME": ...}` redirect is the complete, behavior-stable fix
  (isolates *all* artifacts); the `config_overrides` persistence knob is a
  possible complement pending the rollout-semantics finding above.
- Keep `normalize_options` (model/effort/cwd/approval) unchanged — this is purely
  about where the codex engine persists session state, not how the turn runs.

## Verification

- `grep -qE 'CodexConfig' ap2/adapters/codex.py` — the adapter constructs the codex client with a `CodexConfig`.
- `grep -qE 'CODEX_HOME' ap2/adapters/codex.py` — the config redirects `CODEX_HOME`.
- `uv run --extra dev pytest -q ap2/tests/test_state_file_gitignore_drift.py` — the private codex home is gitignored (drift gate passes).
- New test (no real SDK): assert `CodexAdapter` builds its `AsyncCodex` with a `CodexConfig` whose `env["CODEX_HOME"]` points at the ap2-private dir (not `~/.codex`), and that the credential is made resolvable from that home.
- `ap2/adapters/codex.py` Prose: the adapter instantiates `AsyncCodex` with a `CodexConfig` redirecting `CODEX_HOME` to a gitignored ap2-private dir with the codex credential wired in (symlinked `auth.json` or `OPENAI_API_KEY`), so daemon codex runs' session/rollout/history artifacts no longer write to `~/.codex`; the operator's interactive `codex` is untouched; and the codex rollout-persistence finding is recorded (with the `history.persistence` override set iff needed). Judge confirms via Read.

## Out of scope

- The Claude backend (already headless via `sdk.query()`).
- The daemon-start auth gate logic (unchanged — canonical-credential presence check).
- Any change to how a codex turn runs (model/effort/approval/tool round-trips) — storage location only.
- Cleaning/rotating the private codex home's accumulated sessions (a later hygiene concern if it grows; not this task).

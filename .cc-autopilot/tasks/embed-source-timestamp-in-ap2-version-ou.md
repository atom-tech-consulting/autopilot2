# Embed source timestamp in ap2 --version output

## Goal

Today `ap2 --version` prints a static `ap2 0.3.0` from `pyproject.toml`. That tells you which package was installed but not which source revision the running CLI / daemon is loading. With editable installs (the common case here — both lzhang and claude-agent install via `uv tool install --editable`), the package version stays at 0.3.0 across many source-tree changes, so the operator has to fall back on `git log` or file mtimes to confirm freshness.

Bump format to something like `0.3.0+<git-shortsha>.<commit-ts>` (e.g. `0.3.0+a8d2e57.20260430T235300Z`) so a single `ap2 --version` answers "is this build current?" by visual comparison against `git log -1`.

## Why

This session burned ~1 hour on a stale-source bug: lzhang's tool venv was editable-pointing at `/Users/lzhang/dev/atom/autopilot2` (pre-TB-131), claude-agent's at `/Users/claude-agent/repos/autopilot2` (current). Both reported `ap2 0.2.0`. We had to debug the symptom (TB-132/TB-135 verification kept failing) before realizing the daemon was loading stale `verify.py`. A version string that included the source-commit timestamp would have caught it at first invocation.

## Scope

- (1) Replace the static `version = "0.3.0"` lookup. Implementation choice (pick whichever fits the repo style):
  - **Runtime, git-derived (preferred)**: `ap2/__init__.py` exposes `__version__` computed lazily as `f"{BASE} +{sha}.{ts}"` where `sha = subprocess('git -C <pkg-root> log -1 --format=%h')` and `ts = subprocess('git -C <pkg-root> log -1 --format=%cd --date=format:%Y%m%dT%H%M%SZ')`. Falls back to plain BASE when not in a git repo (released wheel).
  - **Build-time**: `pyproject.toml` uses `setuptools-scm` (or `hatch-vcs`) to embed the latest tag + commit hash. Auto-updates on every install but not between installs of an editable tree.
  - **Hybrid**: BASE in pyproject.toml; a small `_version.py` is regenerated on every CLI invocation (or every Config.load) with current git HEAD info. Cheap (~few ms) since git log is local.
- (2) `ap2/cli.py` `--version` flag prints the full string so `ap2 --version` shows `ap2 0.3.0+a8d2e57.20260430T235300Z` rather than `ap2 0.3.0`. Keep argparse's standard `--version` semantics.
- (3) `ap2 status` already prints daemon URL — add the version line so operators see freshness alongside daemon liveness without a second command.
- (4) `events.jsonl` daemon_start event grows a `version` field carrying the same string so post-mortem can correlate state with source.

## Verification

- `uv run pytest -q ap2/tests/` — full regression gate passes (gating)
- `ap2 --version` output matches the regex `^ap2 0\.\d+\.\d+(\+[a-f0-9]{7,}\.\d{8}T\d{6}Z)?$` — base + optional git suffix.
- New unit test (or integration test) in `test_cli.py`: invoking `cmd_version` (or whatever the entrypoint is) on a directory that IS a git repo includes a SHA-and-timestamp suffix in the printed string.
- New unit test: invoking `cmd_version` on a directory that is NOT a git repo (e.g. `tmp_path` fixture) prints just the base version, no `+suffix` — fallback path works for released wheels.
- New unit test: when `Config.load` runs, the loaded config's `__version__` (or equivalent accessor) matches the CLI-printed string. Pins parity between status / cli / daemon_start event.
- The diff updates `daemon._tick`'s `daemon_start` event emission to include the `version` field, and `cli.cmd_status` to print the version line. Tests pin both.

## Out of scope

- A `--short` / `--full` flag for the version string. One canonical format is enough.
- A version bump policy (manual 0.3.0 → 0.4.0 etc.) — that stays operator-managed in pyproject.toml.
- Source-tree mtime as the freshness signal — git's commit timestamp is more meaningful (uncommitted edits are an operator-visible state, not something to encode in the version string).
## Attempts

### 2026-05-01 — state_violation
(no summary)
- **fenced_files:** .cc-autopilot/operator_queue.jsonl, CLAUDE.md
- **pre_run_head:** d53ec067
- **Debug dumps:** `prompt: .cc-autopilot/debug/20260501T014608Z-TB-139.prompt.md`, `stream: .cc-autopilot/debug/20260501T014608Z-TB-139.stream.jsonl`, `messages: .cc-autopilot/debug/20260501T014608Z-TB-139.messages.jsonl`

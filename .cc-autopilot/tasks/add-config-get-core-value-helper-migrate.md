## Goal

Core (non-component) cluster migration for axis (5) of the
**Current focus: structured config (env → TOML)** (goal.md L266 /
L353-364). The per-component sweep (TB-326..331 + TB-332 / TB-333
this cycle) handles `components.<name>.*` reads via
`Config.get_component_value`, but the `[core.*]` section that
goal.md L308 specifies has no analogous helper. Per
FLAT_TO_SECTIONED L100-114 in `ap2/config_compat.py`, 12+ knobs map
to `core.*`: `AP2_AGENT_MODEL` → `core.agent_model`,
`AP2_AGENT_EFFORT` → `core.agent_effort`, `AP2_TASK_MAX_TURNS` →
`core.task_max_turns`, `AP2_CONTROL_MAX_TURNS` →
`core.control_max_turns`, `AP2_IDEATION_MAX_TURNS` →
`core.ideation_max_turns`, `AP2_VERIFY_JUDGE_MAX_TURNS` →
`core.verify_judge_max_turns`, `AP2_WEB_PORT`, `AP2_WEB_DISABLED`,
`AP2_IDEATION_*` family. The schema is plumbed; readers in
`ap2/daemon.py` (L223 `task_max_turns`, L226-227 `agent_model` /
`agent_effort`, L775 `control_max_turns`, L868 `agent_effort`,
L903 `agent_model`), `ap2/verify.py` (L564 `agent_effort`, L573
`verify_judge_max_turns`, L575 `agent_model`),
`ap2/status_report.py` (L2024 `agent_effort`), and
`ap2/components/janitor/__init__.py` (L214 `agent_effort`, L789
`agent_model`) still call `os.environ.get` directly.

This task adds `Config.get_core_value(key, default=None)` paralleling
`get_component_value` (same precedence: sectioned-env > flat-env >
TOML > default), then migrates the ~10 agent-runtime reads listed
above. The ideation-cluster core reads (`AP2_IDEATION_*` in
`ap2/ideation.py` + `ap2/ideation_scrub.py`) are covered by sibling
TB-335 this cycle so the two migrations stay independently
verifiable.

Why now: without `get_core_value`, every core-cluster reader has to
either (a) keep direct `os.environ.get` (defeats the migration) or
(b) hand-roll the precedence chain — neither scales. Adding the
helper here is one-touch and unlocks both TB-335 and the
~remaining core readers (AP2_WEB_*, AP2_AUTO_DIAGNOSE_*). The
agent-runtime knobs are dispatched on every task / verify / janitor
tick — visible cost surface.

## Scope

- Add `Config.get_core_value(key, default=None)` method on the
  `Config` dataclass in `ap2/config.py`, paralleling
  `get_component_value`. Precedence (high → low):
  1. Sectioned env `AP2_CORE_<KEY>` (canonical naming under the
     sectioned regime — confirm against any existing sectioned-env
     map in `config_compat.py`, e.g. `_apply_sectioned_env_overrides`).
  2. Flat env via reverse-FLAT_TO_SECTIONED lookup for any
     `core.<key>` mapping (catches the existing
     `AP2_AGENT_MODEL` / `AP2_TASK_MAX_TURNS` / etc. flat names).
  3. `self.core_config.get(key)` (the cfg snapshot from
     `config_loader.from_toml`'s `[core]` overlay).
  4. `default`.
  Match `get_component_value`'s docstring shape, the
  type-coercion-only-via-schema policy, and the same purity contract.
- Migrate the listed call sites to use the new helper:
  - `ap2/daemon.py` L223 (`task_max_turns`),
    L226-227 (`agent_model` / `agent_effort`),
    L775 (`control_max_turns`),
    L868 (`agent_effort`),
    L903 (`agent_model`).
  - `ap2/verify.py` L564 (`agent_effort`),
    L573 (`verify_judge_max_turns`),
    L575 (`agent_model`).
  - `ap2/status_report.py` L2024 (`agent_effort`).
  - `ap2/components/janitor/__init__.py` L214 (`agent_effort`),
    L789 (`agent_model`).
- Each migrated call site must preserve its existing default value
  exactly (`"claude-opus-4-7"`, `"xhigh"` / `"high"` / `"medium"`
  per site, `DEFAULT_TASK_MAX_TURNS`, `15`, `20`).
- Confirm the Config dataclass has a `core_config` field (TB-321 /
  TB-322 plumbing) or add an empty `dict[str, Any]` field analogous
  to `components_config` if missing.
- New regression-pin test
  `ap2/tests/test_tb334_core_cfg_reads.py`: per-knob behavioral test
  asserts cfg-read returns the same value the env-read would have
  under monkeypatch (covers each migrated key under both
  sectioned-env and flat-env overrides), plus a grep-walk asserting
  zero remaining `os.environ.get("AP2_AGENT_` /
  `os.environ.get("AP2_TASK_MAX_TURNS"` /
  `os.environ.get("AP2_CONTROL_MAX_TURNS"` /
  `os.environ.get("AP2_VERIFY_JUDGE_MAX_TURNS"` calls in the listed
  consumer files.

## Design

`get_core_value` mirrors `get_component_value` shape — option-2
helper pattern proven across TB-326..331. Single addition to
`Config` in `ap2/config.py`, same purity contract, same env-first
precedence to preserve the pre-migration test-monkeypatch idiom.

Call site shape — each existing read becomes:

```python
# Before
max_turns=int(os.environ.get("AP2_TASK_MAX_TURNS", DEFAULT_TASK_MAX_TURNS)),

# After
max_turns=int(cfg.get_core_value("task_max_turns", default=DEFAULT_TASK_MAX_TURNS)),
```

Verify that `cfg` is in scope at each migration site — the daemon
threads it explicitly already (`cfg.tick_interval_s` reads exist);
`verify.py` consumes it in the verifier path; `status_report.py`
passes it through `collect_window_*` helpers; `janitor/__init__.py`
sees it via the tick hook signature. Where `cfg` isn't in scope at
a call site, prefer threading it through over keeping the env read.

If the migration walk surfaces latent bugs (TB-326 60bdb1f / TB-330
manifest-schema mismatch pattern), close them in a follow-up commit.

Why now: see Goal — the helper unblocks TB-335 (ideation cluster)
and the residual `AP2_WEB_*` / `AP2_AUTO_DIAGNOSE_*` migrations,
plus the agent-runtime reads fire on every dispatch.

## Verification

- `uv run pytest -q` — full suite passes (regression gate).
- `uv run pytest -q ap2/tests/test_tb334_core_cfg_reads.py` — new
  core-cluster test passes.
- `grep -nE "def get_core_value" ap2/config.py` — helper present.
- `! grep -rqE "os\.environ\.get\(.AP2_AGENT_" ap2/daemon.py ap2/verify.py ap2/status_report.py ap2/components/janitor/`
  — zero remaining direct env reads of AP2_AGENT_* in the listed
  files (per TB-270 absence-check convention).
- `! grep -rqE "os\.environ\.get\(.AP2_TASK_MAX_TURNS" ap2/daemon.py`
  — zero remaining AP2_TASK_MAX_TURNS reads in daemon.py.
- `! grep -rqE "os\.environ\.get\(.AP2_CONTROL_MAX_TURNS" ap2/daemon.py`
  — zero remaining AP2_CONTROL_MAX_TURNS reads in daemon.py.
- `! grep -rqE "os\.environ\.get\(.AP2_VERIFY_JUDGE_MAX_TURNS" ap2/verify.py`
  — zero remaining AP2_VERIFY_JUDGE_MAX_TURNS reads in verify.py.
- `grep -rE "get_core_value\(" ap2/daemon.py ap2/verify.py ap2/status_report.py ap2/components/janitor/`
  — new resolved-config read path present in each migrated file.
- `uv run python -m ap2 status --project .` exits 0 (daemon-loaded
  cfg correctly resolves the core knobs the status path reads).

## Out of scope

- Ideation cluster knobs (`AP2_IDEATION_*`) — sibling TB-335 this
  cycle.
- `AP2_WEB_PORT` / `AP2_WEB_DISABLED` migration — follow-up task
  next cycle once helper stabilizes.
- `AP2_AUTO_DIAGNOSE_*` core reads — follow-up.
- `AP2_TICK_S` / `AP2_TASK_TIMEOUT_S` / `AP2_VERIFY_CMD` / friends
  that already exist as Config dataclass fields (the
  `cfg.tick_interval_s` etc. attributes) — these already flow
  through cfg; no change needed.
- Removing keys from FLAT_TO_SECTIONED or `_KNOBS_STAYING_ENV_ONLY`
  — back-compat stays through the full migration arc.

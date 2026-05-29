## Goal

Core ideation cluster migration for axis (5) of the **Current focus:
structured config (env → TOML)** (goal.md L266 / L353-364). Sibling
to TB-334 — the new `Config.get_core_value` helper (added in
TB-334) parallels `get_component_value` for the `[core.*]` schema
section. The ideation cluster owns 5 `core.*` mappings per
FLAT_TO_SECTIONED L112-115 in `ap2/config_compat.py`:
`AP2_IDEATION_DISABLED` → `core.ideation_disabled`,
`AP2_IDEATION_TRIGGER_TASK_COUNT` →
`core.ideation_trigger_task_count`, `AP2_IDEATION_COOLDOWN_S` →
`core.ideation_cooldown_s`, `AP2_IDEATION_SCRUB_MODEL` →
`core.ideation_scrub_model`, `AP2_IDEATION_MAX_TURNS` →
`core.ideation_max_turns` (already in TB-334's agent-runtime scope —
this brief covers only the 4 ideation-cluster-specific knobs not
overlapping TB-334).

Identified readers: `ap2/ideation.py` L566 (`_ideation_cooldown_s`,
`AP2_IDEATION_COOLDOWN_S`), L584 (`_trigger_task_count`,
`AP2_IDEATION_TRIGGER_TASK_COUNT`), L929 (`_ideation_disabled`,
`AP2_IDEATION_DISABLED`); `ap2/ideation_scrub.py` L166
(`AP2_IDEATION_SCRUB_MODEL`). Each is a small helper called from
the cron path; the `cfg`-kwarg-+-TypeError-guard shape (TB-327)
fits each cleanly. The `AP2_IDEATION_MAX_TURNS` read at
`ap2/ideation.py` L789 lands in TB-334's agent-runtime sweep — call
out the boundary so the two tasks don't double-touch.

Why now: ideation is the cron entry point this very task runs
under — migrating its knob reads tightens the operator's
config-discovery story end-to-end (set the knob in TOML once, see
it on `ap2 config get core.ideation_cooldown_s` immediately
without a daemon restart). Without it, even after TB-334 lands,
the most operator-visible cron path still reads env directly.

## Scope

- Migrate the 4 `os.environ.get("AP2_IDEATION_...")` call sites
  listed above to read via `Config.get_core_value(<key>,
  default=<existing default>)`.
- Adopt the TB-327 cfg-kwarg-+-TypeError-guard shape on each
  helper: `cfg: Config | None = None` kwarg with default-None
  preserving the legacy env-read for zero-callers-broken
  back-compat. Update the ideation cron entry point and any direct
  caller in `ap2/cli_review.py` to pass `cfg`.
- Confirm `ap2/ideation.py` and `ap2/ideation_scrub.py` already
  receive `cfg` (the cron-dispatch path runs under the daemon's
  cfg snapshot); if a helper is called from a context where cfg
  isn't reachable, thread it through rather than keep the env
  read.
- New regression-pin test
  `ap2/tests/test_tb335_ideation_cfg_reads.py`: grep-walk asserts
  zero remaining `os.environ.get("AP2_IDEATION_DISABLED"` /
  `os.environ.get("AP2_IDEATION_COOLDOWN_S"` /
  `os.environ.get("AP2_IDEATION_TRIGGER_TASK_COUNT"` calls in
  `ap2/ideation.py`, and zero remaining
  `os.environ.get("AP2_IDEATION_SCRUB_MODEL"` calls in
  `ap2/ideation_scrub.py`; per-knob behavioral test asserts
  cfg-read returns the same value the env-read would have under
  monkeypatch.
- Preserve TB-89 (insights index), TB-94 (cooldown semantics),
  TB-191 (decisions-needed shape) behavior contracts.

## Design

Reuse the TB-334 helper (`Config.get_core_value`) verbatim across
all 4 call sites. Each migration is a one-line diff:

```python
# Before
v = os.environ.get("AP2_IDEATION_COOLDOWN_S")
# After
v = cfg.get_core_value("ideation_cooldown_s", default=None)
```

For `_ideation_disabled` (truthy parse), preserve the existing
`.strip() in ("1", "true", "yes")` shape — only the read source
changes. For `AP2_IDEATION_SCRUB_MODEL` in `ideation_scrub.py`,
thread `cfg` through the scrub entry point.

If the migration walk surfaces a latent bug (TB-326 60bdb1f /
TB-330 manifest-schema pattern), close it in a follow-up commit.

Why now: see Goal — ideation is the most visible per-cycle cron
path; finishing its cluster closes the legible-feedback loop for
operators tuning `core.ideation_*` knobs.

## Verification

- `uv run pytest -q` — full suite passes (regression gate).
- `uv run pytest -q ap2/tests/test_tb335_ideation_cfg_reads.py` —
  new ideation-cluster test passes.
- `! grep -rqE "os\.environ\.get\(.AP2_IDEATION_DISABLED" ap2/ideation.py`
  — zero remaining direct env reads (per TB-270 absence-check
  convention).
- `! grep -rqE "os\.environ\.get\(.AP2_IDEATION_COOLDOWN_S" ap2/ideation.py`
  — zero remaining direct env reads.
- `! grep -rqE "os\.environ\.get\(.AP2_IDEATION_TRIGGER_TASK_COUNT" ap2/ideation.py`
  — zero remaining direct env reads.
- `! grep -rqE "os\.environ\.get\(.AP2_IDEATION_SCRUB_MODEL" ap2/ideation_scrub.py`
  — zero remaining direct env reads.
- `grep -rE "get_core_value\(.ideation_" ap2/ideation.py ap2/ideation_scrub.py`
  — new resolved-config read path present.
- `uv run python -m ap2 status --project .` exits 0 (cfg path
  resolves the ideation knobs cleanly).

## Out of scope

- `AP2_IDEATION_MAX_TURNS` migration — already in TB-334's
  agent-runtime sweep (avoid double-touch).
- Other core knobs (`AP2_WEB_PORT`, `AP2_WEB_DISABLED`,
  `AP2_AUTO_DIAGNOSE_IDLE_THRESHOLD_S`, etc.) — follow-up next
  cycle.
- Behavior changes to the ideation cron path (TB-89 / TB-94 /
  TB-191 contracts unchanged) — pure read-path swap only.
- `_KNOBS_STAYING_ENV_ONLY` curation — deferred per
  ideation_state.md.

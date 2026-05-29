## Goal

Cross-package consumer migration for axis (5) of the **Current focus:
structured config (env → TOML)** (goal.md L266 / L353-364). TB-326..331
migrated the component-BODY reads of `AP2_AUTO_APPROVE*` knobs in
`ap2/components/auto_approve/`, but the same knobs are still read via
direct `os.environ.get` by ~10 call sites OUTSIDE the component:
`ap2/automation_status.py` (L118 `_is_auto_approve_dry_run`, L143
`_is_validator_judge_noisy_pause_disabled`, L204 `_freeze_threshold`,
L524 + L526-527 `collect_auto_approve_state`), `ap2/board_edits.py`
(L243, L256 `auto_approved` event knob field), `ap2/operator_queue.py`
(L1513, L1526 — same shape), `ap2/doctor.py` (L146, L155, L156),
`ap2/ideation.py` (L639 `_auto_approve_enabled`, L658
`_auto_approve_gate_tags`), `ap2/tests/conftest.py`. These leak past
TB-326's grep gate because TB-326's verification scoped to the
component body only. Each reader is a sibling cluster knob (per
`FLAT_TO_SECTIONED` in `ap2/config_compat.py` L117-124:
`AP2_AUTO_APPROVE*` → `components.auto_approve.*`) — the
`cfg.get_component_value("auto_approve", <key>)` helper already
exists on `Config` (TB-326 b3eba54). This task finishes the
auto_approve-cluster sweep so the L398-399 progress signal
("≥80% of source-side `os.environ.get('AP2_*')` calls migrated to
`cfg.<path>.<key>` reads") moves another ~10 reads toward target.

Why now: TB-326..331 settled the per-component template and the
cross-package leak is the largest remaining axis-5 deficit — the
70-vs-11 grep ratio (outside vs inside `ap2/components/`) is
driven mostly by these cross-package readers, and operator-facing
surfaces (`ap2 status`, the cron status-report) all touch
`automation_status.py` so an inconsistent shape there is visible
on every tick. Deferring lets the residual env-read sprawl grow
as new features land.

## Scope

- Migrate every `os.environ.get("AP2_AUTO_APPROVE...")` reader
  OUTSIDE `ap2/components/auto_approve/` to read via
  `Config.get_component_value("auto_approve", <key>)`. Per
  FLAT_TO_SECTIONED L117-124 the keys are: `enabled`, `dry_run`,
  `gate_tags`, `freeze_threshold`, `per_task_token_cap`,
  `window_token_cap`, `noisy_pause_disabled`.
- Adopt the TB-327 cfg-kwarg shape: each helper that today reads
  env at call-time gains a `cfg: Config | None = None` kwarg
  guarded by a TypeError-on-positional pattern; default-None preserves
  the legacy env-read fallback for zero-callers-changed back-compat.
  Update direct callers (status-report path,
  `collect_auto_approve_state`, board_edits / operator_queue
  knob-field emit sites) to pass `cfg`.
- `automation_status.collect_auto_approve_state(cfg, ...)` already
  takes `cfg`; replace its internal `os.environ.get` reads with
  `cfg.get_component_value("auto_approve", ...)`. Helpers that lack
  cfg (`_is_auto_approve_dry_run`, `_freeze_threshold`,
  `_positive_int_cap`) gain the kwarg per above.
- `board_edits.py` L243 / L256 + `operator_queue.py` L1513 / L1526
  embed the knob value in emitted `auto_approved` / `would_auto_approve`
  events for audit. Switch each call-site to cfg-read while
  preserving the event field's string shape exactly (downstream
  parsers expect the raw env-style value).
- New regression-pin test
  `ap2/tests/test_tb332_auto_approve_cross_package_cfg_reads.py`:
  grep-walk asserts zero remaining
  `os.environ.get("AP2_AUTO_APPROVE` calls in
  `ap2/automation_status.py`, `ap2/board_edits.py`,
  `ap2/operator_queue.py`, `ap2/doctor.py`, `ap2/ideation.py`,
  `ap2/cli_daemon.py`; per-knob behavioral test asserts cfg-read
  returns the same value the env-read would have under monkeypatch.
- Existing tests pass without modification: TB-223 / TB-232 / TB-243
  / TB-272 behavior contracts unchanged.

## Design

Adopt the TB-327 cfg-kwarg-with-TypeError-guard pattern verbatim:

```python
def _is_auto_approve_dry_run(cfg: "Config | None" = None) -> bool:
    if cfg is not None and not isinstance(cfg, Config):
        raise TypeError(...)
    if cfg is not None:
        return _is_truthy(cfg.get_component_value("auto_approve", "dry_run", default=""))
    # Legacy fallback (TB-327 back-compat shape): pre-cfg callers
    # still get the env-read behavior.
    return _is_truthy(os.environ.get("AP2_AUTO_APPROVE_DRY_RUN"))
```

Callers in the daemon path (status_report, cli_daemon) thread `cfg`
explicitly; uncovered legacy callers fall through to the env path
until follow-up. If the migration walk surfaces a latent bug (as
TB-326's 60bdb1f and TB-330's manifest-schema mismatch did), the
agent may close it in a follow-up commit on the same task.

Why now: the per-component sweep finishes with TB-331 — without
this cross-package follow-through, ~10 knob reads remain
hand-rolled, the progress signal sits below target, and every new
feature adds more sprawl.

## Verification

- `uv run pytest -q` — full suite passes (regression gate).
- `uv run pytest -q ap2/tests/test_tb332_auto_approve_cross_package_cfg_reads.py`
  — new cross-package test passes.
- `! grep -rqE "os\.environ\.get\(.AP2_AUTO_APPROVE" ap2/automation_status.py ap2/board_edits.py ap2/operator_queue.py ap2/doctor.py ap2/ideation.py ap2/cli_daemon.py`
  — zero remaining direct env reads of AP2_AUTO_APPROVE keys in
  the listed cross-package files (passes iff grep finds zero
  matches, per TB-270 absence-check convention).
- `grep -rE "get_component_value\(.auto_approve." ap2/automation_status.py ap2/board_edits.py ap2/operator_queue.py`
  — new resolved-config read path present in the three primary
  consumers.
- `uv run python -m ap2 status --project .` exits 0 and the
  auto-approve status block still renders ("auto-approve: enabled"
  / token caps) — sanity that the cfg-read swap didn't break the
  status enumeration.

## Out of scope

- The auto_unfreeze / validator_judge cross-package readers — TB-333
  (this cycle's sibling).
- Core (non-component) knob migration — TB-334 / TB-335 this cycle.
- `_KNOBS_STAYING_ENV_ONLY` curation pass — deferred per
  ideation_state.md "Considered & deferred".
- howto.md `## Configuration knobs` tree-render rewrite — deferred.
- Behavior changes to the auto-approve gate chain (tags, freeze
  threshold, dry-run semantics) — pure read-path swap only.

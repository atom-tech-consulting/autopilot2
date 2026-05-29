## Goal

Cross-package consumer migration for axis (5) of the **Current focus:
structured config (env → TOML)** (goal.md L266 / L353-364). Sibling
to TB-332 — the per-component briefs (TB-327 for auto_unfreeze,
TB-331 for validator_judge) only swept knob reads inside
`ap2/components/<name>/`; the same knobs are still hand-rolled
elsewhere. Identified cross-package readers:

- `AP2_AUTO_UNFREEZE_*` (5 knobs per FLAT_TO_SECTIONED L126-130
  `disabled`, `dry_run`, `fix_shapes`, `max_per_task`, `max_per_day`):
  `ap2/doctor.py` L229, L231, L233, L236; `ap2/automation_status.py`
  L166 `_is_auto_unfreeze_dry_run`; `ap2/_shared.py` comment refs;
  `ap2/tests/conftest.py`.
- `AP2_VALIDATOR_JUDGE_*` (5 knobs per FLAT_TO_SECTIONED L132-136
  `disabled`, `max_tokens`, `max_turns`, `noisy_threshold`,
  `timeout_s`): `ap2/doctor.py` L670; `ap2/automation_status.py` L184
  `validator_judge_noisy_threshold`; `ap2/tests/conftest.py` L77.

Combined ~14 reads across 5 files. Same shape as TB-332 — the
`cfg.get_component_value("<name>", <key>)` helper covers both clusters
identically; the migration is a mechanical sweep using the
TB-327 cfg-kwarg-+-TypeError-guard back-compat shape. The
`auto_unfreeze` cluster is gated on TB-327's body-level migration
landing so the operator-facing cfg read path is canonical at the
helper level; `validator_judge` gates on TB-331's body migration for
the same reason.

Why now: same as TB-332 — finish the cross-package leak for these
two clusters so the L398-399 progress signal lifts another ~14
readers off direct env. `automation_status.py` and `doctor.py` are
operator-facing surfaces (`ap2 status` block, `ap2 doctor` exit
codes); inconsistent shape there is visible on every tick / doctor
run. Deferring lets sprawl compound as new operator surfaces add
knob references.

## Scope

- Migrate every `os.environ.get("AP2_AUTO_UNFREEZE_...")` reader
  OUTSIDE `ap2/components/auto_unfreeze/` to read via
  `Config.get_component_value("auto_unfreeze", <key>)`.
- Migrate every `os.environ.get("AP2_VALIDATOR_JUDGE_...")` reader
  OUTSIDE `ap2/components/validator_judge/` to read via
  `Config.get_component_value("validator_judge", <key>)`.
- Adopt the TB-327 cfg-kwarg-+-TypeError-guard shape verbatim:
  helpers gain `cfg: Config | None = None` with default-None
  preserving legacy env-read; direct callers thread `cfg`.
- `doctor.py` runs as a top-level CLI; thread `cfg` from the entry
  point through the per-component check functions
  (`_auto_unfreeze_check`, `_validator_judge_check`).
- New regression-pin test
  `ap2/tests/test_tb333_unfreeze_judge_cross_package_cfg_reads.py`:
  grep-walk asserts zero remaining
  `os.environ.get("AP2_AUTO_UNFREEZE_` and
  `os.environ.get("AP2_VALIDATOR_JUDGE_` calls in
  `ap2/automation_status.py`, `ap2/doctor.py`, `ap2/_shared.py`,
  `ap2/briefing_validators.py`, `ap2/tests/conftest.py`;
  per-knob behavioral test asserts cfg-read matches env-read under
  monkeypatch.
- Preserve TB-238 / TB-243 / TB-249 / TB-272 behavior contracts.

## Design

Same as TB-332: TB-327 cfg-kwarg-with-TypeError-guard pattern. The
two clusters share the helper signature; the only difference is
the component name string. Each helper sees a one-call diff:

```python
def _is_auto_unfreeze_dry_run(cfg: "Config | None" = None) -> bool:
    if cfg is not None and not isinstance(cfg, Config):
        raise TypeError(...)
    if cfg is not None:
        return _is_truthy(cfg.get_component_value("auto_unfreeze", "dry_run", default=""))
    return _is_truthy(os.environ.get("AP2_AUTO_UNFREEZE_DRY_RUN"))
```

If the migration walk surfaces a latent bug (as TB-326's 60bdb1f
and TB-330's manifest-schema fix did), the agent may close it in a
follow-up commit on the same task.

Why now: see Goal — operator surfaces (status block, doctor exit
codes) are the highest-friction site for residual env reads, and
the per-component briefs structurally excluded them.

## Verification

- `uv run pytest -q` — full suite passes.
- `uv run pytest -q ap2/tests/test_tb333_unfreeze_judge_cross_package_cfg_reads.py`
  — new cross-package test passes.
- `! grep -rqE "os\.environ\.get\(.AP2_AUTO_UNFREEZE_" ap2/automation_status.py ap2/doctor.py ap2/_shared.py ap2/briefing_validators.py`
  — zero remaining direct env reads of AP2_AUTO_UNFREEZE in the
  listed cross-package files.
- `! grep -rqE "os\.environ\.get\(.AP2_VALIDATOR_JUDGE_" ap2/automation_status.py ap2/doctor.py ap2/_shared.py ap2/briefing_validators.py`
  — zero remaining direct env reads of AP2_VALIDATOR_JUDGE in the
  listed cross-package files.
- `grep -rE "get_component_value\(.auto_unfreeze." ap2/automation_status.py ap2/doctor.py`
  — new resolved-config read path present in primary consumers.
- `grep -rE "get_component_value\(.validator_judge." ap2/automation_status.py ap2/doctor.py`
  — new resolved-config read path present in primary consumers.
- `uv run python -m ap2 doctor --project .` exits 0 in the default
  configuration (sanity that the cfg-read swap didn't break the
  doctor check chain).

## Out of scope

- The auto_approve cross-package readers — TB-332 (this cycle's
  sibling).
- Core (non-component) knob migration — TB-334 / TB-335 this cycle.
- `tests/conftest.py` AP2_VALIDATOR_JUDGE_DISABLED gate — the
  test-suite-wide off-switch stays env-driven since cfg isn't
  constructed yet at conftest import time; documented as exempt
  in `_KNOBS_STAYING_ENV_ONLY` if not already.
- Changes to TB-238 / TB-243 / TB-249 / TB-272 behavior contracts.

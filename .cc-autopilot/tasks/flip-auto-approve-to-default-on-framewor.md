# Flip auto_approve to default-on framework-wide: autonomous-by-default, operators opt OUT (was opt-in default-off)

Tags: #autopilot #auto_approve #components #registry #config #posture #distribution

## Goal

Make `auto_approve` enabled by default for every ap2 install — including a fresh `uv
tool install` and the public source-available distribution — reversing the current
opt-in posture. Today the auto_approve manifest (`ap2/components/auto_approve/manifest.py`)
is `default_enabled=False` with `env_flag="AP2_AUTO_APPROVE"` (require-polarity / opt-in)
and a `config_schema` `enabled` key defaulting `False`, so a fresh project keeps every
ideation proposal `@blocked:review` until an operator approves. Operator decision
2026-06-23: ap2 should auto-approve by default and let operators opt OUT, matching the
suppress-polarity convention the other components already use
(`AP2_JANITOR_DISABLED` / `AP2_CRON_DISABLED` / `AP2_IDEATION_DISABLED`,
`[components.<name>] disabled = true`). Operator-filed posture change; no goal.md focus
anchor (filed `--skip-goal-alignment`).

Why now: the operator wants autonomous-by-default dispatch across their projects rather
than per-project opt-in, and the recently-fixed config-aware enablement makes a clean
polarity flip possible. This deliberately changes ap2's default safety posture from
operator-in-the-loop to autonomous-by-default; the change must be loud in the docs and
must not silently break existing deployments that set the old `AP2_AUTO_APPROVE` flag.

## Scope

- `ap2/components/auto_approve/manifest.py`: set `default_enabled=True` and convert to
  suppress-polarity. The kill-switch env flag becomes `AP2_AUTO_APPROVE_DISABLED`
  (matching the `AP2_<X>_DISABLED` convention); the `config_schema` carries a `disabled`
  key (bool, default `False` → component on) in place of `enabled`. `Manifest._enable_config_key`
  already maps `default_enabled=True` → the `disabled` key, and `Manifest.is_enabled`
  resolves it through the config-aware tiers, so the registry/status/gate wiring follows
  automatically — verify it does, don't re-implement it.
- The gate `_is_auto_approve_enabled` already delegates to `is_enabled`; confirm it now
  returns True under no env + no config (default on) and False under the `disabled`
  knob. Update the docstring/truthy-vocabulary references that name `AP2_AUTO_APPROVE`.
- **Back-compat (must-have):** an existing deployment that sets the legacy
  `AP2_AUTO_APPROVE` flag must not break. Honor an explicitly-set `AP2_AUTO_APPROVE`
  (both polarities: `=1` keeps on, `=0` opts out) as a transitional override of the new
  default, with a one-time deprecation note pointing at `AP2_AUTO_APPROVE_DISABLED` /
  `[components.auto_approve] disabled`. (This keeps the countdown daemon's pinned
  `AP2_AUTO_APPROVE=1` and any operator's `AP2_AUTO_APPROVE=0` working through the
  transition.)
- Reconcile the knob registries: `env_reload.HOT_RELOADABLE_KNOBS` (swap/add
  `AP2_AUTO_APPROVE_DISABLED`, keep the legacy name during the deprecation),
  `config.ENV_PERMITTED_KEYS` / the tier-2 flat read in `is_enabled`, `doctor.py`'s
  master-switch read, and the `ap2 status` components renderer +
  `automation_status.collect_auto_approve_state` so status shows on-by-default and the
  opt-out flag correctly.
- Docs: update `ap2/README.md`, `ap2/architecture.md`, and the operator skills
  (`ap2/skills/ap2-config`, `ap2-board-ops`, `ap2-failure-recovery`) + `ap2/AGENTS.md`
  where they describe auto_approve as opt-in / `AP2_AUTO_APPROVE` — they must state the
  new default-on posture and the opt-out knob, and flag it as a behavior change.
- Tests: update every auto_approve test that assumes default-off / `AP2_AUTO_APPROVE`
  opt-in to the new default-on / opt-out semantics (don't leave any failing).

## Design

- Adopt the existing suppress-polarity convention rather than inventing a new one: the
  component is on unless `disabled` (config) / `AP2_AUTO_APPROVE_DISABLED` (env) is
  truthy — identical in shape to janitor/cron/ideation.
- Keep the gate chain (tags / freeze-threshold / token caps / dry-run) UNCHANGED — those
  safety gates still run; this only changes the master on/off default.
- Deprecate, don't abruptly remove, the legacy `AP2_AUTO_APPROVE` flag — honor it if
  explicitly set so the flip is non-breaking, and document the migration.
- **Execution discipline.** Run verification in the FOREGROUND; do NOT
  `run_in_background` + poll. Iterate against targeted tests; the daemon verifier runs
  the full suite after you report. Keep tool calls bounded.

## Verification

- `uv run --extra dev pytest -q ap2/tests/test_auto_approve_default_on.py` — a new test asserts: with NO env override and NO config key, BOTH `default_registry().get("auto_approve").is_enabled(cfg=cfg)` AND the gate `_is_auto_approve_enabled(cfg)` return True (default on); setting `[components.auto_approve] disabled = true` (TOML bool) turns both off; and an explicitly-set legacy `AP2_AUTO_APPROVE=0` still opts out (back-compat).
- `uv run --extra dev pytest -q ap2/tests/ --ignore=ap2/tests/smoke` — the full suite stays green (all pre-existing auto_approve opt-in tests updated to the new default).
- `grep -q "default_enabled=True" ap2/components/auto_approve/manifest.py` — the manifest declares auto_approve enabled by default.
- `ap2/components/auto_approve/manifest.py` Prose: auto_approve is `default_enabled=True` (suppress-polarity), enabled unless the `disabled` config key / `AP2_AUTO_APPROVE_DISABLED` env flag is set, with the legacy `AP2_AUTO_APPROVE` honored as a deprecated transitional override; judge confirms via Read.
- `ap2/README.md` Prose: the docs state auto_approve is ON by default and document the opt-out knob, flagging the autonomous-by-default posture change; judge confirms via Read.

## Out of scope

- The auto_approve gate-chain POLICY (gate tags, freeze threshold, per-task / window
  token caps, dry-run) — unchanged; only the master enablement default flips.
- Other components' polarity or defaults.
- The countdown sandbox project's own config (operator-managed; after this ships +
  deploys, its pinned `AP2_AUTO_APPROVE=1` env hack becomes unnecessary).
- goal.md edits (fenced; operator-owned).

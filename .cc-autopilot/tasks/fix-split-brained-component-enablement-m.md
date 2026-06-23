# Fix split-brained component enablement: `Manifest.is_enabled` ignores config.toml, so `[components.auto_approve] enabled=true` never turns the component on (status/doctor/registry disagree with the gate)

Tags: #autopilot #config #registry #auto_approve #components #bug

## Goal

Make a component's enablement resolve from a SINGLE config-aware source of truth so
the registry layer (`ap2 status` `## Components`, `ap2 doctor`, `enabled_components()`)
agrees with the component's internal gate, and so `[components.<name>].enabled` /
`.disabled` in `config.toml` actually turns the component on/off. Today
`registry.Manifest.is_enabled(self, env=None)` (`ap2/registry.py:234`) reads ONLY the
env flag (`os.environ.get(self.env_flag, "")`) — it has no `cfg` parameter and cannot
see `config.toml`. Meanwhile the auto_approve GATE
(`components/auto_approve/impl.py` `_is_auto_approve_enabled(cfg)` /
`run_auto_approve_pass`) resolves via `cfg.get_component_value("auto_approve",
"enabled")`, whose precedence is sectioned-env → config.toml snapshot → default and
which IGNORES the flat `AP2_AUTO_APPROVE` (it is not in `ENV_PERMITTED_KEYS`). The two layers therefore read DISJOINT sources:
`[components.auto_approve] enabled=true` turns the gate on but leaves `is_enabled`
(status/doctor) off; `AP2_AUTO_APPROVE=1` flips status on but the gate ignores it.
Neither knob turns the whole feature on. Unify them. Operator-filed correctness fix;
no goal.md focus anchor (filed `--skip-goal-alignment`).

Why now: an operator set `[components.auto_approve] enabled = true` in a sandbox
project's `config.toml` and auto-approve never engaged — `ap2 status` kept printing
`auto_approve: off (AP2_AUTO_APPROVE=0)`. The documented config.toml-first enablement (tunables live in config.toml; env is
secrets/runtime-identity only) is silently non-functional for the on/off master switch because the registry
enablement predicate was never migrated off the raw env read.

## Scope

- `ap2/registry.py`: make enablement config-aware. `Manifest.is_enabled` (and the
  sibling `env_flag_description`, `Registry._is_enabled`, `Registry.enabled_components`)
  must resolve through the component's config key (`enabled` for require-polarity /
  `default_enabled=False` components like auto_approve; `disabled` for suppress-polarity
  / `default_enabled=True` components like janitor) using the same precedence as
  `Config.get_component_value`, while still honoring the documented env master flag.
  Thread a `cfg` (or a resolver) to these methods; `enabled_components(cfg=...)` already
  takes `cfg` (currently unused) — wire it through.
- Resolve the env/config split coherently so BOTH documented knobs work and the two
  layers can never disagree. Pick the canonical source (config.toml is the source of
  truth for behavioral tunables) and make env consistent with it —
  either re-permit the specific operational on/off env flags (`AP2_AUTO_APPROVE`,
  `AP2_<X>_DISABLED`) for the enablement read, or route them through the sectioned name.
  The invariant (pinned by tests below) is: env-flag-only and config-key-only each turn
  the component FULLY on, and `is_enabled` == the gate's view in every case.
- `ap2/env_reload.py`: reconcile `HOT_RELOADABLE_KNOBS` — `AP2_AUTO_APPROVE` is listed
  there but the gate ignores it; make hot-reload consistent with whatever
  canonical source the fix lands on (so a `config.toml` `enabled` edit hot-reloads into
  `components_config`, and/or the env flag remains live, with no dead/misleading entry).
- `ap2/doctor.py` (line ~186, `os.getenv("AP2_AUTO_APPROVE")`) and the `ap2 status`
  components renderer: read the unified resolution so both report the true state.
- Sweep the other components (janitor, cron, attention, communication, auto_unfreeze,
  mattermost, ideation) for the same `is_enabled`-vs-config split and fix uniformly —
  this is a generic registry defect, auto_approve is just where it surfaced.

## Design

- One resolver, every surface: the registry enablement predicate and the component
  gate must call the same config-aware resolution so a future polarity/source edit
  ripples through `ap2 status`, `ap2 doctor`, `enabled_components`, the briefing-
  validator registry filter, and the tick-hook gate together.
- Preserve the polarity convention (`registry.py` module docstring): `env_flag=None` →
  `default_enabled`; `default_enabled=True` → flag/`disabled` SUPPRESSES;
  `default_enabled=False` → flag/`enabled` REQUIRES.
- Back-compat: an operator who today enables via the env flag (or via config.toml) must
  keep working; this fix makes BOTH paths work end-to-end, it does not remove a knob.
- **Execution discipline.** Run verification in the FOREGROUND; do NOT
  `run_in_background` + poll. Iterate against the targeted new test; the daemon verifier
  runs the full suite after you report. Keep tool calls bounded.

## Verification

- `uv run --extra dev pytest -q ap2/tests/test_component_enable_unified.py` — a new test asserts, for auto_approve (require-polarity) and at least one suppress-polarity component (e.g. janitor): (a) enabling via the config.toml key alone makes BOTH `Manifest.is_enabled`/`enabled_components` AND `cfg.get_component_value(name, key)` report enabled; (b) enabling via the env master flag alone does the same; (c) `is_enabled` and the component-gate view never disagree across the env-only / config-only / neither / both matrix.
- `uv run --extra dev pytest -q ap2/tests/ --ignore=ap2/tests/smoke` — the full suite stays green (existing TB-319 / status-render / doctor tests updated, not left failing).
- `grep -q "get_component_value" ap2/registry.py` — the registry enablement path resolves through the config-aware value accessor (not a raw `os.environ` read only).
- `! grep -nI 'os.getenv("AP2_AUTO_APPROVE"\|environ.*AP2_AUTO_APPROVE' ap2/doctor.py` — `ap2 doctor` no longer reads the master switch via raw env (it uses the unified resolution); the `-I` skips binary artifacts.
- `ap2/registry.py` Prose: `Manifest.is_enabled` / `enabled_components` resolve enablement through the config-aware per-component value (config.toml `enabled`/`disabled` key + the documented env flag, one coherent precedence), so the registry/status/doctor view always matches the component's own gate; judge confirms via Read.

## Out of scope

- Changing WHICH components are enabled by default, or the polarity convention itself.
- The auto_approve gate-chain policy (tags / freeze threshold / token caps) — only the
  on/off enablement resolution is in scope.
- The packaging / fenced-deny-list tasks (TB-425 / TB-426 siblings).

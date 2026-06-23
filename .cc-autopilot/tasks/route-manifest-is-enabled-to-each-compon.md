# Route `Manifest.is_enabled` to each component's ACTUAL enablement config source: ideation is core-keyed (`[core] ideation_disabled`) but is_enabled reads `[components.ideation]`, so status disagrees with the gate

Tags: #autopilot #config #registry #ideation #components #bug #status-drift

## Goal

Make the registry/status enablement view match a component's own gate for components
whose enablement knob lives in `[core]` rather than `[components.<name>]`. Today
`Manifest.is_enabled(cfg=...)` (`ap2/registry.py`) resolves its config tier from the
component-scoped key only — `cfg.get_component_value(self.name, "enabled"/"disabled")`,
i.e. `[components.<name>]`. Ideation's enablement knob is NOT there: its component
`config_schema` is intentionally empty and the knob is the core key `ideation_disabled`
(in `CORE_CONFIG_SCHEMA`), read by the self-gate via
`cfg.get_core_value("ideation_disabled")`
(`components/ideation/impl.py:_ideation_disabled`). So setting `[core] ideation_disabled
= true` in `config.toml` stops the gate from ideating (correct) but leaves
`is_enabled("ideation")` / `ap2 status` / `ap2 doctor` reading the empty
`[components.ideation].disabled`, falling through to "enabled", so the status block
still prints `ideation: on`. The registry view disagrees with the component's gate.
Route `is_enabled`'s config tier to the component's true source so the registry view and
the gate read ONE signal. Operator-filed correctness fix; no goal.md focus anchor (filed
`--skip-goal-alignment`).

Why now: an audit of the recent component-enablement work found this residual gap — `ap2
status` would misreport ideation as on after a `[core] ideation_disabled = true` config
edit, exactly the misleading signal (status says one thing, the gate does another) that
cost a multi-step investigation for the auto_approve master switch. Closing it makes
"status matches the gate" hold for every component, not just the `[components.*]`-keyed
ones.

## Scope

- `ap2/registry.py`: make `Manifest.is_enabled`'s config tier consult the component's
  ACTUAL enablement config source. For a component whose enable/disable signal is
  core-keyed (ideation → `[core] ideation_disabled` via `get_core_value`), the config
  tier must read that core key, not `get_component_value(name, "disabled")`. The natural
  shape is letting the manifest DECLARE where its enablement knob lives (e.g. an optional
  `enable_core_key` / config-source field on `Manifest`, defaulting to the existing
  component-scoped `enabled`/`disabled` key); `is_enabled` branches on it. Implementer's
  call on the exact field, but the registry must not assume `[components.<name>]` for
  every component.
- `ap2/components/ideation/manifest.py`: declare ideation's enablement source as the core
  key `ideation_disabled` (suppress-polarity), so `is_enabled` resolves it.
- Keep the component-keyed path unchanged for components that already match
  (auto_unfreeze reads `[components.auto_unfreeze].disabled` — verify it still resolves
  identically) and for env-flag-only / always-on manifests (cron / janitor /
  communication / attention / mattermost have no divergent self-gate; `is_enabled` is
  already authoritative — leave them).
- Update the `ap2 status` components renderer / `env_flag_description` only as needed so
  the displayed state reflects the corrected resolution.

## Design

- One signal per component: `is_enabled` (status / doctor / enabled_components) and the
  component's own gate must read the SAME config key from the SAME cluster (core vs
  component). The existing unification established this for `[components.*]`-keyed
  components; this extends it to core-keyed ones.
- Preserve the existing tier order (sectioned env → flat master flag → config) and the
  polarity convention; only the config-tier KEY/CLUSTER lookup changes for core-keyed
  components.
- **Execution discipline.** Run verification in the FOREGROUND; do NOT
  `run_in_background` + poll. Iterate against the targeted new test; the daemon verifier
  runs the full suite after you report. Keep tool calls bounded.

## Verification

- `uv run --extra dev pytest -q ap2/tests/test_ideation_enable_source.py` — a new test asserts that with `[core] ideation_disabled = true` (the TOML-bool shape) BOTH `default_registry().get("ideation").is_enabled(cfg=cfg)` AND the gate `_ideation_disabled(cfg)` report disabled; with the knob unset/false BOTH report enabled; and a `[components.*]`-keyed control (auto_unfreeze) still resolves consistently between `is_enabled` and its gate.
- `uv run --extra dev pytest -q ap2/tests/ --ignore=ap2/tests/smoke` — the full suite stays green.
- `grep -q "get_core_value" ap2/registry.py` — `is_enabled` can resolve a core-keyed component's enablement from the core cluster (not only `get_component_value`).
- `ap2/registry.py` Prose: `Manifest.is_enabled` resolves each component's enablement from its declared source — the core key for ideation (`ideation_disabled`), the component key for `[components.*]`-keyed components — so `ap2 status` / `ap2 doctor` always agree with the component's own gate; judge confirms via Read.

## Out of scope

- The auto_approve enablement fixes (already shipped: the enablement source-unification +
  bool-coercion work) — this only closes the core-keyed (ideation) residual.
- Moving ideation's knob into `[components.ideation]` (it stays a core knob; this teaches
  `is_enabled` to read it where it lives).
- Any change to ideation BEHAVIOR / the gate itself — the gate is already correct; this
  fixes the registry/status view that diverges from it.

# Fix config.toml bool coercion in component enablement gates: TOML `enabled = true` becomes `"True"` and fails the lowercase truthy check, so the gate silently reads False

Tags: #autopilot #config #auto_approve #ideation #components #bug #truthy

## Goal

Make component on/off gates correctly interpret a `config.toml` boolean. Today
`_is_auto_approve_enabled(cfg)` (`ap2/components/auto_approve/impl.py:90-99`) does
`raw = str(cfg.get_component_value("auto_approve","enabled", default="") or "")` then
`return raw.strip() in ("1","true","yes")`. When `config.toml` sets
`[components.auto_approve] enabled = true`, `get_component_value` returns the Python
bool `True`; `True or ""` is `True`; `str(True)` is `"True"` (capital T); and
`"True"` is NOT in the lowercase set — so the gate reads **False** and auto-approve
silently no-ops even though the operator set the documented config key. The flat
`AP2_AUTO_APPROVE` env can't compensate (it's outside `ENV_PERMITTED_KEYS`, so
`get_component_value` ignores it for the gate), so the operator is left with no
working config-side lever. Coerce booleans correctly (and case-insensitively) in every
component truthy gate. Operator-filed correctness fix surfaced by a live sandbox
operator; sibling to the enablement-source-unification work and independently
landable; no goal.md focus anchor (filed `--skip-goal-alignment`).

Why now: a sandbox operator set `[components.auto_approve] enabled = true`, saw
`ap2 status` and dispatch do nothing, and traced it to this exact coercion — they had
to fall back to an in-process `AP2_COMPONENTS_AUTO_APPROVE_ENABLED=1` env var (which
doesn't persist across a daemon restart) because the committed config.toml lever is
non-functional. Auto-approve via config.toml is documented and is the only restart-
durable enablement home, so the bool path must work.

## Scope

- `ap2/components/auto_approve/impl.py`: fix `_is_auto_approve_enabled` so a TOML bool
  (`True`/`False`) from `get_component_value` resolves correctly — accept a real bool
  (short-circuit) AND normalize string forms case-insensitively (`"True"`, `"true"`,
  `"1"`, `"yes"`). Audit the sibling reads in this file that wrap
  `get_component_value` in `str(...)` for the same defect (e.g. `dry_run`).
- `ap2/components/ideation/impl.py:184`: same `raw.strip() in ("1","true","yes")`
  shape with no lowercasing — fix if it can receive a config.toml bool (same coercion
  treatment).
- Converge on ONE canonical truthy helper used by every component gate so the behavior
  can't drift again. Today there are at least five copies with three behaviors:
  `auto_unfreeze/impl.py:192` and `doctor.py:130` lowercase; `automation_status.py:102`
  bool-short-circuits; `auto_approve/impl.py:99` and `ideation/impl.py:184` do neither.
  Make them all bool-safe + case-insensitive (extract/share a single helper, or align
  each on the same logic).
- Preserve the existing accepted string forms and the unset→False default; this only
  ADDS correct bool handling and case-insensitivity, it does not change which values
  count as truthy.

## Design

- Minimal, behavior-preserving: a bool returns itself; a string normalizes via
  `.strip().lower()` against `{"1","true","yes"}`; anything else is falsy. This matches
  what `auto_unfreeze`/`doctor`/`automation_status` already do — the fix brings the
  laggards into line.
- This is value-interpretation correctness; it is orthogonal to which SOURCE wins
  (sectioned env vs config.toml vs flat env). It lands cleanly on top of the
  enablement-source work without depending on it.
- **Execution discipline.** Run verification in the FOREGROUND; do NOT
  `run_in_background` + poll. Iterate against the targeted new test; the daemon verifier
  runs the full suite after you report. Keep tool calls bounded.

## Verification

- `uv run --extra dev pytest -q ap2/tests/test_truthy_bool_coercion.py` — a new test asserts `_is_auto_approve_enabled(cfg)` returns True when `cfg.components_config["auto_approve"]["enabled"]` is the Python bool `True` (the TOML-parsed shape), False when it is `False`, and True for the string forms `"true"`, `"True"`, `"1"`, `"yes"`; plus the same bool/`"True"` case for the ideation gate helper.
- `uv run --extra dev pytest -q ap2/tests/ --ignore=ap2/tests/smoke` — the full suite stays green.
- `! grep -nIE 'str\(\s*cfg.get_component_value\("auto_approve", ?"enabled"' ap2/components/auto_approve/impl.py` — the auto_approve enabled gate no longer stringifies the value before a lowercase-only membership test (the `-I` skips binary artifacts).
- `ap2/components/auto_approve/impl.py` Prose: `_is_auto_approve_enabled` interprets a `config.toml` boolean `true` as enabled (bool short-circuit) and treats string values case-insensitively, so `[components.auto_approve] enabled = true` actually engages the gate; judge confirms via Read.

## Out of scope

- Unifying the enablement SOURCE between `Manifest.is_enabled` (env) and the gate
  (config) — that is the sibling enablement-source task; this task only fixes value
  coercion.
- Changing the truthy vocabulary (`1`/`true`/`yes`) or the unset→False default.
- The gate-chain policy (tags / freeze threshold / token caps).

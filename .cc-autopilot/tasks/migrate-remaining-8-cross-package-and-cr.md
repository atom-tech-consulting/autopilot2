## Goal

Current focus: structured config (env → TOML). Lift goal.md L398-399
progress signal "≥80% of source-side `os.environ.get('AP2_*')` calls
migrated to `cfg.<path>.<key>` reads" by swapping the remaining ~8
direct env reads outside the documented `_KNOBS_STAYING_ENV_ONLY`
12-factor exempt set + outside the `ap2/config.py` / `ap2/env_reload.py`
bootstrap path. Each target knob already has a `FLAT_TO_SECTIONED`
entry (so the sectioned home is decided) and a helper available
(`cfg.get_core_value` from TB-334 d4404ef or `cfg.get_component_value`
from TB-326 b3eba54).

Why now: TB-332 (f1a6176) + TB-334 (d4404ef) just landed the
auto_approve cross-package + core agent-runtime clusters. The
remaining ~8 reads are the last cleanly-migrable cross-package /
cross-component strays — `_KNOBS_STAYING_ENV_ONLY` (config_compat.py
L207-212) explicitly carves out the Mattermost-family + sandbox
identity knobs, and `config.py` / `env_reload.py` are the bootstrap
that CONSTRUCTS `cfg` (can't read from `cfg` to build `cfg`). Bundling
these 8 reads into one task closes the migration tail in a single
sweep before the residual list fragments into low-leverage one-offs;
the TB-330 "config_schema gained 2 keys to close a 1-vs-3 schema
mismatch" pattern (migration walk surfaces latent schema-doc gaps)
applies here too — expect tiny `Manifest.config_schema` extensions
on `focus_advance` / `auto_approve` if any of the target keys aren't
yet declared. Cheaper as one task than three since the shape repeats
verbatim per the proven pilots.

## Scope

Read-path swap at each of these call sites, preserving the
pre-migration default value EXACTLY:

- `ap2/web.py` L214 (`AP2_WEB_DISABLED`) → `cfg.get_core_value("web_disabled", default="")` — the L214-217 truthy check stays as-is on the helper's return value.
- `ap2/web.py` L226 (`AP2_WEB_PORT`) → `cfg.get_core_value("web_port", default="")` — preserve the `.strip()` semantics; `int()` cast stays at the call site.
- `ap2/goal.py` L419 (`AP2_FOCUS_ADVANCE_EMPTY_CYCLES`) → `cfg.get_component_value("focus_advance", "empty_cycles", default="")`.
- `ap2/goal.py` L446 (`AP2_FOCUS_AUTO_ADVANCE_DISABLED`) → `cfg.get_component_value("focus_advance", "auto_advance_disabled", default="")`.
- `ap2/doctor.py` L374 (`AP2_VERIFY_CMD`) → `cfg.get_core_value("verify_cmd", default="")`.
- `ap2/doctor.py` L375 (`AP2_VERIFY_TIMEOUT_S`) → `cfg.get_core_value("verify_timeout_s", default=DEFAULT_VERIFY_TIMEOUT_S)`.
- `ap2/ideation.py` L845 (`AP2_IDEATION_MAX_TURNS`) → `cfg.get_core_value("ideation_max_turns", default=IDEATION_MAX_TURNS_DEFAULT)` — the TB-334 straggler.
- `ap2/components/attention/__init__.py` L234 (`AP2_AUTO_APPROVE_COST_APPROACH_PCT`) → `cfg.get_component_value("auto_approve", "cost_approach_pct", default="")` — cross-COMPONENT read; if auto_approve's `Manifest.config_schema` doesn't yet declare `cost_approach_pct`, add it (TB-330 precedent).

Tests: a new `ap2/tests/test_tb336_axis5_tail_cfg_reads.py` paralleling `test_tb334_core_cfg_reads.py` — one assertion per migrated call site that the helper returns the env value when set, the default when unset, and that the sectioned env override (e.g. `AP2_CORE_WEB_PORT`) takes precedence over the flat one (`AP2_WEB_PORT`).

## Design

Helper invocation shape per the TB-327 `should_suppress(*, cfg: Config | None = None)` template: each migrated call site signature gains a `*, cfg: Config | None = None` kwarg with `if cfg is None: cfg = Config.from_env()` fallback. Callers that already thread `cfg` (daemon-loop sites) pass it; pre-daemon-init callers (CLI verbs, doctor probes) hit the fallback. TypeError-guard on kwarg passthrough preserves back-compat for any external callers — the same shape TB-327 / TB-328 already validated.

For the `attention` cross-component read: thread `cfg` through `_resolve_cost_approach_pct(...)` from its caller (already inside the attention component tick path which has `cfg` in scope). If `auto_approve.Manifest.config_schema` is missing `cost_approach_pct`, extend it with `ConfigKey(name="cost_approach_pct", type=str, default="", description=..., hot_reloadable=True)` and add a `[components.auto_approve.cost_approach_pct]` entry to the howto.md `## Config keys (TOML)` section so `test_every_config_key_documented` stays green.

## Verification

- `uv run pytest -q` — full suite passes.
- `uv run pytest -q ap2/tests/test_tb336_axis5_tail_cfg_reads.py` — new tail-migration test passes.
- `! grep -qE "os\.environ\.get\(.AP2_WEB_" ap2/web.py` — zero remaining direct env reads of AP2_WEB_ in web.py.
- `! grep -qE "os\.environ\.get\(.AP2_FOCUS_" ap2/goal.py` — zero remaining direct env reads of AP2_FOCUS_ in goal.py.
- `! grep -qE "os\.environ\.get\(.AP2_VERIFY_(CMD|TIMEOUT_S)" ap2/doctor.py` — zero remaining direct env reads of AP2_VERIFY_CMD / AP2_VERIFY_TIMEOUT_S in doctor.py.
- `! grep -qE "os\.environ\.get\(.AP2_IDEATION_MAX_TURNS" ap2/ideation.py` — zero remaining direct env reads of AP2_IDEATION_MAX_TURNS in ideation.py.
- `! grep -qE "os\.environ\.get\(.AP2_AUTO_APPROVE_COST_APPROACH_PCT" ap2/components/attention/__init__.py` — zero remaining cross-component direct read of auto_approve's cost-approach knob inside attention/.
- `grep -rE "get_core_value\(.web_(port|disabled)." ap2/web.py` — new resolved-config read path present.
- `grep -rE "get_component_value\(.focus_advance." ap2/goal.py` — new resolved-config read path present.
- `grep -rE "get_core_value\(.verify_(cmd|timeout_s)." ap2/doctor.py` — new resolved-config read path present.
- `ap2/components/attention/__init__.py` Prose: the file's `os.environ.get("AP2_AUTO_APPROVE_COST_APPROACH_PCT", "")` read at L234 is replaced with a `cfg.get_component_value("auto_approve", "cost_approach_pct", ...)` call; judge confirms via Read.
- `uv run python -m ap2 --project . status` exits 0 (sanity that the cfg-read swap didn't break the status reporter chain).

## Out of scope

- Mattermost-family + sandbox-identity knobs (`AP2_MM_*`, `AP2_MM_TEAM_ID`, `AP2_REAL_SDK`, `AP2_DIR`, OAuth) — documented as 12-factor exempts in `ap2/config_compat.py` `_KNOBS_STAYING_ENV_ONLY` (L207-212).
- `ap2/config.py` `Config.from_env` construction reads (L332-351) and `ap2/env_reload.py` hot-reload mirror (L303-343) — these CONSTRUCT cfg.
- Declaring `[core.*]` `ConfigKey` schema entries — separate task (axis 1 completion).
- Exempt-list enforcement test — separate task (progress signal 6 gate).

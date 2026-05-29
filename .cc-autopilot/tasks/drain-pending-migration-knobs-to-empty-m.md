## Goal

Current focus: structured config (env → TOML). Drain the documented
`_PENDING_MIGRATION_KNOBS` debt set in
`ap2/tests/test_tb338_env_only_cut_line.py` L133-145 to empty so the
TB-338 cut-line gate enforces "every `os.environ.get('AP2_*')` is
EITHER in the 12-factor exempt set OR in the config.py/env_reload.py
bootstrap" without an escape hatch. Closes goal.md L398's progress
signal "≥80% of source-side `os.environ.get('AP2_*')` calls migrated
to `cfg.<path>.<key>` reads" to the strictest reading (~100% minus
the exempt + bootstrap carve-outs) and tightens goal.md L401-403's
"clearly minimal" framing — the debt set, by construction, trends to
empty rather than accumulating.

Two reads remain: `AP2_VERIFY_JUDGE_EFFORT` at `ap2/verify.py` L588
and `AP2_STATUS_REPORT_EFFORT` at `ap2/status_report.py` L2028. Both
wrap a `cfg.get_core_value("agent_effort", default=...)` lookup in a
`per-site env > global cfg > per-site default` chain — the per-site
env's fallback is itself a cfg read, which is why TB-334 deferred
the migration (the prior `cfg.get_core_value` shape took a static
default, not a cfg-read fallback). The migration shape uses the
schema's empty-string default + a chained `or` at the read site
(same convention `agent_effort` itself uses — core_config_schema.py
L245-257 declares `default=""`), so no new helper is needed.

`config_compat.py::FLAT_TO_SECTIONED` already maps
`AP2_VERIFY_JUDGE_EFFORT → core.verify_judge_effort` and
`AP2_STATUS_REPORT_EFFORT → core.status_report_effort` (L105-106),
but `CORE_CONFIG_SCHEMA` doesn't declare those two keys (carve-out
documented at `core_config_schema.py` L14-20). Without schema
declarations, an operator who authors `[core] verify_judge_effort =
"low"` in `config.toml` would hit `ConfigSchemaError: unknown key`
at daemon-start (validate_config L302-310). Adding the two schema
entries makes the TOML write path work end-to-end.

Why now: the cut-line CI gate landed one cycle ago carrying the
2-entry debt set as a documented exception. Draining the debt set
NOW keeps the "clearly minimal" framing per goal.md L401-403 honest
— the set is intended to trend to empty, and pinning the last 2
residuals immediately after the gate lands is the cheapest possible
debt payoff. Deferring lets the carve-out normalize as "permanent"
rather than transitional. The migration shape is mechanical
(TB-326/336 template applies verbatim now that FLAT_TO_SECTIONED is
in place); this is the smallest possible task that takes the
structured-config focus from "in-progress" to
"exhausted-needs-operator".

## Scope

- Extend `ap2/core_config_schema.py::CORE_CONFIG_SCHEMA` with two
  new `ConfigKey` entries:
  - `verify_judge_effort` — `type=str`, `default=""`,
    `hot_reloadable=True`, description mirrors `agent_effort`'s
    pattern (per-site override of the global reasoning-effort label).
  - `status_report_effort` — same shape.
  Update the module docstring at L14-20 to drop these two from the
  "intentionally out of scope" carve-out list.

- Swap the two read sites:
  - `ap2/verify.py` L588-591: replace
    `effort = os.environ.get("AP2_VERIFY_JUDGE_EFFORT",
    cfg.get_core_value("agent_effort", default="high"))`
    with
    `effort = cfg.get_core_value("verify_judge_effort", default="")
    or cfg.get_core_value("agent_effort", default="high")`.
  - `ap2/status_report.py` L2028-2031: same shape with
    `status_report_effort` and `default="medium"`.
  Drop the inline TB-334 comment block now that the migration
  landed (or replace with a one-line TB-339 cross-reference).

- Remove the 2 entries from
  `ap2/tests/test_tb338_env_only_cut_line.py::_PENDING_MIGRATION_KNOBS`
  (L133-145). The stale-debt test
  (`test_pending_migration_knobs_still_referenced`) AND the
  cut-line test BOTH must stay green after the migration —
  removing entries from the set is correct because the underlying
  reads no longer exist. Also update the comment block at L133-138
  explaining the debt set: trim the deferring rationale and note
  the drained state with a TB-339 cross-reference.

- Extend `ap2/init.py::CONFIG_TEMPLATE` with the 2 new keys in the
  rendered `[core]` block (same shape as existing core keys —
  commented-out default + description).

- Extend `ap2/howto.md`'s `### [core]` section (L2418-2530) with
  2 new alphabetical entries: `core.status_report_effort` (between
  the existing `status_report_*` entries) and `core.verify_judge_effort`
  (between `verify_judge_max_turns` at L2518 and `verify_timeout_s`
  at L2522). Mirror the existing entry shape.

- Add a regression-pin test
  `ap2/tests/test_tb339_pending_migration_drained.py` covering:
  1. `_PENDING_MIGRATION_KNOBS` is now empty (deliberate pin so a
     future regression that adds debt back trips this gate, not just
     the cut-line gate).
  2. `verify_judge_effort` + `status_report_effort` appear in
     `CORE_CONFIG_SCHEMA`.
  3. Grep-absence: `os.environ.get("AP2_VERIFY_JUDGE_EFFORT"`
     and `os.environ.get("AP2_STATUS_REPORT_EFFORT"` are both 0
     hits across `ap2/` (excluding `ap2/tests/`).
  4. Per-site env precedence: with `AP2_VERIFY_JUDGE_EFFORT=low` in
     env, `cfg.get_core_value("verify_judge_effort")` returns `"low"`
     via the FLAT_TO_SECTIONED back-compat path (and same for
     `AP2_STATUS_REPORT_EFFORT`).
  5. Fallback chain: with neither per-site env nor TOML override,
     read at the call-site falls back to `agent_effort`.

## Design

- Two-knob migration follows the established axis-5 pilot + tail
  template verbatim. No new helpers introduced — the `or`-chain at
  the read site handles the cfg-read-fallback pattern that the prior
  core agent-runtime migration deferred (the prior
  `cfg.get_core_value(default=...)` signature needed a static
  default; the `or` chain accomplishes the same effect with two cfg
  reads).

- Schema defaults are empty-string (matching `agent_effort`'s
  convention at core_config_schema.py L245-257 — "Empty default =
  no extra_args sent"); the read-site `or` collapses the empty
  string to the global fallback. This preserves the exact
  precedence chain the env-read had: per-site override (`AP2_*` env
  OR `[core.<key>]` TOML) > global `agent_effort` cfg read >
  per-site hardcoded default ("high" for verify-judge, "medium" for
  status-report).

- The `_PENDING_MIGRATION_KNOBS` set going to `frozenset()` is the
  desired end-state. The stale-debt test
  (`test_pending_migration_knobs_still_referenced`) passes
  vacuously when the set is empty (no entries to check); the
  disjointness test passes by construction (empty ∩ anything =
  empty); the cut-line test passes because the 2 reads are gone.

- The new regression test pins the drained state. Without this
  pin, a future migration regression that re-introduced a direct
  env read could in principle add a new entry to
  `_PENDING_MIGRATION_KNOBS` to satisfy the cut-line gate; the
  emptiness pin prevents that workaround. Same defensive shape
  as the existing stale-entry detector, in the other direction.

## Verification

- `uv run pytest -q` — full suite passes.
- `uv run pytest -q ap2/tests/test_tb339_pending_migration_drained.py` — new regression pin passes.
- `uv run pytest -q ap2/tests/test_tb338_env_only_cut_line.py` — existing cut-line gate stays green with drained debt set.
- `uv run pytest -q ap2/tests/test_tb337_core_schema.py` — existing core-schema test stays green with 2 added keys.
- `uv run pytest -q ap2/tests/test_docs_drift.py` — docs-drift gate stays green (2 new keys documented in howto.md core block).
- `! grep -rE "os\.environ\.get..AP2_VERIFY_JUDGE_EFFORT" ap2/ --include=*.py` — zero direct reads of AP2_VERIFY_JUDGE_EFFORT remain across ap2/.
- `! grep -rE "os\.environ\.get..AP2_STATUS_REPORT_EFFORT" ap2/ --include=*.py` — zero direct reads of AP2_STATUS_REPORT_EFFORT remain across ap2/.
- `grep -nE "verify_judge_effort.*ConfigKey" ap2/core_config_schema.py` — new schema entry for verify_judge_effort declared.
- `grep -nE "status_report_effort.*ConfigKey" ap2/core_config_schema.py` — new schema entry for status_report_effort declared.
- `grep -nE "core\.verify_judge_effort" ap2/howto.md` — howto core block documents the new key.
- `grep -nE "core\.status_report_effort" ap2/howto.md` — howto core block documents the new key.
- `test -f ap2/tests/test_tb339_pending_migration_drained.py` — new regression test module exists.
- `ap2/tests/test_tb338_env_only_cut_line.py` Prose: the `_PENDING_MIGRATION_KNOBS` frozenset at L133 is now empty (`frozenset()` literal or equivalent); the comment block above it notes the drained state; judge confirms via Read.
- `ap2/verify.py` Prose: the line at L588 reads via `cfg.get_core_value("verify_judge_effort", ...)` with an `or cfg.get_core_value("agent_effort", ...)` fallback; no `os.environ.get("AP2_VERIFY_JUDGE_EFFORT", ...)` remains; judge confirms via Read.
- `ap2/status_report.py` Prose: the line at L2028 reads via `cfg.get_core_value("status_report_effort", ...)` with an `or cfg.get_core_value("agent_effort", ...)` fallback; no `os.environ.get("AP2_STATUS_REPORT_EFFORT", ...)` remains; judge confirms via Read.

## Out of scope

- Adding a fallback-callable form to `cfg.get_core_value`. The
  chained-`or` shape at the two read sites handles the
  cfg-read-fallback pattern directly without needing a new helper
  signature; introducing a helper extension for two call sites would
  be premature aggregation. Defer to a future task only if a third
  call-site with the same shape appears.

- Migrating any 12-factor exempt knob (Mattermost identity, OAuth,
  AP2_DIR, AP2_REAL_SDK). Those stay in
  `_KNOBS_STAYING_ENV_ONLY` by design.

- Schema-validating the empty-string default at daemon start. The
  schema's `type=str` allows `""`; the `or`-chain in the read site
  is what enforces "empty means fall through to global".

- Behavior change. Both call sites' EFFECTIVE effort value must
  stay identical to today's behavior at every state of the env /
  TOML / global-cfg precedence chain. Test bullets 4 + 5 pin this.
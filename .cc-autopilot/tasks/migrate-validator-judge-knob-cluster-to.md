## Goal

Long-tail cluster migration for axis (5) of the **Current focus:
structured config (env → TOML)** (goal.md L266 / L353-364). The
TB-326 pilot (b3eba54 + 60bdb1f) proved the per-component read-swap
template using `Config.get_component_value("auto_approve", <key>)`;
this task applies the same template to `validator_judge`. Per
FLAT_TO_SECTIONED in `ap2/config_compat.py` (TB-323, a50e686), the
validator_judge cluster owns 5 sectioned mappings under
`components.validator_judge`: `disabled` (from
`AP2_VALIDATOR_JUDGE_DISABLED`), `max_tokens` (from
`AP2_VALIDATOR_JUDGE_MAX_TOKENS`), `max_turns` (from
`AP2_VALIDATOR_JUDGE_MAX_TURNS`), `noisy_threshold` (from
`AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD`), `timeout_s` (from
`AP2_VALIDATOR_JUDGE_TIMEOUT_S`). TB-322 (e38bb38) already declared
the schema on `ap2/components/validator_judge/manifest.py`; this
task finishes the read-side swap.

Why now: TB-326's pilot template is proven and explicitly named this
cluster in its Out-of-scope list ("Migrating the other 6 component
clusters (attention, focus_advance, auto_unfreeze, mattermost,
validator_judge, janitor) — separate TB-N per cluster post-pilot").
Goal.md Progress signal at L398-399 ("≥80% of source-side
`os.environ.get('AP2_*')` calls migrated to `cfg.<path>.<key>` reads")
is currently at ~3/N migrated; each cluster moves the needle.

## Scope

- Migrate every `os.environ.get("AP2_VALIDATOR_JUDGE_...")` call
  site in `ap2/components/validator_judge/__init__.py` and
  `ap2/components/validator_judge/manifest.py` to read from the
  resolved config via `Config.get_component_value("validator_judge",
  <key>)` per the TB-326 pilot pattern. The 5 keys per
  FLAT_TO_SECTIONED: `disabled`, `max_tokens`, `max_turns`,
  `noisy_threshold`, `timeout_s`. Cross-check against the
  manifest's `config_schema` (TB-322) for any additional keys.
- Preserve existing env-override semantics — TB-323's
  `apply_env_overrides()` plumbs flat-`AP2_FOO` and
  sectioned-`AP2_COMPONENTS_VALIDATOR_JUDGE_FOO` into the resolved
  config; removing direct `os.environ.get` calls does not break
  shell-export overrides, and `env_deprecated` one-shot still fires
  on flat-knob use.
- New regression-pin test
  `ap2/tests/test_tb331_validator_judge_cfg_reads.py`: grep-walk
  asserts zero remaining `os.environ.get("AP2_VALIDATOR_JUDGE_...")`
  calls in `ap2/components/validator_judge/`; one behavioral test
  per migrated knob asserts the cfg read returns the same value the
  env read would have returned.
- The TB-316 validator-pipeline-as-list integration stays
  unchanged — the judge component reaches the cluster knobs through
  the cfg-side path automatically once the swap lands.

## Design

Adopt the TB-326 option-2 pattern verbatim:
`Config.get_component_value("validator_judge", "<key>")`. The
helper already exists on `Config` (landed in TB-326's commit
b3eba54); this task reuses it without redesign. Behavior
preservation contract: every test that exercises validator_judge
today (covering kill-switch, max-tokens, max-turns, noisy-threshold
pause-reason wiring, timeout-s SDK budgeting) passes without
modification.

If the migration walk surfaces a latent bug (as TB-326's did in
60bdb1f), the agent may close it in a follow-up commit on the same
task; that's expected and not a scope expansion.

## Verification

- `uv run pytest -q` — full suite passes (regression gate).
- `uv run pytest -q ap2/tests/test_tb331_validator_judge_cfg_reads.py`
  — new cluster test passes.
- `! grep -rqE "os\.environ\.get\(.AP2_VALIDATOR_JUDGE_" ap2/components/validator_judge/`
  — zero remaining direct env reads of AP2_VALIDATOR_JUDGE keys in
  the component body (passes iff the grep finds zero matches, per
  TB-270 absence-check convention).
- `grep -rE "get_component_value\(.validator_judge." ap2/components/validator_judge/`
  — the new resolved-config read path is present.
- `uv run python -m ap2 status --project .` exits 0 and the
  `## Components` block still renders `validator_judge` correctly
  (sanity check the cfg read path didn't break the status
  enumeration).

## Out of scope

- The other 4 component clusters (auto_unfreeze, attention,
  focus_advance, janitor) — each ships under its own TB-N this
  cycle batch.
- Core (non-component) knob cluster migration — separate scope.
- Removing keys from FLAT_TO_SECTIONED or
  `_KNOBS_STAYING_ENV_ONLY` — back-compat stays through the full
  migration arc.
- Changes to `env_deprecated` event semantics — TB-323's
  one-shot-per-process behavior preserved verbatim.
- The TB-316 validator-pipeline-as-list semantics — knob value-read
  path swaps but pipeline structure stays unchanged.

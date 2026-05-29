## Goal

Long-tail cluster migration for axis (5) of the **Current focus:
structured config (env → TOML)** (goal.md L266 / L353-364). The
TB-326 pilot (b3eba54 + 60bdb1f) proved the per-component read-swap
template using `Config.get_component_value("auto_approve", <key>)`;
this task applies the same template to `focus_advance`. Per
FLAT_TO_SECTIONED in `ap2/config_compat.py` (TB-323, a50e686), the
focus_advance cluster owns 2 sectioned mappings under
`components.focus_advance`: `disabled` (from
`AP2_FOCUS_AUTO_ADVANCE_DISABLED`) and `empty_cycles` (from
`AP2_FOCUS_ADVANCE_EMPTY_CYCLES`). TB-322 (e38bb38) already declared
the schema on `ap2/components/focus_advance/manifest.py`; this task
finishes the read-side swap.

Why now: TB-326's pilot template is proven and explicitly named this
cluster in its Out-of-scope list ("Migrating the other 6 component
clusters (attention, focus_advance, auto_unfreeze, mattermost,
validator_judge, janitor) — separate TB-N per cluster post-pilot").
Goal.md Progress signal at L398-399 ("≥80% of source-side
`os.environ.get('AP2_*')` calls migrated to `cfg.<path>.<key>` reads")
is currently at ~3/N migrated; each cluster moves the needle.

## Scope

- Migrate every `os.environ.get` call site reading the 2
  focus_advance knobs (`AP2_FOCUS_AUTO_ADVANCE_DISABLED`,
  `AP2_FOCUS_ADVANCE_EMPTY_CYCLES`) in
  `ap2/components/focus_advance/__init__.py` and
  `ap2/components/focus_advance/manifest.py` to read from the
  resolved config via `Config.get_component_value("focus_advance",
  <key>)` per the TB-326 pilot pattern. Cross-check against the
  manifest's `config_schema` (TB-322) for any additional keys.
- Preserve existing env-override semantics — TB-323's
  `apply_env_overrides()` plumbs flat-`AP2_FOO` and
  sectioned-`AP2_COMPONENTS_FOCUS_ADVANCE_FOO` into the resolved
  config; removing direct `os.environ.get` calls does not break
  shell-export overrides, and `env_deprecated` one-shot still fires
  on flat-knob use.
- New regression-pin test
  `ap2/tests/test_tb329_focus_advance_cfg_reads.py`: grep-walk
  asserts zero remaining direct env reads for the 2 focus_advance
  knobs in `ap2/components/focus_advance/`; one behavioral test per
  migrated knob asserts the cfg read returns the same value the env
  read would have returned.
- TB-292's cycle-grouped empty-cycles counter and TB-295's
  rewind-focus CLI verb stay unchanged — both reach the cluster
  knobs through the cfg-side path automatically once the swap
  lands.

## Design

Adopt the TB-326 option-2 pattern verbatim:
`Config.get_component_value("focus_advance", "<key>")`. The helper
already exists on `Config` (landed in TB-326's commit b3eba54);
this task reuses it without redesign. Behavior preservation
contract: every test that exercises focus_advance today (covering
kill-switch, empty-cycles threshold counting, focus_advanced event
emission, roadmap_complete halt) passes without modification.

If the migration walk surfaces a latent bug (as TB-326's did in
60bdb1f), the agent may close it in a follow-up commit on the same
task; that's expected and not a scope expansion.

## Verification

- `uv run pytest -q` — full suite passes (regression gate).
- `uv run pytest -q ap2/tests/test_tb329_focus_advance_cfg_reads.py`
  — new cluster test passes.
- `! grep -rqE "os\.environ\.get\(.AP2_FOCUS_(AUTO_ADVANCE_DISABLED|ADVANCE_EMPTY_CYCLES)" ap2/components/focus_advance/`
  — zero remaining direct env reads of the 2 focus_advance knobs
  in the component body (passes iff the grep finds zero matches,
  per TB-270 absence-check convention).
- `grep -rE "get_component_value\(.focus_advance." ap2/components/focus_advance/`
  — the new resolved-config read path is present.
- `uv run python -m ap2 status --project .` exits 0 and the
  `## Components` block still renders `focus_advance` correctly
  (sanity check the cfg read path didn't break the status
  enumeration).

## Out of scope

- The other 4 component clusters (auto_unfreeze, attention,
  janitor, validator_judge) — each ships under its own TB-N this
  cycle batch.
- Core (non-component) knob cluster migration — separate scope.
- Removing keys from FLAT_TO_SECTIONED or
  `_KNOBS_STAYING_ENV_ONLY` — back-compat stays through the full
  migration arc.
- Changes to `env_deprecated` event semantics — TB-323's
  one-shot-per-process behavior preserved verbatim.
- The TB-292 cycle-grouped counter or TB-295 rewind-focus CLI
  semantics — knob value-read path swaps but counter / verb
  behavior stays unchanged.

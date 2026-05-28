## Goal

Axis (2) of the **Current focus: structured config (env → TOML)**
focus (goal.md L317-329): land the env-var override layer +
back-compat map for the existing flat `AP2_*` env knobs + the
`env_deprecated` one-shot event vocabulary. Operators continue to
override per-shell-session via existing names; OSS users get a
clean migration path from old shell exports to the new sectioned
TOML/env shape, with one event per deprecated name per process
to make the migration discoverable in `events.jsonl`.

Why now: goal.md L325-329 articulates the delete-test explicitly —
"if not shipped, OSS users get a new file but can no longer
override per-shell-session; existing CI / sandbox setups break."
Today's `.cc-autopilot/env` is the source of every operator's
tunable overrides; the TOML migration must preserve that path or
every existing install breaks on upgrade. Lands in parallel with
TB-322 once TB-321's read paths exist (goal.md L378-381 — "(2)
and (3) are parallelizable once (1) lands").

## Scope

(1) `ap2/config_compat.py` — new module containing:
  - `FLAT_TO_SECTIONED: dict[str, str]` mapping every existing
    flat `AP2_*` env name to its sectioned counterpart (e.g.
    `"AP2_AUTO_APPROVE": "components.auto_approve.enabled"`,
    `"AP2_TICK_S": "core.tick_interval_s"`). The map covers
    every knob currently read by `os.environ.get("AP2_*")`
    across the codebase (110-call Grep audit 2026-05-28).
  - `_KNOBS_STAYING_ENV_ONLY: frozenset[str]` of true 12-factor
    knobs (Mattermost auth tokens, sandbox user identity,
    `AP2_DIR`, `AP2_REAL_SDK`, OAuth tokens) per goal.md
    L356-358 — these never migrate. Single comment block above
    the frozenset documents the cut-line for auditability per
    goal.md L361.

(2) Env-var override layer plumbed into `Config.from_toml`
    (landed by TB-321): for each `AP2_<SECTION>_<KEY>` env name
    present at daemon-start, override the loaded TOML value at
    the matching section path. Mapping rule:
    `AP2_COMPONENTS_AUTO_APPROVE_ENABLED` overrides
    `[components.auto_approve] enabled = ...`. Regex-anchored
    detection (`^AP2_[A-Z][A-Z0-9_]+$`).

(3) Back-compat shim: for each `AP2_<FLAT>` env name present at
    daemon-start AND listed in `FLAT_TO_SECTIONED`, override the
    matching sectioned path AND emit a one-shot `env_deprecated`
    event per process per knob (payload: flat-name, sectioned-
    replacement, process pid, ts).

(4) `ap2/events.py` — register the `env_deprecated` event type
    in the events vocabulary so the `/events` web page renders
    it correctly and `events.jsonl` consumers can filter on the
    type.

(5) `ap2/env_reload.py` — extend the file-watch trigger to also
    watch `.cc-autopilot/config.toml` mtime (today it watches
    `.cc-autopilot/env`); a config.toml edit triggers the same
    `HOT_RELOADABLE_KNOBS`-filtered reload pass on the next
    tick.

(6) New regression-pin module
    `ap2/tests/test_tb323_config_compat.py`:
    - Sectioned env override applies (e.g. set
      `AP2_COMPONENTS_AUTO_APPROVE_ENABLED=1`, assert the
      loaded config reflects that override).
    - Flat back-compat override applies AND fires exactly one
      `env_deprecated` event per knob per process (second read
      stays silent).
    - 12-factor knobs in `_KNOBS_STAYING_ENV_ONLY` don't fire
      `env_deprecated` even when present in env.
    - `env_reload`'s config.toml mtime trick triggers
      HOT_RELOADABLE reload on file change.
    - Every `AP2_*` name currently in `_TEMPLATE_EXEMPT_KNOBS`
      (TB-305, `ap2/init.py`) either appears in
      `FLAT_TO_SECTIONED` or in `_KNOBS_STAYING_ENV_ONLY` (no
      leakage — every existing knob gets a documented migration
      path or an explicit 12-factor exemption).

## Design

`FLAT_TO_SECTIONED` is the operator-facing contract: anything in
this map gets a back-compat read path with a deprecation event;
anything in `_KNOBS_STAYING_ENV_ONLY` is documented-permanent
env-only. The two sets partition the existing `AP2_*` namespace;
the per-knob test enforces the partition is total against
`_TEMPLATE_EXEMPT_KNOBS` (TB-305's source-of-truth set).

`env_deprecated` event payload shape: `{flat, sectioned,
process_pid, ts}`. Emission is per-process via a module-level
`_emitted_once: set[str]` guarded by a lock — same shape
`events.jsonl` already uses for one-shot events (mirror
`watchdog._emitted_attention_keys` accounting).

The override layer in `Config.from_toml`: after TOML parse + before
returning the `Config` dataclass, iterate `os.environ` for each
sectioned `AP2_<SECTION>_<KEY>` shape (regex-anchored) AND for
each flat key in `FLAT_TO_SECTIONED` (set membership); apply
overrides; emit `env_deprecated` for each flat-key hit; assemble
the final dataclass.

Precedence order (documented as a one-line comment in
`config_compat.py`): sectioned env > flat env (back-compat) >
TOML file > in-source defaults. Same shape as today's "shell
export wins over `.cc-autopilot/env`" rule, extended one
precedence level lower.

`env_reload.py` extension: today the helper watches
`.cc-autopilot/env` mtime and re-applies HOT_RELOADABLE_KNOBS on
the next tick when the file changes. Add a parallel watch on
`.cc-autopilot/config.toml` so an operator editing the TOML
gets the same hot-reload behavior for any HOT_RELOADABLE-flagged
key.

## Verification

- `uv run pytest -q ap2/tests/` — full suite passes after the
  compat layer + override plumbing + event registration land.
- `uv run pytest -q ap2/tests/test_tb323_config_compat.py` —
  new regression-pin module passes.
- `test -f ap2/config_compat.py` — new module exists.
- `grep -q "FLAT_TO_SECTIONED" ap2/config_compat.py` —
  back-compat map declared.
- `grep -q "_KNOBS_STAYING_ENV_ONLY" ap2/config_compat.py` —
  12-factor exemption set declared.
- `grep -q "env_deprecated" ap2/events.py` — event type
  registered in the vocabulary.
- `grep -q "config.toml" ap2/env_reload.py` — env_reload
  watches the TOML file for hot-reload.
- `! grep -qE "^from ap2\.components" ap2/config_compat.py` —
  back-compat layer avoids static component imports; registry
  walk is the cross-reference path (TB-311 import-direction
  gate parity).
- `ap2/config_compat.py` Prose: `FLAT_TO_SECTIONED` covers
  every `AP2_*` knob currently read in source EXCEPT those
  listed in `_KNOBS_STAYING_ENV_ONLY` (the partition is total
  against `_TEMPLATE_EXEMPT_KNOBS`); judge confirms via Read of
  the module body and the regression-pin test that asserts the
  partition.
- `ap2/config_compat.py` Prose: `env_deprecated` event fires
  exactly once per process per flat knob on first read of a
  flat name with a sectioned counterpart; second read stays
  silent; judge confirms via Read of the emission helper and
  the test that pins single-fire semantics.

## Out of scope

- `Config.from_toml` itself (TB-321 axis 1 — this TB plumbs
  into it but doesn't ship the constructor).
- Per-component `config_schema` declarations (TB-322 axis 3).
- Renaming any existing `AP2_*` knob name (back-compat shim
  preserves every name verbatim).
- Deletion of any existing knob — every name either migrates
  via `FLAT_TO_SECTIONED` or stays env-only per the partition.
- `ap2 config list / get / set / validate` CLI (axis 4).
- Per-cluster migration of `os.environ.get` call sites (axis
  5).
- TB-305-sibling docs-drift gate for config-key documentation
  (axis 6).
- Writing `.cc-autopilot/config.toml` from `ap2 init` (axis 6
  fresh-init concern).

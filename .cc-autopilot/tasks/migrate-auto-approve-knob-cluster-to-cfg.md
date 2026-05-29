## Goal

Pilot the per-cluster knob migration for axis (5) of the
**Current focus: structured config (env → TOML)** (goal.md L266
/ L353-364). auto_approve is the natural pilot cluster:
FLAT_TO_SECTIONED (TB-323, a50e686 L117-130) declares 9 sectioned
mappings under `components.auto_approve` — the largest single
cluster — and the component is operator-facing so the read-path
swap is independently verifiable via `ap2 status`. Goal.md's
Progress signal at L398-399 says "≥80% of source-side
`os.environ.get("AP2_*")` calls migrated to `cfg.<path>.<key>`
reads"; today the migrated count is 0/N — this pilot ships the
first cluster + the template every follow-up cluster reuses.

Why now: TB-321 + TB-322 + TB-323 landed the full foundation
(parser, schemas, override layer) this cycle. The 0% migration
count is currently the largest gap to the Progress signal at
L398-399. Pilot-first is the right shape — once the pattern
proves out (test fixture, kill-switch interaction, env-only knob
handling, `env_deprecated` interplay), the remaining 6 clusters
(attention, focus_advance, auto_unfreeze, mattermost,
validator_judge, janitor, core) become near-identical follow-up
TB-Ns. Without the pilot, queueing all 6 clusters now risks
6 identical fixups if a template gap surfaces.

## Scope

- Migrate every `os.environ.get("AP2_AUTO_APPROVE_...")` call
  site in `ap2/components/auto_approve/__init__.py` and
  `ap2/components/auto_approve/manifest.py` to read from the
  resolved `cfg.components_config` (or equivalent accessor —
  author chooses the ergonomic shape). The 9 keys per
  FLAT_TO_SECTIONED: `enabled`, `dry_run`, `gate_tags`,
  `freeze_threshold`, `per_task_token_cap`, `window_token_cap`,
  `noisy_pause_disabled`, `cost_approach_pct` (+ any others
  surfaced by the migration walk; cross-check against the
  manifest's `config_schema` from TB-322).
- Preserve the existing env-override semantics — TB-323's
  `apply_env_overrides()` already plumbs flat-AP2_FOO and
  sectioned-AP2_COMPONENTS_AUTO_APPROVE_FOO into the resolved
  config, so removing the direct `os.environ.get` calls does not
  observable-break shell-export overrides; the
  `env_deprecated` one-shot still fires on flat-knob use.
- `daemon._tick` inline gate logic stays in place if it reads
  the same knobs; either migrate it here OR keep direct env
  reads with a one-line comment naming TB-318's deferral
  (author judgment; default to migrating in-place if
  test-suite stays green).
- New regression-pin `ap2/tests/test_tb326_auto_approve_cfg_reads.py`:
  Grep-walk asserts zero remaining `os.environ.get("AP2_AUTO_APPROVE_...")`
  calls in `ap2/components/auto_approve/`; one behavioral test
  per migrated knob asserts the cfg read path returns the same
  value the env path returned.

## Design

Resolved-config access pattern: `cfg.components_config` is
already populated by `Config.from_toml` per TB-321 / TB-323;
this task standardizes the read shape. Candidate shapes (author
picks ONE and documents in the manifest):

1. Dict access: `cfg.components_config["components.auto_approve.dry_run"]`
2. Helper: `cfg.get_component_value("auto_approve", "dry_run")`
3. Per-component dataclass: `cfg.components.auto_approve.dry_run`
   (requires a per-component config-instance synthesized at load
   time from the schema)

Option 2 is the lightest-touch incremental shape; option 3 is
the long-term ergonomic shape. Pilot picks the one that lets
the remaining 6 clusters reuse the pattern with minimal
boilerplate; if option 3 is chosen, the synthesis helper lives
in `config_loader.py` so every cluster benefits.

Behavior preservation contract: every test that exercises
auto_approve today (covering kill-switch, dry-run, token caps,
freeze-threshold, gate-tag interaction, cost-approach) passes
without modification. The migration is a pure read-path swap —
no observable behavior change, no event-shape change, no new
config keys.

## Verification

- `uv run pytest -q` — full suite passes (regression gate).
- `uv run pytest -q ap2/tests/test_tb326_auto_approve_cfg_reads.py`
  — new pilot test passes.
- `! grep -rqE "os\.environ\.get\(.AP2_AUTO_APPROVE_" ap2/components/auto_approve/`
  — zero remaining direct env reads of AP2_AUTO_APPROVE keys in
  the component body (passes iff the grep finds zero matches,
  per TB-270 absence-check convention).
- `uv run python -m ap2 status --project .` exits 0 and the
  `## Components` block still renders `auto_approve` correctly
  (sanity check the cfg read path didn't break the status
  enumeration).
- `uv run pytest -q ap2/tests/test_tb318_auto_approve_migration.py`
  — existing auto_approve migration test passes unchanged.
- `grep -rE "cfg\.components_config|cfg\.get_component_value|cfg\.components\.auto_approve" ap2/components/auto_approve/`
  — the new resolved-config read path is present.
- `ap2/components/auto_approve/manifest.py` Prose: the manifest
  documents (docstring or top-of-file comment) which of the
  three resolved-config access shapes was chosen for this pilot
  + cites the rationale, so the follow-up cluster migrations
  can adopt the same shape; SDK judge confirms via Read.

## Out of scope

- Migrating the other 6 component clusters (attention,
  focus_advance, auto_unfreeze, mattermost, validator_judge,
  janitor) — separate TB-N per cluster post-pilot.
- Migrating core (non-component) knobs (AP2_AGENT_MODEL,
  AP2_AGENT_EFFORT, AP2_TICK_SECONDS, etc.) — separate cluster
  scope.
- Removing keys from FLAT_TO_SECTIONED or
  `_KNOBS_STAYING_ENV_ONLY` — back-compat stays through the
  full migration arc; pruning is a post-arc cleanup.
- `daemon._tick` inline auto_approve gate refactor (TB-318
  Out-of-scope clause — gate extraction is a deeper refactor
  with observable-behavior risk and is explicitly deferred).
- Changes to `env_deprecated` event semantics — TB-323's
  one-shot-per-process behavior is preserved verbatim.
## Attempts

### 2026-05-29 — verification_failed
(no summary)
- **kind:** per_task
- **failed_criteria:** [fail] `uv run python -m ap2 status --project .` exits 0 and the`## Components` block still renders `auto_approve` correctly(sa; [fail] `uv run pytest -q ap2/tests/test_tb318_auto_approve_migration.py`— existing auto_approve migration test passes unchanged
- **Debug dumps:** `prompt: .cc-autopilot/debug/20260529T005835Z-TB-326.prompt.md`, `stream: .cc-autopilot/debug/20260529T005835Z-TB-326.stream.jsonl`, `messages: .cc-autopilot/debug/20260529T005835Z-TB-326.messages.jsonl`

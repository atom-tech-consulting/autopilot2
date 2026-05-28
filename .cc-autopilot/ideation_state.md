# Ideation State

_Last updated: 2026-05-28T21:57:00Z by ideation cron_

## Mission alignment

Cycle entry: board 0A / 0R / 0B / 0P / 192C / 0F. Operator extended
goal.md at 2026-05-28T20:33:04Z with a new focus, **"structured
config (env → TOML)"**, then ran `rewind-focus` to reset the empty-
cycles counter (the prior focus auto-exhausted as soon as goal.md
was extended). The just-shipped component-refactor focus
(TB-309 → TB-320, all six named axes + the `ap2 status` L235-237
Progress signal landed) closed cleanly: every autonomous behavior
now lives under `ap2/components/<name>/` with manifest-declared
hook points (TB-318 final migration, 548e667). Most recent Completes
considered: TB-318 (axis-5 auto_approve migration, 548e667),
TB-319 (`ap2 status` enumerates components, ce55765), TB-320
(env_flag wiring on the last 3 manifests + AP2_AUTO_UNFREEZE_DISABLED
kill switch, e61ecc9). Backlog is empty and a fresh 6-axis roadmap
is on the table — this cycle re-derives proposals from scratch
against goal.md L266-403 (the new focus body).

## Current focus assessment

- **Current focus: structured config (env → TOML)**
  - Progress so far:
    - Zero TB-Ns shipped against this focus yet (added 2026-05-28
      via operator `update_goal` op at 20:33:04Z; rewind_focus
      at 20:33:50Z reset the empty-cycles counter so the daemon
      doesn't auto-advance on entry).
    - Adjacent ground-truth shipped: TB-309 left the registry +
      `Manifest` dataclass (`ap2/registry.py`) as the natural home
      for the new `config_schema` field per goal.md L335-340; TB-305
      shipped the env-template docs-drift gate
      (`test_every_env_knob_in_template_or_exempt`,
      `_TEMPLATE_EXEMPT_KNOBS` frozenset with 38 entries) that the
      axis-6 config-schema sibling will mirror.
  - Gaps:
    - **Axis (1) prerequisite missing** (goal.md L304-315): no
      `ap2/config_loader.py` or `Config.from_toml(path)`
      constructor exists; the daemon still loads exclusively via
      `Config.load()` reading `os.environ.get("AP2_*", default)`
      at 9 sites in `ap2/config.py` (110 `AP2_*` env reads across
      30 files — Grep audit 2026-05-28). Without axis (1) every
      downstream axis has nothing to read against.
    - **Axis (2) back-compat layer missing** (goal.md L317-329):
      no `ap2/config_compat.py` mapping the existing flat
      `AP2_*` names to the TOML-section overrides; no
      `env_deprecated` one-shot event vocabulary registered in
      `ap2/events.py`. Without this, OSS users get a new file
      but every existing shell-export / CI override breaks.
    - **Axis (3) per-component schema declarations missing**
      (goal.md L331-340): the 7 existing `Manifest` instances
      (janitor, validator_judge, mattermost, attention,
      focus_advance, auto_unfreeze, auto_approve) carry no
      `config_schema` field; the `Manifest` dataclass at
      `ap2/registry.py` L88-105 has no slot for it yet either.
      A scaffold (field + janitor canary) + per-component fill-in
      both need to land.
    - **Axes (4) CLI surface, (5) ~52-knob migration, (6) docs-
      drift sibling**: all blocked on (1)-(3); not ideation-
      proposable this cycle. (5) is explicitly a per-cluster
      long-tail per goal.md L353-364.
  - Status: `in-progress`
  - Reasoning: fresh focus, zero TB-Ns shipped, prerequisite
    structural slice (axis 1) is the unambiguous next step;
    parallelizable follow-ups (axes 2 + 3) have a natural shape
    once axis 1 lands.

## Non-goal risk check

None. All three proposed tasks sit squarely in the new focus's
axes (1)-(3) per goal.md L304-340. The previously-shipped
behavior surface stays bit-identical (axis 1 / 2 are pure
add-then-parallel-path; axis 3 is dataclass-field additions
+ registry-walked validation). No drift into goal.md L405-447
Non-goals (no multi-tenancy, no goal.md auto-rotation, no
API-stability commitments on `ap2/core/`, no behavior removal
during component extraction).

## Considered & deferred this cycle

- **Axis (4) CLI surface (`ap2 config list / get / set / validate`)**:
  natural follow-up to TB-321 (axis 1), but the validate / list
  verbs need the schema-registry walk that doesn't exist until
  TB-321 ships. Defer to a post-TB-321 cycle. Re-rank once axis 1
  is in HEAD; until then the daemon-startup-validator covers the
  validation surface and the toml file is operator-readable
  directly.
- **Axis (5) per-knob migration (one TB-N per cluster)**: the
  long-tail body of the focus per goal.md L353-364. Defer until
  axes (1) + (3) ship — without `cfg.<path>.<key>` read paths
  and per-component `config_schema` declarations, every
  migration task would be a stub. Each cluster (auto_approve
  knobs, attention knobs, etc.) is a clean ~30-line TB-N once
  the foundation exists.
- **Axis (6) docs-drift gate sibling (TB-305 sibling for
  config-key documentation)**: cheap to write but premature —
  with zero schema keys declared and zero knobs migrated, the
  gate would pass vacuously. Defer until axis (3) fills in the
  first 2-3 component schemas so the gate has surface to assert
  against.
- **Recurring rejection-pattern check (carried, re-justified)**:
  operator vetoes TB-185/184 (ap2-meta-polish unconnected to
  focus), TB-175 (premature aggregation), TB-231
  (symptom-patching), TB-240 (validator whack-a-mole). None of
  the three proposed tasks fit those shapes — they're direct
  build-out of operator-authored goal.md L266-403 axes (1)-(3)
  with explicit delete-test alignment. Pattern carried so
  future cycles re-verify alignment as the foundation matures.

## Cycle observations

- Prior cycle's observation about manifest-internal-switch design
  polarity being double-anchored (in manifest docstrings + TB-320
  Out-of-scope) has shipped to current state and no longer
  informs reasoning. Dropped.
- New observation worth carrying once: the 110-call sweep of
  `os.environ.get("AP2_*")` across 30 files (Grep,
  2026-05-28T21:55Z) is the size estimate for axis (5)'s
  migration tail; informs cluster-grouping decisions in future
  cycles. Carry for one cycle, then drop once axis (5) starts
  shipping.

## Decisions needed from operator

(none — fresh focus is well-specified at goal.md L266-403 with
explicit axis ordering, delete-tests, and Progress signals; no
narrative-judgment ambiguity ideation is uniquely positioned to
surface this cycle. The three proposed tasks below are direct
build-out of operator-authored axes (1)-(3); operator approval
via `ap2 approve TB-321`/`TB-322`/`TB-323` is the standard
review-gate path.)

## Proposals this cycle

- TB-321 (axis 1): TOML config schema + parser + validator +
  `Config.from_toml` + `Manifest.config_schema` dataclass field
  + janitor canary declaration (single end-to-end vertical
  slice).
- TB-322 (axis 3): walk the remaining 6 component manifests and
  fill in their `config_schema` declarations; registry's
  startup-validator (from TB-321) consumes them. `@blocked:TB-321`.
- TB-323 (axis 2): env-var override layer + `config_compat.py`
  back-compat map for the ~52 flat `AP2_*` names + one-shot
  `env_deprecated` event vocabulary. `@blocked:TB-321`.
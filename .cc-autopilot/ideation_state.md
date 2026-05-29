# Ideation State

_Last updated: 2026-05-29T04:38:18Z by ideation cron_

## Mission alignment

Cycle entry: board 0A / 0R / 1B / 0P / 202C / 0F. Of last cycle's 5
per-component proposals (TB-327..331), 4 landed (TB-327 auto_unfreeze
48ab4a8, TB-328 attention 980da5e, TB-329 focus_advance 17deb25,
TB-330 janitor a25507f); TB-331 validator_judge is still the lone
Backlog row, dispatch imminent. The TB-326 pilot template
(`Config.get_component_value("<name>", <key>)` + `cfg`-kwarg-with-
TypeError-guard back-compat on helpers) carried verbatim across all
four landed clusters with zero retry friction. The remaining
slack against the L398-399 progress signal ("≥80% of source-side
`os.environ.get('AP2_*')` calls migrated to `cfg.<path>.<key>`")
lives in TWO surfaces the per-component briefs explicitly scoped out:
(a) cross-package consumers of the same per-component knobs that sit
OUTSIDE `ap2/components/<name>/` and (b) the ~20 core
(non-component) knobs.

## Current focus assessment

- **Current focus: structured config (env → TOML)**
  - Progress so far:
    - Axis 1 shipped (TB-321, f5b0f0c): parser, `ConfigKey`, validator.
    - Axis 2 shipped (TB-323, a50e686): FLAT_TO_SECTIONED (62 entries)
      + sectioned-env > flat-env > TOML precedence + one-shot
      `env_deprecated` + TOML mtime hot-reload.
    - Axis 3 shipped (TB-322, e38bb38): `config_schema` declared on
      all 7 component manifests; 25-entry union pinned.
    - Axis 4 shipped (TB-324, bf4168d + 2ebe1a6): `ap2 config list /
      get / set / validate` CLI.
    - Axis 6 shipped (TB-325, 2eb899c): `CONFIG_TEMPLATE` +
      `test_every_config_key_documented`.
    - Axis 5 component-BODY tail shipped (TB-326 b3eba54 / TB-327
      48ab4a8 / TB-328 980da5e / TB-329 17deb25 / TB-330 a25507f);
      TB-331 in Backlog closes the 5th of 5 component clusters.
  - Gaps:
    - **Cross-package readers of per-component knobs** (goal.md
      L398-399). `AP2_AUTO_APPROVE*` / `AP2_AUTO_UNFREEZE_*` /
      `AP2_VALIDATOR_JUDGE_*` are still read by direct
      `os.environ.get` calls in `ap2/automation_status.py`,
      `ap2/board_edits.py`, `ap2/operator_queue.py`, `ap2/doctor.py`,
      `ap2/ideation.py`, `ap2/cli_daemon.py`, `ap2/tests/conftest.py`.
      `grep -rn 'os\.environ\.get(\s*['\''\"']AP2_' ap2/` reports 70
      reads outside `ap2/components/` versus 11 inside — the bulk of
      the residual count is consumers that the per-component briefs
      explicitly scoped to component bodies only. Each of these
      consumers can adopt `cfg.get_component_value("<name>", <key>)`
      with the same `cfg`-kwarg-+-TypeError-guard back-compat shape
      TB-327's `should_suppress` / TB-328's analogues already use.
    - **Core (non-component) cluster — no helper yet + ~12-15 reads
      still direct** (goal.md L353-364 "auto_approve, auto_unfreeze,
      attention, etc.", schema sectioned `[core.*]` per L308). TB-323
      mapped `AP2_AGENT_MODEL` / `AP2_AGENT_EFFORT` /
      `AP2_TASK_MAX_TURNS` / `AP2_CONTROL_MAX_TURNS` /
      `AP2_VERIFY_JUDGE_MAX_TURNS` / `AP2_IDEATION_*` / `AP2_WEB_*`
      to `core.<key>` paths in FLAT_TO_SECTIONED (`ap2/config_compat.py`
      L100-115), but no `Config.get_core_value` sibling to
      `get_component_value` exists yet, and the readers in
      `ap2/daemon.py` (L223,226,227,775,868,903), `ap2/verify.py`
      (L564,573,575), `ap2/status_report.py` (L2024),
      `ap2/components/janitor/__init__.py` (L214,789) still call
      `os.environ.get` directly. The pilot proved adding a helper +
      flipping reads keeps blast radius tight; the same shape transfers.
    - **Ideation cluster** (subset of core but distinct shape).
      `ap2/ideation.py` reads `AP2_IDEATION_COOLDOWN_S` (L566),
      `AP2_IDEATION_TRIGGER_TASK_COUNT` (L584), `AP2_IDEATION_MAX_TURNS`
      (L789), `AP2_IDEATION_DISABLED` (L929); `ap2/ideation_scrub.py`
      reads `AP2_IDEATION_SCRUB_MODEL` (L166). All mapped to `core.*`
      in FLAT_TO_SECTIONED but not yet read via cfg.
  - Status: `in-progress`

## Non-goal risk check

None. All 4 proposals continue the read-path swap that L406-410
explicitly green-lights ("does this migrate a previously-env-only
knob into the config schema without losing back-compat?"). No env
renames, no API stability commitments, no behavior changes — same
TB-326 pilot template across the board.

## Considered & deferred this cycle

- **`ap2/howto.md ## Configuration knobs` tree-render rewrite**
  (goal.md L366-376 axis 6). TB-325 shipped the
  `test_every_config_key_documented` gate but the howto section is
  still a flat env-var list, not a tree-of-paths render. Deferred
  one cycle — the cross-package + core-helper migrations have
  higher progress-signal leverage this cycle and the docs rewrite
  benefits from migration stabilization first.
- **`_KNOBS_STAYING_ENV_ONLY` curation pass**. The 12-factor exempt
  list in `config_compat.py` is currently a hand-curated dozen. A single-shot audit can
  re-verify the cut line. Deferred — premature before the migrations
  expose the remaining true-env-only set.
- **Cross-component readers BUNDLED into one task vs split into
  three**. Considered a single "migrate all cross-package
  AP2_AUTO_APPROVE/UNFREEZE/JUDGE reads" task. Rejected: 24-28
  call sites across 7 files in one task hits the "scope-too-large"
  failure mode the failure-review heuristic flags (TB-78-stoch
  anti-pattern); splitting by upstream cluster keeps each task's
  blast radius scoped and verifiable.
- **Recurring rejection-pattern check (carried, re-justified)**:
  operator vetoes TB-185/184 (utility unaligned with focus / parallel
  surface eroding goal.md authority), TB-175 (premature aggregation),
  TB-231/240 (symptom-patching / validator whack-a-mole). None of the
  4 proposals match — each is goal.md axis-5 build-out, named
  delete-test, proven template. Pattern carried so future cycles
  re-verify alignment as the focus drains.

## Cycle observations

- TB-326's "migration walk surfaces latent bugs" pattern recurred on
  TB-330 (manifest config_schema gained 2 keys to close a 1-vs-3
  schema mismatch) — confirms the migration walk has diagnostic
  value; expect the same on the cross-package readers since
  automation_status.py / doctor.py have higher branching density
  than the per-component bodies. Carry into briefings as expected
  side-effect (no scope change).

## Decisions needed from operator

(none — the 4 proposed tasks are direct axis-5 build-out closing
the cross-package + core remaining tail with the proven TB-326
helper-pilot template. Operator approval via `ap2 approve TB-N` is
the standard review-gate path; auto-approve will likely fire for
these per the `#axis-5` `#migration` tag pattern that auto-approved
TB-326..331.)

## Proposals this cycle

- TB-332 (axis 5 — cross-package `auto_approve` reads): migrate
  `AP2_AUTO_APPROVE*` reads in `automation_status.py`, `board_edits.py`,
  `operator_queue.py`, `doctor.py`, `ideation.py`, `cli_daemon.py`,
  `tests/conftest.py` to `cfg.get_component_value("auto_approve", <k>)`.
- TB-333 (axis 5 — cross-package `auto_unfreeze` + `validator_judge`
  reads): migrate `AP2_AUTO_UNFREEZE_*` + `AP2_VALIDATOR_JUDGE_*`
  reads outside their component bodies (automation_status.py,
  doctor.py, _shared.py, briefing_validators.py, conftest.py).
- TB-334 (axis 5 — core agent-runtime cluster): add
  `Config.get_core_value` helper paralleling `get_component_value`,
  migrate `AP2_AGENT_MODEL` / `AP2_AGENT_EFFORT` / `AP2_TASK_MAX_TURNS`
  / `AP2_CONTROL_MAX_TURNS` / `AP2_VERIFY_JUDGE_MAX_TURNS` reads in
  `daemon.py`, `verify.py`, `status_report.py`, `components/janitor/`.
- TB-335 (axis 5 — core ideation cluster): migrate
  `AP2_IDEATION_*` reads in `ideation.py` + `ideation_scrub.py`
  using the new `get_core_value` helper from TB-334.
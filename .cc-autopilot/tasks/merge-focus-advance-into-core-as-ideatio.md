# Merge focus_advance into core as ideation-halt; rename the AP2_FOCUS_ADVANCE knobs to the AP2_IDEATION_HALT namespace

Tags: #autopilot #components #core #refactor #ideation-halt #rename

## Goal

Post-TB-342, the `focus_advance` component no longer does multi-focus
rotation — it collapsed to a single ideation-exhaustion detector:
count consecutive 0-proposal ideation cycles since the last
`goal_updated`, and at the `AP2_FOCUS_ADVANCE_EMPTY_CYCLES` threshold
emit `roadmap_complete` once to park the ideation trigger (or, when
`AP2_FOCUS_AUTO_ADVANCE_DISABLED` is set, surface a decisions-needed
bullet for manual halt). That residual function is **core ideation
lifecycle**, not an optional component: it always runs, it's
essential (without it ideation burns SDK cycles forever against an
exhausted goal), and its output (`roadmap_complete` /
`roadmap_exhausted`) is consumed by ~10 core modules. The "component"
framing was correct for rotation; it's vestigial now. The knob names
are also misleading post-collapse — `*_AUTO_ADVANCE_*` describes a
pointer-advance that no longer exists.

Two coupled changes: (1) merge the detector from
`ap2/components/focus_advance/` into a core module
`ap2/ideation_halt.py`, removing the component; (2) rename the two
knobs to the `AP2_IDEATION_HALT_*` namespace that matches what they
now do, keeping the old flat names as deprecated aliases so no
stale env silently loses its setting.

Behavior is preserved exactly — same exhaustion-detection logic, same
default threshold (3), same kill-switch semantics, same
`roadmap_complete` event + `ap2 status` "parked — ideation exhausted"
line. This is a relocation + rename, not a behavior change.

Why now: it's the honest consequence of TB-342 (flagged by operator
audit 2026-05-30), it removes a degenerate component + a misleading
knob name ahead of the OSS cut, and it simplifies the registry
(presenting exhaustion-halt as core is more accurate than dressing it
as an opt-in component). Meta-infra, roadmap parked →
`--skip-goal-alignment`.

## Scope

Work this as a checklist — the Verification section pins completeness.

1. **New core module `ap2/ideation_halt.py`.** Relocate the body
   from `ap2/components/focus_advance/impl.py` (use `git mv` to
   preserve history). Rename the entry point
   `_maybe_advance_focus(cfg, sdk)` → `maybe_halt_on_exhaustion(cfg)`
   (drop the vestigial `sdk` param). Rename the helpers to drop the
   "focus/advance" framing (`_ideation_empty_against_focus` →
   `_consecutive_empty_ideation_cycles`, `_focus_auto_advance_disabled`
   → `_ideation_halt_disabled`, `_advance_empty_cycles_threshold` →
   `_ideation_halt_empty_cycles_threshold`). Import the pointer
   helpers (`load_pointer` / `save_pointer` / `read_focus_list`) from
   `ap2.goal` (core). Read the two knobs via
   `cfg.get_core_value("ideation_halt_empty_cycles")` /
   `cfg.get_core_value("ideation_halt_disabled")`.

2. **`ap2/daemon.py` `_tick`.** Remove `focus_advance` from the
   registry tick-hook dispatch path and call
   `ideation_halt.maybe_halt_on_exhaustion(cfg)` directly at the same
   PRE_DISPATCH position the component hook fired (preserve ordering
   relative to the other PRE_DISPATCH hooks). `daemon` importing the
   core `ideation_halt` module is fine (core→core). Drop the
   `focus_advance` re-export aliases in daemon.py if any (TB-313).

3. **Remove the component.** Delete `ap2/components/focus_advance/`
   (the `__init__.py` re-export shim, `impl.py` — now moved — and
   `manifest.py`). The registry is filesystem-discovered, so deleting
   the subpackage removes it from `Registry.discover()`. Verify no
   other module imports `ap2.components.focus_advance`.

4. **`ap2/core_config_schema.py`.** Add two `CORE_CONFIG_SCHEMA`
   entries: `ideation_halt_empty_cycles` (int, default = the current
   focus-advance empty-cycles default — 3) and `ideation_halt_disabled`
   (bool, default False). Mirror the existing core ConfigKey shape
   (name, type, default, description, hot_reloadable).

5. **`ap2/config_compat.py` FLAT_TO_SECTIONED.** Repoint + rename:
   - Add canonical: `"AP2_IDEATION_HALT_EMPTY_CYCLES":
     "core.ideation_halt_empty_cycles"`,
     `"AP2_IDEATION_HALT_DISABLED": "core.ideation_halt_disabled"`.
   - Keep `"AP2_FOCUS_ADVANCE_EMPTY_CYCLES"` and
     `"AP2_FOCUS_AUTO_ADVANCE_DISABLED"` as DEPRECATED aliases mapping
     to the SAME new `core.*` paths (so an operator env still setting
     the old flat name keeps working), wired through the existing
     deprecated-alias / one-shot `env_deprecated` mechanism. Remove
     the old `components.focus_advance.*` targets.

6. **Docs.** `ap2/howto.md` Configuration knobs: rename to the
   `AP2_IDEATION_HALT_*` entries, document the deprecated aliases.
   `ap2/init.py` ENV_TEMPLATE / `_TEMPLATE_EXEMPT_KNOBS`: update knob
   references. `ap2/architecture.md`: drop `focus_advance` from the
   component list / note ideation-halt is core. The TB-305 docs-drift
   gate (`test_every_env_knob_documented`) requires every AP2_* knob
   in source — including the deprecated aliases — to have a
   backtick-fenced mention, so document both new + deprecated names.

7. **Tests.** Relocate/rename `ap2/tests/test_tb226_focus_rotation.py`
   → `ap2/tests/test_ideation_halt.py` (update imports to
   `ap2.ideation_halt` + the renamed knobs). Drop `focus_advance` from
   the TB-317 disabled-config test and the TB-311 import-direction
   gate's component enumeration (one fewer component). Add a
   back-compat test: setting the deprecated `AP2_FOCUS_AUTO_ADVANCE_DISABLED`
   still disables the halt (resolves to `core.ideation_halt_disabled`).

## Design

- **Home = `ap2/ideation_halt.py` (new core module), not folded into
  goal.py.** `goal.py` owns `roadmap_exhausted` (the reader) + the
  pointer helpers; `ideation_halt.py` is the writer and imports those.
  Separate module keeps goal.py from growing and makes the
  writer/reader split legible. daemon calls the writer; ideation /
  status / web call the reader (`roadmap_exhausted`) — unchanged.

- **Knob namespace.** Canonical names become
  `AP2_IDEATION_HALT_EMPTY_CYCLES` (threshold) and
  `AP2_IDEATION_HALT_DISABLED` (kill switch). Deprecated aliases for
  the old `AP2_FOCUS_*` names map to the same core keys for one
  release — the operator env doesn't currently set either (verified
  2026-05-30), so this is belt-and-suspenders against stale envs in
  other sandboxes.

- **Behavior preserved.** Same detection (empty cycles since
  `goal_updated`), same default threshold (3), same emit-once +
  dismissal-clear, same kill-switch-surfaces-bullet path, same
  `roadmap_complete` event name (renaming the EVENT is explicitly out
  of scope — it'd churn the event-type docs gate + ~10 consumers).
  The `ap2 status` focus line + Components block lose `focus_advance`
  from the latter but the "parked — ideation exhausted" line (driven
  by `goal.roadmap_exhausted`) is unchanged.

- **Insulation.** Daemon imports `ideation_halt` at module load; the
  change takes effect on the next `ap2 stop && ap2 start`. The pytest
  gate catches a broken move before commit.

## Verification

- `uv run --extra dev pytest -q ap2/tests/` — full suite passes
  (project's canonical gate).
- `test -f ap2/ideation_halt.py` — the core module exists.
- `! test -d ap2/components/focus_advance` — the component subpackage
  is gone.
- `! grep -rnE "AP2_FOCUS_(ADVANCE|AUTO_ADVANCE)" ap2/ --include="*.py" | grep -v "config_compat.py" | grep -v "tests/"` — the old knob names remain ONLY in config_compat.py (the deprecated-alias map) and tests; no live code path still reads them by the old name.
- `grep -q "AP2_IDEATION_HALT_EMPTY_CYCLES" ap2/howto.md` — the new threshold knob is documented.
- `grep -q "AP2_IDEATION_HALT_DISABLED" ap2/howto.md` — the new kill-switch knob is documented.
- `grep -q "ideation_halt_empty_cycles" ap2/core_config_schema.py` — the core schema carries the threshold key.
- `grep -q "ideation_halt_disabled" ap2/core_config_schema.py` — the core schema carries the kill-switch key.
- `uv run --extra dev python -c "import ap2.registry as r; reg=r.Registry.discover(); names=sorted(c.name for c in reg.components); assert 'focus_advance' not in names, names; print(names)"` — focus_advance no longer a discovered component.
- `uv run --extra dev python -c "import ap2.ideation_halt as h; assert hasattr(h, 'maybe_halt_on_exhaustion'); import inspect; assert 'sdk' not in inspect.signature(h.maybe_halt_on_exhaustion).parameters; print('ok')"` — core entry point exists with the cleaned signature.
- `ap2/daemon.py` Prose: `_tick` calls `ideation_halt.maybe_halt_on_exhaustion(cfg)` directly (not via the registry tick-hook walk) at the PRE_DISPATCH point the focus_advance hook previously occupied; no `ap2.components.focus_advance` import remains. Judge confirms via Read.
- `ap2/config_compat.py` Prose: `FLAT_TO_SECTIONED` maps the new `AP2_IDEATION_HALT_*` names to `core.ideation_halt_*`, and retains the old `AP2_FOCUS_ADVANCE_EMPTY_CYCLES` / `AP2_FOCUS_AUTO_ADVANCE_DISABLED` as deprecated aliases pointing at the same core paths (not the removed `components.focus_advance.*`). Judge confirms via Read.
- `ap2/tests/` Prose: a back-compat test asserts the deprecated `AP2_FOCUS_AUTO_ADVANCE_DISABLED` still disables the halt (resolves through to `core.ideation_halt_disabled`), and the disabled-config + import-direction tests no longer reference `focus_advance`. Judge confirms via Read.

## Out of scope

- Renaming the `roadmap_complete` event or `roadmap_exhausted` /
  `roadmap_complete_notice_dismissed` (TB-340) — keep the event name
  + the goal.py reader API; renaming churns ~10 consumers + the
  event-type docs gate for no functional gain. Follow-up if desired.
- Changing the empty-cycles detection semantics, the default
  threshold (stays 3), or the `goal_updated`-reset behavior (TB-342).
- Removing the deprecated `AP2_FOCUS_*` aliases entirely — keep them
  one release; a later task can drop them.
- The two config-polish items flagged separately (`ideation_disabled`
  / `ideation_scrub_model` ""-vs-None inline defaults; `auto_diagnose_*`
  missing from CORE_CONFIG_SCHEMA) — unrelated.
- Multi-focus headings in goal.md — they remain operator prose (TB-342);
  this task doesn't touch goal.md parsing.
## Attempts

### 2026-05-30 — verification_failed
(no summary)
- **kind:** project_wide
- **verify_command:** uv run pytest -q ap2/tests/
- **exit_code:** 1
- **stderr_tail:** 
- **Debug dumps:** `prompt: .cc-autopilot/debug/20260530T042637Z-TB-345.prompt.md`, `stream: .cc-autopilot/debug/20260530T042637Z-TB-345.stream.jsonl`, `messages: .cc-autopilot/debug/20260530T042637Z-TB-345.messages.jsonl`
### 2026-05-30 — verification_failed
(no summary)
- **kind:** project_wide
- **verify_command:** uv run pytest -q ap2/tests/
- **exit_code:** 1
- **stderr_tail:** 
- **Debug dumps:** `prompt: .cc-autopilot/debug/20260530T051731Z-TB-345.prompt.md`, `stream: .cc-autopilot/debug/20260530T051731Z-TB-345.stream.jsonl`, `messages: .cc-autopilot/debug/20260530T051731Z-TB-345.messages.jsonl`

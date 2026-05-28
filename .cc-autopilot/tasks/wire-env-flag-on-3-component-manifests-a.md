# Wire env_flag on 3 component manifests + add AP2_AUTO_UNFREEZE_DISABLED

Tags: #autopilot #components #env-flag #manifest #refactor

## Goal

The just-landed component refactor focus ("Current focus: refactor
features into opt-in components") shipped 11 axis-tasks (TB-309
through TB-319) and successfully migrated every autonomous behavior
into `ap2/components/<name>/` subpackages. But `ap2 status`'s new
`## Components` block (TB-319) exposes a remaining gap against
goal.md's Done-when bullet "Every component can be independently
disabled via its env flag": four components — `attention`,
`auto_approve`, `auto_unfreeze`, `focus_advance` — currently
render as `env_flag=None`, meaning the registry has no canonical
knob to mark them disabled. The operator made the explicit
decision on 2026-05-28 that `attention/` stays always-on (its
detectors are baseline operator-legible signal), while the other
three should get explicit env flags so they're independently
disable-able.

Wire `env_flag` + `default_enabled` on the three manifests:

- `auto_approve/`: `env_flag="AP2_AUTO_APPROVE"`, `default_enabled=False`
  (opt-in / require-polarity). Matches the existing semantics where
  `AP2_AUTO_APPROVE=1` is required to enable the autonomous-approve
  behavior; the env var is already read inside the tick hook for
  self-gating. Internal self-gate stays in place — the manifest
  wiring just makes the registry aware so `ap2 status` renders the
  state correctly and the `briefing_validators(cfg)` filter (and
  any future registry-level filter) picks it up.

- `auto_unfreeze/`: `env_flag="AP2_AUTO_UNFREEZE_DISABLED"`,
  `default_enabled=True` (kill switch / suppress-polarity). This
  env knob does NOT exist today — auto_unfreeze runs whenever the
  component is loaded. Add a new `AP2_AUTO_UNFREEZE_DISABLED` knob
  (truthy = disable the sweep), mirror the polarity / naming
  convention of `AP2_JANITOR_DISABLED` and `AP2_VALIDATOR_JUDGE_DISABLED`,
  list it in `env_reload.HOT_RELOADABLE_KNOBS`, and add the same
  internal self-gate at the top of the tick hook so behavior
  matches what the registry advertises.

- `focus_advance/`: `env_flag="AP2_FOCUS_AUTO_ADVANCE_DISABLED"`,
  `default_enabled=True` (kill switch). The env knob ALREADY
  exists (TB-226) and is read inside the tick hook's
  `_maybe_advance_focus`; the manifest wiring just exposes it so
  the registry knows about it. No new knob, no internal-logic
  change.

The other env-flag-bearing components (`janitor`, `mattermost`,
`validator_judge`) already have their manifests wired and stay
untouched. `attention/` keeps `env_flag=None` per operator
decision.

Why now: TB-319 just made the gap operator-visible on every
`ap2 status` run. Closing it before the next focus begins keeps
the just-shipped component refactor coherent and unblocks any
future "disable everything except core" operator scenario. The
work is ~3 manifest edits + 1 new env knob + 1 internal self-gate
addition + docs + tests — small, scope-tight, no architectural
risk.

## Scope

- `ap2/components/auto_approve/manifest.py` — set
  `env_flag="AP2_AUTO_APPROVE"` and `default_enabled=False`.
- `ap2/components/auto_unfreeze/manifest.py` — set
  `env_flag="AP2_AUTO_UNFREEZE_DISABLED"` and
  `default_enabled=True`.
- `ap2/components/auto_unfreeze/__init__.py` — add a top-of-tick-hook
  short-circuit reading `AP2_AUTO_UNFREEZE_DISABLED` from
  `os.environ` directly (matching the lazy-read pattern of
  `AP2_FOCUS_AUTO_ADVANCE_DISABLED` in `focus_advance/`). When
  truthy, the hook returns immediately without running
  `auto_unfreeze_sweep`. Emit an event (mirror the
  `auto_unfreeze_skipped` shape if it exists, else
  `auto_unfreeze_disabled` with `reason="env_flag_set"`) on the
  first skip per process to make the suppression visible in audit;
  cheap deduplication acceptable.
- `ap2/components/focus_advance/manifest.py` — set
  `env_flag="AP2_FOCUS_AUTO_ADVANCE_DISABLED"` and
  `default_enabled=True`.
- `ap2/env_reload.py` — append `AP2_AUTO_UNFREEZE_DISABLED` to
  `HOT_RELOADABLE_KNOBS` so an operator toggling the knob at
  runtime takes effect on the next tick (matches the existing
  AP2_AUTO_UNFREEZE_* knobs already listed there).
- `ap2/howto.md` — add `AP2_AUTO_UNFREEZE_DISABLED` to the
  `## Configuration knobs` section with a backtick-fenced mention
  (TB-305's `test_every_env_knob_documented` gate will otherwise
  fail).
- `ap2/init.py` — `_TEMPLATE_EXEMPT_KNOBS` exemption set already
  declares `AP2_AUTO_UNFREEZE_*` as template-exempt (internal
  default, operator-rare); confirm the new knob is covered by the
  existing exemption or add it explicitly.
- `ap2/tests/test_components_disabled.py` (TB-317) — extend the
  registry's disabled-config sweep so the three newly-flagged
  components each get a "registry sees disabled when env set"
  assertion. Mirrors the existing `janitor` / `validator_judge`
  / `mattermost` disable assertions.

## Design

- **Polarity convention.** The registry already handles both
  polarities via `default_enabled` (`ap2/registry.py:160-227`):
  `default_enabled=True` makes `env_flag` a suppress / kill-switch
  knob (truthy disables); `default_enabled=False` makes `env_flag`
  a require / opt-in knob (truthy enables). Mirror existing
  components — janitor / validator_judge use suppress-polarity
  with `*_DISABLED` suffix; the new `AP2_AUTO_UNFREEZE_DISABLED`
  follows the same suffix. `AP2_AUTO_APPROVE` is the one exception
  and uses require-polarity (no `_DISABLED` suffix) because the
  underlying knob already has require-polarity semantics in
  TB-228's autoapprove module.

- **Internal self-gate stays.** `tick_hooks(phase)` walks every
  component's tick hook regardless of `env_flag` (line 416 walks
  `self.components`, not `enabled_components(cfg)`); the env-flag
  wiring is informational at the registry layer for status rendering
  and briefing-validator filtering. The actual no-op-when-disabled
  behavior must live inside each tick hook (or its delegated
  helper). `auto_approve` and `focus_advance` already self-gate.
  `auto_unfreeze` needs the new self-gate added.

- **`auto_unfreeze` audit event on first skip.** Mirrors
  `attention`'s `attention_pushed_suppressed_no_destination`
  one-shot pattern: a sticky boolean on the module avoids
  per-tick noise while still surfacing the disabled state in
  `events.jsonl`. First skip → event; subsequent skips → silent.
  Resets only on process restart.

- **`ap2 status` rendering.** No code change needed — the
  registry's `env_flag_description(env)` helper already renders
  the polarity-correct string ("AP2_FOO unset" for kill switches,
  "AP2_FOO=value" for opt-ins). Land the manifest edits and the
  status block updates automatically.

- **Disabled-config test coverage.** The existing TB-317 test
  walks `enumerate_disabled_env_flags` which already knows how to
  flip each component's env knob. The three new env knobs
  (`AP2_AUTO_APPROVE` unset, `AP2_AUTO_UNFREEZE_DISABLED=1`,
  `AP2_FOCUS_AUTO_ADVANCE_DISABLED=1`) need to be reflected in
  whatever per-component test asserts the registry-marks-disabled
  state. The helper may already auto-pick-up new manifest
  `env_flag` values via the registry walk — if so, the test
  passes automatically; if not, extend per the helper's existing
  shape.

## Verification

- `uv run pytest -q ap2/tests/` — full suite passes after the
  manifest edits + new knob + self-gate land.
- `grep -q 'env_flag="AP2_AUTO_APPROVE"' ap2/components/auto_approve/manifest.py` —
  auto_approve manifest exposes the existing opt-in knob.
- `grep -q 'env_flag="AP2_AUTO_UNFREEZE_DISABLED"' ap2/components/auto_unfreeze/manifest.py` —
  auto_unfreeze manifest exposes the new kill switch.
- `grep -q 'env_flag="AP2_FOCUS_AUTO_ADVANCE_DISABLED"' ap2/components/focus_advance/manifest.py` —
  focus_advance manifest exposes the existing TB-226 kill switch.
- `grep -q AP2_AUTO_UNFREEZE_DISABLED ap2/env_reload.py` — the new
  knob is in HOT_RELOADABLE_KNOBS.
- `grep -q '\`AP2_AUTO_UNFREEZE_DISABLED\`' ap2/howto.md` — the
  new knob has a backtick-fenced mention in `## Configuration knobs`
  (TB-305 docs-drift gate format).
- `grep -q AP2_AUTO_UNFREEZE_DISABLED ap2/components/auto_unfreeze/__init__.py` —
  the internal self-gate reads the knob.
- `! grep -qE 'env_flag=None' ap2/components/auto_approve/manifest.py` —
  auto_approve's manifest no longer declares env_flag=None.
- `! grep -qE 'env_flag=None' ap2/components/auto_unfreeze/manifest.py` —
  same for auto_unfreeze.
- `! grep -qE 'env_flag=None' ap2/components/focus_advance/manifest.py` —
  same for focus_advance.
- `ap2/components/auto_unfreeze/__init__.py` Prose: the tick hook
  short-circuits at the top when `AP2_AUTO_UNFREEZE_DISABLED` is
  truthy in `os.environ`, returns early without running the
  sweep, and emits an audit event (e.g. `auto_unfreeze_disabled`)
  exactly once per process on first skip — subsequent skips stay
  silent (sticky state). Judge confirms via Read.
- `ap2/tests/test_components_disabled.py` Prose: the test fixture
  asserts that setting `AP2_AUTO_UNFREEZE_DISABLED=1`,
  unsetting `AP2_AUTO_APPROVE`, and setting
  `AP2_FOCUS_AUTO_ADVANCE_DISABLED=1` each independently flips
  the registry's `is_enabled(env)` to False for the respective
  component. Judge confirms via Read.

## Out of scope

- Refactoring `Registry.tick_hooks(phase)` to filter via
  `enabled_components(cfg)` so disabled components don't even
  return their tick hooks. That's a cleaner architecture but
  changes observable behavior (today every hook fires every
  tick and self-gates internally; tomorrow hooks wouldn't fire
  at all when disabled). Defer to a follow-on briefing.
- Adding env_flag to `attention/` — operator made the explicit
  call that attention stays always-on as baseline operator-legible
  signal.
- Adding component dependency / topo-sort wiring (`Manifest.dependencies`
  field stays a stub per `ap2/registry.py:410-413`). No current
  component needs it; goal.md's separate decision about wiring
  this stays open.
- Extracting the inline `auto_approve` gate logic in `daemon._tick`
  out into the component (separate decision the operator surfaced
  on 2026-05-28 alongside the env_flag question; deferred).
- Renaming `AP2_AUTO_APPROVE` to align with the `*_DISABLED` suffix
  convention. The existing knob name is operator-facing and
  documented; backwards compatibility wins.

## Goal

Current focus: refactor features into opt-in components — relocate
`ap2/attention.py` into `ap2/components/attention/` as the next
sequenced axis-5 subpackage migration. Per goal.md L187-188 the
`attention/` migration "publishes via the channel-adapter abstraction
landed in axis 3" — TB-312 shipped that adapter (`ap2/channel.py` +
three sibling adapters + `Registry.channel_adapters(cfg)`), so
attention is now unblocked. The existing TB-310 stub manifest at
`ap2/components/attention/manifest.py` late-binds via `from ap2 import
daemon as _daemon_mod`; this task makes the subpackage carry the
implementation body and exposes every previously-direct-imported
symbol via `hook_points` so the daemon's module-level alias block can
resolve through the registry rather than through a flat `from ap2
import attention` (which the TB-311 import-direction gate would
otherwise have to keep exempting). Preserves the existing
`AP2_ATTENTION_IMMEDIATE_PUSH` / `AP2_ATTENTION_DEBOUNCE_S` knobs
verbatim — purely structural refactor, zero behavior change.

Why now: 4 of 6 axes have shipped (1, 2, 3, half of 6) and 3 of 7
component migrations are done (janitor, focus_advance, auto_unfreeze,
mattermost). Of the two remaining migrations, `attention/` is the
smaller-blast-radius one (auto_approve is goal.md-sequenced LAST per
L196-197). The canary pattern is now well-grooved across TB-309 →
TB-313 → TB-314, so this lands cheaply; deferring it lets the
TB-311 import-direction gate's exemption set grow stale.

## Scope

- `git mv ap2/attention.py ap2/components/attention/__init__.py`.
- Rewrite `ap2/components/attention/manifest.py` to source symbols
  intra-package via `from . import ...` (drop the late-binding via
  `daemon._maybe_emit_attention_events`); declare every previously
  daemon-imported symbol from `attention.py` in `hook_points` so
  core can resolve them via the registry. Match the
  `auto_unfreeze/manifest.py` shape (commit 73f5a52) — constants
  and functions both live in `hook_points`.
- Update `ap2/daemon.py` to drop the flat `from ap2 import attention`
  import and rebind module-level attention aliases via
  `default_registry().get("attention").hook_points[...]`. Match the
  shape used for `auto_unfreeze` in daemon.py post-73f5a52.
- Fix the handful of test files that import the flat path. Confirm via
  `grep -rln "from ap2 import attention\|from ap2.attention\|import ap2.attention"`
  in `ap2/tests/` and rewrite to `ap2.components.attention`.
- Add `ap2/tests/test_tb315_attention_migration.py` (regression pin,
  ~10 tests) covering: subpackage structural shape, manifest
  `hook_points` triad exposes the daemon-alias surface, daemon's
  module-level aliases resolve via registry, env-knob preservation
  (`AP2_ATTENTION_IMMEDIATE_PUSH` + `AP2_ATTENTION_DEBOUNCE_S`
  verbatim), end-to-end `_maybe_emit_attention_events` via the
  manifest's tick hook, and that the import-direction gate stays
  green after the move.

## Design

Mirror the `auto_unfreeze` migration shape (commit 73f5a52) — that's
the closest sibling because attention also exposes constants alongside
functions through its `hook_points`. Three structural changes:

1. **File move**: `git mv` the body. Keep the file's existing module
   docstring; add a one-line note at top that the module's home moved
   to `ap2/components/attention/__init__.py` as part of axis-5
   migration.
2. **Manifest rewrite**: replace the late-binding `_tick_hook` with
   the body-local one (the wrapping `_tick_hook` that emits the
   `[ap2] _maybe_emit_attention_events error: ...` stderr line stays
   to preserve the pre-existing error surface). Populate `hook_points`
   with every symbol daemon imports — at minimum
   `_maybe_emit_attention_events`, `detect_attention_conditions`,
   `should_suppress`, and the attention-debounce / immediate-push
   state helpers. Audit via `grep -nE 'attention\.(_?[a-z][a-zA-Z_]*)'
   ap2/daemon.py` to enumerate every direct call site.
3. **Daemon rebind**: the module-level alias block (look for the
   block analogous to auto_unfreeze's at daemon.py L1781-1793
   post-73f5a52) resolves each symbol via
   `default_registry().get("attention").hook_points[name]`.

The `env_flag=None` polarity matches `auto_unfreeze` — there's no
single master enable/disable knob, the per-behavior knobs gate
internally. The import-direction gate's exemption set should NOT
need attention added — the registry resolution path is the whole
point of the migration.

## Verification

- `uv run pytest -q ap2/tests/test_tb315_attention_migration.py` — new regression-pin module passes
- `uv run pytest -q ap2/tests/test_core_import_direction.py` — import-direction gate still green
- `uv run pytest -q ap2/tests/test_tb310_tick_hook_protocol.py` — tick-hook protocol pin still green
- `uv run pytest -q ap2/tests/` — full suite passes
- `test -f ap2/components/attention/__init__.py` — subpackage body present
- `test ! -f ap2/attention.py` — flat module removed
- `! grep -rqE 'from ap2 import attention\b|from ap2\.attention\b|import ap2\.attention\b' ap2/daemon.py ap2/cli.py ap2/cli_daemon.py ap2/status_report.py ap2/operator_queue.py ap2/briefing_validators.py` — core never statically imports the flat-or-relocated attention module path (axis-6 cleavage)
- `grep -q "hook_points" ap2/components/attention/manifest.py` — manifest declares hook_points dict
- `grep -qE 'env_flag\s*=\s*None' ap2/components/attention/manifest.py` — env_flag polarity matches auto_unfreeze (no master switch)
- `grep -q "AP2_ATTENTION_IMMEDIATE_PUSH" ap2/components/attention/__init__.py` Prose: env-knob name preserved verbatim (operator-facing contract)
- `ap2/daemon.py` Prose: the module-level attention alias block resolves via `default_registry().get("attention").hook_points[...]` rather than `from ap2 import attention`; judge confirms via Read

## Out of scope

- `auto_approve/` migration (goal.md L196-197 sequences it LAST;
  separate task next cycle).
- Disabled-config test suite (separate axis-6 task this cycle).
- Channel-adapter refactor of attention's MM push call site
  (already routed through the registry's adapter list per axis 3
  shipped work; no further work needed).
- Renaming `AP2_ATTENTION_*` env knobs (goal.md L64-67 constraint).
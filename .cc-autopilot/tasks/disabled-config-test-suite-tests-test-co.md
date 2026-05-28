## Goal

Current focus: refactor features into opt-in components — ship the
disabled-config test suite that goal.md L206-209 names as the second
half of axis 6 ("Toggle-correctness tests + CI gate"). Add
`ap2/tests/test_components_disabled.py` (or distributed per-component)
that flips every registered component's env flag to its disabled
polarity and asserts the core dispatch-verify-report loop, briefing
validators, operator queue, basic ideation entry points, and
status-report digest composition + channel-adapter routing all keep
working. Closes the "every component can be independently disabled"
done-when criterion from goal.md L62 and the "full test suite passes
in default configuration AND in an every-component-disabled
configuration" delete-test from L62-63.

Why now: with TB-312/313/314 landed there are now 4 real subpackage
components (janitor, focus_advance, auto_unfreeze, mattermost) plus
the core `ap2/channel.py` adapter ABC + sibling defaults — enough
surface to assert "core behavior unchanged when every component env
flag is suppressed". Last cycle this was deferred because only the
janitor canary existed; that gate has now cleared. Without this test,
the cleavage erodes silently — a future migration accidentally
re-couples core to a component and nobody notices until a downstream
distribution attempt (goal.md L211-214).

## Scope

- Add `ap2/tests/test_components_disabled.py` with a `monkeypatch`-driven
  fixture that sets the disabled polarity for every component env flag
  the registry knows about (walk `default_registry().manifests()` and
  flip each `manifest.env_flag` to its disabled value). The fixture
  handles both polarities: components whose env_flag enables (e.g.
  `AP2_JANITOR_DISABLED` is suppress-style — set `=1`) and components
  whose env_flag gates the body (e.g. `AP2_MM_CHANNELS` — clear from
  env). Components with `env_flag=None` keep firing per goal.md L267-271
  ("conservative defaults" — only knob-bearing components are toggled).
- Smoke-test core surfaces in the disabled config: (a) board parse +
  render round-trip on a small fixture board; (b)
  `_validate_briefing_structure` accepts a canonical briefing fixture
  (deterministic checks still run, validator_judge component disabled
  bypasses its SDK call); (c) operator-queue drain on a fixture op;
  (d) status-report `compose_status_report_text` composes a digest
  from a fixture board + events tail + focus state (composition stays
  in core per goal.md L150-151); (e) channel-adapter routing — assert
  the registry's `channel_adapters(cfg)` returns at least the core
  sibling adapters (`StdoutChannelAdapter` /
  `FileAppendChannelAdapter` / `WebhookChannelAdapter`) so the digest
  has a non-null default destination per goal.md L156-159.
- Add an `enumerate_disabled_env_flags` helper in the test module that
  the test uses to build its monkeypatch dict — exposed for any future
  per-component disabled test that wants the same polarity-correct
  setup.
- Run the full `uv run pytest -q ap2/tests/` suite in the disabled
  config as well (parametrized via a session-scoped fixture or a
  separate pytest invocation in the new test module). If the existing
  suite has component-dependent tests (e.g. tests for janitor's tick
  behavior), mark them `requires_component=<name>` and skip when off
  per goal.md L63 — but don't paper over real failures.

## Design

Goal.md L62-63 is the delete-test: "the full test suite passes in the
default configuration AND in an 'every component disabled'
configuration. (Component-specific tests may be marked
`requires_component=<name>` and skip when off.)" Two implementation
options for the test module:

1. **In-process fixture (preferred)**: a session-scoped pytest fixture
   that monkeypatches every component env flag to its disabled value,
   then explicitly drives a curated set of "core surface" smoke
   assertions inline (board parse, briefing validation, status-report
   composition, channel-adapter routing). This avoids spawning a
   subprocess and keeps wall-clock cost low — 1-2s vs. ~90s for a
   full subprocess re-run.

2. **Subprocess re-run (fallback)**: a single test that
   `subprocess.run`s `uv run pytest -q ap2/tests/` with the disabled
   env block exported in the child's environment. Truer to the
   "every component disabled" claim but doubles CI time.

Default to (1) for the new test module; if the smoke assertions miss
a real failure mode the suite's existing tests would catch, fall back
to (2) gated behind a `--run-disabled-config` pytest opt-in flag so
the gate runs in CI but not in routine local dev.

The `requires_component=<name>` skip marker is a new pytest marker
declared in `conftest.py` (or `pyproject.toml` `[tool.pytest.ini_options]`).
Tests using it skip when the named component's env_flag indicates
disabled — implementation walks the same `enumerate_disabled_env_flags`
helper. Initially mark no tests (default config is fully-enabled so
the marker is a no-op there); the new disabled-config test module
exercises the skip path on its own behalf if any of its smoke
assertions need a specific component enabled.

## Verification

- `uv run pytest -q ap2/tests/test_components_disabled.py` — new disabled-config test passes
- `uv run pytest -q ap2/tests/` — full suite still green in default config
- `uv run pytest -q ap2/tests/test_core_import_direction.py` — import-direction gate still green
- `test -f ap2/tests/test_components_disabled.py` — new test module present
- `grep -q "enumerate_disabled_env_flags" ap2/tests/test_components_disabled.py` — disabled-env-flag enumeration helper present
- `grep -q "channel_adapters" ap2/tests/test_components_disabled.py` — channel-adapter routing surface covered
- `grep -q "_validate_briefing_structure\|briefing_validators" ap2/tests/test_components_disabled.py` — briefing validation surface covered
- `grep -q "compose_status_report_text\|status_report" ap2/tests/test_components_disabled.py` — status-report digest composition surface covered
- `ap2/tests/test_components_disabled.py` Prose: walks the registry's manifests to discover every component env flag and asserts core surfaces still function with all of them set to disabled polarity; judge confirms via Read
- `ap2/tests/test_components_disabled.py` Prose: at least one assertion confirms `default_registry().channel_adapters(cfg)` returns the core sibling adapters even when the `mattermost/` component is disabled (digest has non-null destination per goal.md L156-159); judge confirms via Read

## Out of scope

- Subprocess full-suite re-run mode (the `--run-disabled-config`
  pytest opt-in fallback is mentioned in Design but not required to
  ship this task — the in-process smoke surface is sufficient for the
  axis-6 done-when criterion).
- Marking any specific existing test with `requires_component=<name>`
  (the marker is declared, but no existing tests need it today —
  components were extracted preserving observable behavior).
- New CI workflow files (the existing pytest run picks up the new
  module automatically).
- Renaming any component env flag (goal.md L64-67 constraint).
- Adding more components — this is a test gate, not a migration task.
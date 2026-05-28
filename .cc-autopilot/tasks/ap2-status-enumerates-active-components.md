---
title: "`ap2 status` enumerates active components from the registry (closes Progress signal)"
tags: [autopilot, components, cli, status, registry, operator-legible]
---

## Goal

Current focus: refactor features into opt-in components — close the
Progress signal at goal.md L235-237 ("The registry's tick-hook list
is the canonical source of 'what runs each tick'; `ap2 status` could
in principle enumerate active components from it") by adding a
`## Components` block to `ap2 status` (text + JSON output) that lists
every component the registry discovers, with on/off state derived
from each manifest's `env_flag` polarity. The component registry has
existed since TB-309; today the only way to discover registered
components is to `ls ap2/components/` and read manifests by hand.
After this lands, an operator running `ap2 status` sees at a glance
which components are wired into the daemon AND which are currently
enabled/disabled — paying out the value of the axes-1-5 cleavage.

Why now: with TB-309 through TB-317 shipped, the registry has 7 real
components (janitor, mattermost, focus_advance, auto_unfreeze,
attention, validator_judge) plus auto_approve (TB-318 pending). The
Progress signal at goal.md L235-237 cannot close until the operator
surface enumerates components; without this task the refactor delivers
only internal plumbing benefit. The slot is naturally now because
the cleavage is complete enough that a stable enumeration list exists.

## Scope

- Edit `ap2/cli.py` (or wherever `cmd_status` lives — likely
  `ap2/cli.py` based on `cmd_add`'s neighborhood) to add a
  `## Components` section after the existing top-block (daemon /
  version / focus / board / cron / tasks / events / web / etc.).
- Each component line shows: `<name>: <on|off> [<env_flag_repr>]`
  where `<env_flag_repr>` is e.g. `AP2_JANITOR_DISABLED unset → on`,
  `AP2_MM_CHANNELS=… → on`, or `env_flag=None → on` for the four
  manifests with no master switch.
- Sort components by manifest name (alphabetic), matching the order
  `default_registry().tick_hooks(phase)` already walks.
- Add a JSON parity surface: `ap2 status --json` (existing flag, see
  TB-298's attention parity precedent) gains a top-level
  `"components": [{"name": ..., "enabled": bool, "env_flag": str|None,
  "default_enabled": bool}, ...]` entry.
- Reuse the existing `default_registry()` access — no new modules.
- Resolve enabled state via the same logic the registry uses for
  walking enabled manifests: read `manifest.env_flag` against
  `os.environ` with the same polarity rule the registry already
  applies (suppress-style for `*_DISABLED` flags; truthy-required for
  `AP2_MM_CHANNELS`-style flags; etc.). If no helper exists in
  `ap2/registry.py` for this, add `Manifest.is_enabled(env=os.environ)
  -> bool` as a tiny method so both the registry walk and the status
  enumeration share the polarity logic.
- Add `ap2/tests/test_tb319_status_components.py` with at least:
  - One test that runs `ap2 status` against a tmp project and asserts
    the output contains `## Components` plus a line for `janitor`.
  - One test that runs `ap2 status --json` and asserts the parsed
    JSON has a `components` list with ≥7 entries (or matching the
    real registered count) and each entry has the documented shape.
  - A polarity test: set `AP2_JANITOR_DISABLED=1` in `monkeypatch.setenv`
    and assert the janitor entry's `enabled` flips to `False`.
  - A polarity test for the inverse polarity: with `AP2_MM_CHANNELS=""`
    (unset) the mattermost entry's `enabled` is `False`; with
    `AP2_MM_CHANNELS=channel-id` it's `True`.
- Document in `ap2/howto.md` under whichever section covers
  `ap2 status` output (search for the existing `ap2 status` mention).

## Design

`ap2 status` is the operator's CLI dashboard; goal.md L235-237 names
it as the natural surface for component enumeration. The
implementation is mechanical:

1. **Status helper**: in `ap2/cli.py` (or wherever the status renderer
   lives), after the top-block, call the registry to list
   manifests (add a helper if needed — a one-line wrapper around the
   cached filesystem walk). For each manifest, compute its enabled
   state via the polarity convention below.

2. **Polarity helper on Manifest**: `Manifest.is_enabled(env)` codifies
   the polarity convention already in registry walk logic:
   - `env_flag=None` → enabled iff `default_enabled` is True.
   - `env_flag="AP2_*_DISABLED"` (suppress polarity, default-on
     style) → enabled iff env var is unset / empty.
   - `env_flag="AP2_*"` non-suppress (require polarity) → enabled iff
     env var is set to a truthy value.

   The convention rule: if the name ends in `_DISABLED`, the env-flag
   is suppress-polarity; otherwise it's require-polarity. (Janitor,
   validator_judge use `_DISABLED`; mattermost uses
   `AP2_MM_CHANNELS` as require-polarity.) Verify this matches the
   existing enabled-walk logic. If a divergence shows up, the polarity
   helper goes where it most naturally belongs (likely a method on
   Manifest that the registry walk already calls).

3. **Text formatting**: each line is `  <name>: <on|off> (<env_flag_desc>)`
   where the desc is e.g. `AP2_JANITOR_DISABLED unset` for suppress
   polarity active, or `AP2_MM_CHANNELS=<id>` for require polarity
   set, or `env_flag=None` for always-on. Two-space indent matches
   existing status sub-block style.

4. **JSON parity**: extend the existing `--json` output dict with a
   `components` key. Mirror the TB-298 pattern (which added attention
   JSON parity to `ap2 status`).

5. **Web parity (out of scope this task)**: a `/components` web page
   could mirror this. Deferred to next cycle if operator wants it.

## Verification

- `uv run pytest -q ap2/tests/test_tb319_status_components.py` — new test file passes.
- `uv run pytest -q ap2/tests/test_cli.py` — existing CLI tests stay green.
- `uv run pytest -q ap2/tests/test_components_disabled.py` — TB-317 disabled-config gate still green.
- `uv run pytest -q ap2/tests/` — full suite passes.
- `uv run ap2 --project . status | grep -q "^## Components"` — text status output contains the new Components section header.
- `uv run ap2 --project . status | grep -qE "^  janitor: (on|off)"` — janitor entry renders with on/off state.
- `uv run ap2 --project . status --json | python -c "import json,sys; d=json.load(sys.stdin); assert isinstance(d.get('components'), list) and len(d['components']) >= 6 and all('name' in c and 'enabled' in c for c in d['components'])"` — JSON parity surfaces a `components` list whose entries each carry `name` + `enabled`.
- `ap2/cli.py` Prose: the status renderer enumerates components via the registry and renders the `## Components` block after the existing top-block sections. Judge confirms via Read.
- `ap2/howto.md` Prose: an entry under the `ap2 status` documentation describes the new Components block and what its on/off state means. Judge confirms via Read + Grep for "Components" in the howto file.

## Out of scope

- A `/components` web page parallel to TB-296's `/attention` pull
  page. Deferred — if operator wants web parity after the CLI lands,
  it's a one-cycle follow-up.
- Adding NEW env knobs to control which components show up. The
  enumeration walks whatever the registry discovers; no filter knobs.
- Mutating the registry walk's existing enabled/disabled polarity
  logic — purely additive surface.
- Surfacing per-component diagnostic info (tick counts, last-fired
  timestamp, recent events) — that's a richer follow-up. This task
  delivers the basic on/off enumeration only.
- Adding master kill-switch env flags to the four `env_flag=None`
  manifests (attention, auto_approve, auto_unfreeze, focus_advance).
  Surfaced as an open operator question in ideation_state.md; not
  in this task's scope.

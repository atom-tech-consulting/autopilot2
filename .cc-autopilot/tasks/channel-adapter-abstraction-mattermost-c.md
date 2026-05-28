## Goal

Current focus: refactor features into opt-in components — axis (3)
channel-adapter abstraction + axis (5) `mattermost/` component
migration, bundled because goal.md L184-186 explicitly sequences
them together ("Mattermost HTTP client, channel/team/bot env knobs,
and the `mattermost_reply` MCP tool all move together"). Land a
`ChannelAdapter` ABC in core (`ap2/channel.py`) with a `post(text,
**meta) -> Result` contract and three core-shipped adapters
(`StdoutChannelAdapter`, `FileAppendChannelAdapter`,
`WebhookChannelAdapter`); git-move `ap2/mattermost.py` →
`ap2/components/mattermost/__init__.py` plus a `manifest.py` whose
`hook_points["channel_adapter"]` returns a `MattermostChannelAdapter`
when `AP2_MM_CHANNELS` is non-empty. The three `_mm_post` call sites
(`daemon.py:1919`, `watchdog.py:90,130`) and the status-report
delivery path call `default_registry().channel_adapters(cfg)` and
route through the registered list. Status-report digest *composition*
stays in core (goal.md L150-152). Existing `AP2_MM_CHANNELS` /
`AP2_MM_TEAM` / `AP2_MM_BOT_USER` env-knob names preserved verbatim
(goal.md L64-67 constraint). The `mattermost_reply` MCP tool keeps
its registered name but its handler moves into the component package.

Why now: without axis 3 + the `mattermost/` migration, "report to
anything other than Mattermost" requires editing `_mm_post` call
sites by hand (goal.md L160-161 delete-test), and the downstream
`attention/` migration (goal.md L188) stays blocked — it publishes
via the channel-adapter abstraction so it cannot proceed until this
lands. The bundling per goal.md L184-186 means deferring one axis
forces deferring the other.

## Scope

- Add `ap2/channel.py` with `ChannelAdapter` ABC (`name: str`;
  `post(text: str, **meta) -> dict | None`) plus three concrete
  adapters: `StdoutChannelAdapter`, `FileAppendChannelAdapter`
  (configurable target path via env), `WebhookChannelAdapter`
  (POSTs JSON to `AP2_WEBHOOK_URL`).
- Extend `ap2/registry.py` `Manifest` with a
  `hook_points["channel_adapter"]` convention and a
  `Registry.channel_adapters(cfg)` accessor that returns the list
  of enabled adapters, deterministically sorted by component name.
- `git mv ap2/mattermost.py ap2/components/mattermost/__init__.py`
  and add `ap2/components/mattermost/manifest.py` declaring
  `env_flag=AP2_MM_CHANNELS` (truthy → enabled;
  `default_enabled=False`), `hook_points` exposing
  `channel_adapter` → `MattermostChannelAdapter` and
  `mcp_tool` → the existing `mattermost_reply` handler.
- Rewire the 3 `_mm_post` call sites (`ap2/daemon.py:1919`,
  `ap2/watchdog.py:90,130`) to walk
  `default_registry().channel_adapters(cfg)` and call `.post(...)`
  on each enabled adapter; status-report digest delivery path
  follows the same pattern.
- Update `ap2/tests/test_core_import_direction.py` `_EXEMPT_FILES`
  if (and only if) a core file legitimately needs to import the
  `ChannelAdapter` ABC from `ap2/channel.py` (which is core, not a
  component — no exemption should be needed).
- Update `ap2/howto.md` with the new channel-adapter docstring
  shape and `AP2_MM_CHANNELS` polarity note.

## Design

Mirror the existing `janitor/` canary shape: subpackage with
`__init__.py` carrying the runtime symbols and `manifest.py`
declaring the `Manifest` dataclass. `ChannelAdapter` is a thin
`abc.ABC` in core (sibling of `ap2/registry.py`), not a Protocol —
the ABC route lets subclasses register via `Manifest.hook_points`
without runtime checks at every call site. `Registry.channel_adapters(cfg)`
filters by `_is_enabled(manifest)` (same polarity rule as tick
hooks) and returns the list of `ChannelAdapter` instances in
deterministic component-name-sorted order so digest delivery to
multiple adapters is reproducible. The Mattermost manifest's
`channel_adapter` factory reads `AP2_MM_CHANNELS` lazily so a
hot-reloaded env file takes effect on the next dispatch pass.
`_mm_post`'s implementation stays in
`ap2/components/mattermost/__init__.py` and becomes the
`MattermostChannelAdapter.post` body — call sites in `daemon.py`
and `watchdog.py` get a small helper (`_deliver(text, **meta)`)
in core that walks the registry's adapter list. The three sibling
adapters in core (Stdout, FileAppend, Webhook) are minimal — they
exist primarily so the digest's default destination is non-null
when Mattermost is disabled (goal.md L156-157); their `post`
bodies are ~10 lines each.

## Verification

- `uv run pytest -q` — full suite passes (no behavior change for
  any operator-observable signal; all existing Mattermost-routed
  tests pass with the new adapter dispatch path).
- `uv run pytest -q ap2/tests/test_core_import_direction.py` — the
  import-direction gate still passes (core does not import from
  `ap2/components/`).
- `test -f ap2/channel.py` — `ChannelAdapter` ABC module lands in
  core.
- `test -f ap2/components/mattermost/__init__.py` — `mattermost`
  subpackage exists.
- `test -f ap2/components/mattermost/manifest.py` — manifest file
  lands.
- `test ! -f ap2/mattermost.py` — flat module removed (git-moved
  into the subpackage).
- `! grep -nE '^from \.mattermost|^from ap2\.mattermost|^import ap2\.mattermost' ap2/daemon.py ap2/watchdog.py` — flat-module imports gone from core.
- `grep -nE 'class +ChannelAdapter' ap2/channel.py` — ABC defined.
- `grep -nE 'class +MattermostChannelAdapter' ap2/components/mattermost/__init__.py` — concrete adapter ships under the component.
- `grep -nE 'class +(Stdout|FileAppend|Webhook)ChannelAdapter' ap2/channel.py` — three core-shipped sibling adapters exist (goal.md L156-157 default-destination requirement).
- `grep -nE 'channel_adapters' ap2/registry.py` — registry exposes the accessor.
- `grep -nE 'AP2_MM_CHANNELS' ap2/components/mattermost/manifest.py` — env-knob name preserved verbatim per goal.md L64-67.
- `! grep -nE 'tools\._mm_post\(' ap2/daemon.py ap2/watchdog.py` — direct `_mm_post` calls eliminated from core.
- `ap2/components/mattermost/manifest.py` Prose: the manifest registers the `mattermost_reply` MCP-tool handler under a named `hook_points` slot so the MCP server discovers it through the registry rather than via direct `from .tools import mattermost_reply` in core; judge confirms via Read of the manifest + the MCP-server build site.
- `ap2/howto.md` Prose: the channel-adapter convention is documented with the `ChannelAdapter.post(text, **meta) -> dict | None` signature and the `AP2_MM_CHANNELS` polarity note; judge confirms via Read/Grep.

## Out of scope

- Adding any non-Mattermost adapter beyond the three core stubs
  (Stdout, FileAppend, Webhook). Real delivery channels are
  separate downstream TB-Ns; this task just lands the abstraction.
- Renaming any operator-visible env knob (goal.md L64-67 forbids).
- Touching the status-report digest *composition* logic in
  `ap2/status_report.py` (goal.md L150-152 keeps composition in
  core).
- The `attention/` subpackage migration (goal.md L188) — separate
  TB-N after this lands.

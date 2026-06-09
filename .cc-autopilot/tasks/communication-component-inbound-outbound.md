# TB-389: Communication component (inbound + outbound) wrapping the channel adapters

## Goal

Advance the focus "get the component boundary right — loop-level participants
only" toward goal.md's Done-when bullet: "Every loop-level autonomous behavior
(auto-approve, auto-unfreeze, attention, focus-advance, janitor, cron, ideation,
communication) lives under ap2/components/<name>/ and is loaded via the component
registry." Extract the channel surface — today split across
`registry.channel_adapters()` (outbound) and `hook_points["inbound_poll"]`
(inbound), with mattermost as a top-level component — into a single
`communication` component that owns BOTH directions as tick-phase work and holds
its channel adapters in an internal registry invisible to core.

Why now: channel multiplicity (mattermost today, slack/email later) is currently a
kernel concern — core walks `channel_adapters()` in four call sites and polls
inbound via a one-off hook_point; folding it behind one component now, while
mattermost is the only channel, is far cheaper than after a second channel
hard-codes the leak deeper.

## Scope

- Introduce `ap2/components/communication/` owning inbound + outbound as tick-phase
  work; it holds its channel adapters (mattermost, future slack/email) in an
  internal registry that core cannot see.
- Make outbound event-driven: the component delivers undelivered notification
  events on its tick pass; remove the synchronous `_deliver(...)` walk over
  `channel_adapters` from core.
- Demote `mattermost` to a channel adapter under the communication component;
  `AP2_MM_CHANNELS` becomes channel-level config.
- Remove the `channel_adapters()` surface and the `inbound_poll` hook from core so
  core never references channels; preserve observable behavior (inbound polling +
  outbound delivery work exactly as today with the component enabled; env knobs
  preserved).

## Design

- Outbound today: `registry.channel_adapters(cfg)` walked by `ap2/daemon.py:2137`
  (attention immediate-push `_deliver`), `ap2/watchdog.py:96`/`125`,
  `ap2/smoke_runner.py:163`, `ap2/components/attention/impl.py:1155`.
- Inbound today: `ap2/daemon.py:2097` `manifest.hook_points.get("inbound_poll")`.
- mattermost is a top-level component (`ap2/components/mattermost/`); after this
  task it is a channel adapter the communication component instantiates internally.
- If TB-387 (generic `contributions(point)` accessor) has already landed, the
  communication component uses it internally for its channel adapters; if not, the
  internal channel registry is the component's own concern either way — the
  invariant is that core stops walking channels.

## Verification

- `test -d ap2/components/communication` — the communication component exists.
- `! grep -rn 'inbound_poll' ap2/daemon.py` — core no longer polls inbound via the one-off hook_point.
- `uv run pytest -q ap2/tests/` — the full suite passes.
- `ap2/components/communication/manifest.py` Prose: the communication component registers inbound + outbound tick-phase hooks and holds its channel adapters (including mattermost) in an internal registry not exposed to core; judge confirms via Read.
- `ap2/daemon.py` Prose: core no longer walks a channel-adapter list for outbound delivery — delivery is event-driven through the communication component's tick pass; judge confirms via Read/Grep across daemon.py, watchdog.py, smoke_runner.py.
- `ap2/components/mattermost` Prose: mattermost is wired as a channel adapter under the communication component rather than a top-level loop participant; judge confirms via Read/Grep.

## Out of scope

- The generic `contributions(point)` accessor (TB-387) — this task coordinates with
  it but does not depend on it.
- The LLM-judge demotion (TB-386), the hook_points/POST_DISPATCH cleanup (TB-388),
  and the ideation component.
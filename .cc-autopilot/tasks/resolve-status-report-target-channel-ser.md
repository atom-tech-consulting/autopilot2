# Resolve status-report target channel server-side — stop posting to town-square

## Goal

This task is anchored in goal.md's `## Done when` criterion: "An operator can point ap2 at a fresh project, paste a goal.md (with Mission + `## Done when`), and walk away for a week without intervention." The cron-driven Mattermost status report is the operator's primary at-a-distance walk-away signal — the message that lands in the operator's chat client every 6 hours saying "here's what changed, here's what needs your attention." If that message lands in the wrong channel (server default town-square instead of the operator's configured `#ap2` channel), the walk-away promise quietly breaks: the operator never sees the report, never notices that something needs their attention, and the daemon's careful audit trail goes to a channel nobody reads.

The bug shape: `ap2/status_report.py:109-110` (the body of `STATUS_REPORT_PROMPT`) instructs the agent to "Post a concise autopilot status report to the mattermost channel identified by `AP2_MM_REPORT_CHANNEL` (or `#autopilot` if unset)." But `AP2_MM_REPORT_CHANNEL` appears nowhere else in the codebase — there is NO daemon-side code that resolves the env var to an actual channel ID and passes the value into the agent's context. The agent (a control-agent SDK run) has no Bash and no env-var access, so it sees the literal string "AP2_MM_REPORT_CHANNEL" in the prompt and treats it as opaque. The fallback `#autopilot` doesn't exist on the server. The agent ends up calling `mattermost_reply` with whatever channel ID it can guess from server defaults — which empirically is town-square (`i9kk5ta84tgn3prbu5xkxtepbw` in recent posts), NOT the configured watched channel (`u4e41y7gr78zupikzus8huw6kr` = `#ap2` per `.cc-autopilot/env`'s inline comment).

The fix shifts channel resolution from "agent reads env var by name" (which can't work) to "daemon resolves the value, passes it via `state_extras`" — same pattern TB-151 (pending-review surfacing), TB-163 (operator rejections), TB-177 (janitor findings), and TB-183 (proposal slot count) use. The agent then reads the resolved ID from the `## Current state` snapshot block at the top of its prompt, not from a hypothetical env-var-name reference.

Why now: the bug is actively breaking every status report today — every 6-hour cron fire AND every chat-triggered report posts to town-square instead of the operator's `#ap2`. The walk-away promise depends on that signal landing where the operator looks. The fix is small and the failure mode is visible.

## Scope

- `ap2/status_report.py::run_status_report` — resolve the target channel ID from `AP2_MM_REPORT_CHANNEL`, falling back to the first entry of `AP2_MM_CHANNELS` (the natural default for single-channel projects: the channel you watch for inbound mentions is where outbound status posts belong). Append to `state_extras` as a single line: `- post target channel: <channel-id>`. When neither env var is set, omit the line — the agent will then `log_event` and skip per the prompt instruction below.
- `ap2/status_report.py::STATUS_REPORT_PROMPT` (lines 109-110) — replace the current "Post...to the mattermost channel identified by `AP2_MM_REPORT_CHANNEL` (or `#autopilot` if unset)" with: "Post...to the channel ID from the `post target channel:` line in the `## Current state` snapshot above. If that line is absent, the operator hasn't configured a status-report target — `log_event(type='status_report', summary='skipped: no AP2_MM_REPORT_CHANNEL or AP2_MM_CHANNELS configured')` and finish. Do NOT guess a channel ID from server defaults or recent inbound `mattermost` events."
- `ap2/tests/test_status_report.py` (or wherever the prompt + state_extras flow is tested) — regression test pinning that (a) when `AP2_MM_REPORT_CHANNEL` is set, the resolved value appears in `state_extras`; (b) when only `AP2_MM_CHANNELS` is set, the first entry is used; (c) when neither is set, no `post target channel:` line is appended and the prompt instructs the agent to skip; (d) the prompt body no longer contains the string "AP2_MM_REPORT_CHANNEL" as a bare reference (regression for the bug shape).

## Design

### Why state_extras, not a new prompt-assembly arg

The `state_extras` mechanism in `ap2/prompts.py::build_control_prompt` is the established channel for daemon-resolved context that needs to reach a control agent. Adding a third-or-fourth consumer (post target channel) keeps the architectural surface stable — no new kwargs, no new prompt sections, agent-readable from the same snapshot block it already reads for `now:`, board counts, recent commits, pending-review TB-Ns, rejections, janitor findings, and proposal slots.

### Why fall back to `AP2_MM_CHANNELS[0]`

The single-channel case is the dominant one in deployment (autopilot2 has one entry; post-train likely the same). The channel watched for inbound mentions is the natural place to send outbound status reports — operators don't typically configure a separate "status-only" channel. Having the fallback keeps the env file simple: setting `AP2_MM_CHANNELS` alone is enough; `AP2_MM_REPORT_CHANNEL` is an optional override for projects that DO want separate inbound and outbound channels.

### Why explicit-skip-on-unset instead of guessing

The current bug is exactly the guess-at-server-default failure mode. When neither env var is set, the right behavior is silent skip with a `log_event` audit, not "post to whatever channel I can find." Operators who haven't configured anything genuinely don't have a target; posting to a guessed channel is worse than posting nowhere (operator can grep events.jsonl for the skip and configure intentionally).

### Channel-ID-only, no name resolution

The `AP2_MM_REPORT_CHANNEL` and `AP2_MM_CHANNELS` env vars carry channel IDs, not channel names — same convention as the existing watch-channel parsing. If operators want to reference channels by name (`#ap2`), the `ap2 sandbox install-channel` CLI helper resolves names to IDs at config time. This task doesn't change that contract; the env value is opaque to the daemon-side resolver, just passed through.

### Backwards compatibility

- Projects with only `AP2_MM_CHANNELS` set: status reports now post to that channel ID instead of town-square (BUG FIX — the intended behavior).
- Projects with `AP2_MM_REPORT_CHANNEL` set: status reports post to that channel (matches the prompt's pre-fix stated intent for the first time).
- Projects with neither set: status reports skip with an explicit audit event (was: posted-to-town-square mystery).
- The existing skip-gate (TB-128, no-activity since last report) is unaffected — it runs BEFORE channel resolution.

### Cost/timing considerations

The state_extras line adds ~30 tokens to every status-report prompt. Status-report cron costs ~$0.05-0.20 per fire; 30 tokens is rounding error at that scale.

## Verification

- `uv run pytest -q ap2/tests/` — full regression gate passes.
- `grep -nE "AP2_MM_REPORT_CHANNEL" ap2/status_report.py` — appears in `run_status_report` env-resolution code AND in the prompt body's instruction (the fix updates both surfaces; both should reference the env name in their respective contexts).
- `grep -nE "post target channel" ap2/status_report.py` — at least one match in the state_extras line construction AND one in the prompt body's instruction to read it.
- `grep -nE "AP2_MM_CHANNELS" ap2/status_report.py` — fallback resolution code references it.
- `grep -nE "#autopilot" ap2/status_report.py` — should return ZERO matches in the prompt body (regression: the dead-letter `#autopilot` fallback string is gone).
- prose: a test in `test_status_report.py` synthesizes a fixture environment with `AP2_MM_REPORT_CHANNEL=channel-foo` set and `AP2_MM_CHANNELS=channel-bar,channel-baz` set; calls `run_status_report` (or its prompt-assembly helper); asserts the `state_extras` list contains `- post target channel: channel-foo` (the explicit env wins over the fallback).
- prose: a test pins the fallback path — `AP2_MM_REPORT_CHANNEL` unset, `AP2_MM_CHANNELS=channel-bar,channel-baz`; asserts `state_extras` contains `- post target channel: channel-bar` (first entry of the fallback list).
- prose: a test pins the unset path — both env vars unset; asserts NO `post target channel:` line is appended to `state_extras`, AND the rendered prompt body contains the explicit skip instruction (greppable phrase like "no AP2_MM_REPORT_CHANNEL or AP2_MM_CHANNELS configured").
- prose: a test pins the dead-letter regression — `STATUS_REPORT_PROMPT` body does NOT contain the literal string `#autopilot` (the bug's fallback that nobody could reach is gone).
- prose: a test pins agent-input integrity — synthesizes a fixture environment with the channel resolved, runs status-report's prompt-assembly through `build_control_prompt`, asserts the rendered prompt's `## Current state` block contains the `- post target channel: <id>` line in a position the agent can read it.

## Out of scope

- Multi-channel status report posting (post to two channels at once). v1 picks one channel; multi-channel routing is a separate concern.
- Channel-name-to-ID resolution. Env values are IDs; name resolution lives in `ap2 sandbox install-channel`.
- Per-event-type channel routing (e.g., janitor findings to one channel, status reports to another). Single channel for status reports per the existing design.
- Renaming `AP2_MM_REPORT_CHANNEL` to something else, or making `AP2_MM_CHANNELS` plural in a different way. Existing env-var names stay.
- Backfilling old town-square posts. They stay where they landed; future posts go to the right place.
- Adding a CLI helper to display the resolved target channel (`ap2 status` could surface "status reports go to: <channel>"). Useful but separate; this task is about fixing the routing.
- Updating the README or skills docs to mention `AP2_MM_REPORT_CHANNEL`. The env var should be self-documenting via the resolved-channel surfacing in `state_extras`; doc updates can come later if the env-knob discoverability becomes an operator complaint.

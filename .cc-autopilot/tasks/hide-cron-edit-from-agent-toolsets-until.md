# Hide cron_edit from agent toolsets until a clear use case lands

## Goal

Remove `mcp__autopilot__cron_edit` from `CONTROL_AGENT_TOOLS` (and the downstream `MM_HANDLER_TOOLS` set, which TB-145 collapsed from the prior FULL/RESTRICTED variants) so it stops being available to cron jobs, ideation, and the MM handler. Cron schedule changes become operator-CLI-only (`ap2 cron edit`) until a workflow that actually needs programmatic cron mutation is designed.

## Why

Surveying current usage of `cron_edit`:

(1) **Task agents** — already don't have it (use `cron_propose` to emit `cron_proposed` events; operator/ideation adopts).
(2) **Status-report cron** — has it via `CONTROL_AGENT_TOOLS`. No documented reason to use it.
(3) **Ideation cron** — uses it to adopt `cron_proposed` events from task agents (the only programmatic write path that fires today in practice).
(4) **MM handler when idle** — has it for "@claude-bot edit cron schedule X" operator chat commands. Niche, has CLI alternative. (TB-145 will drop this regardless.)

So the only in-workflow programmatic use is ideation auto-adopting cron proposals. This bypasses the operator-in-the-loop pattern TB-121 establishes for ideation-proposed *tasks* — which require `ap2 approve` to dispatch. Today, ideation can adopt a cron proposal a task agent emitted without any human review. That asymmetry is hard to defend: if proposed tasks need a gate, proposed crons probably should too.

Removing `cron_edit` from agent toolsets:
- Task agents continue to use `cron_propose` (no change).
- Ideation can still SEE `cron_proposed` events and re-surface them to the operator (e.g., in its per-cycle assessment), but cannot mutate cron.yaml.
- Operator runs `ap2 cron edit ...` to adopt — same gate as today's manual adoption path, just now exclusive.
- Status-report and any future crons lose a tool they weren't using anyway.

If a future workflow legitimately needs programmatic cron mutation (e.g., ideation auto-tuning poll cadence based on activity), we re-add `cron_edit` then with explicit justification + a corresponding gate.

## Scope

(1) Remove `mcp__autopilot__cron_edit` from `CONTROL_AGENT_TOOLS` (in `ap2/tools.py`). The downstream `MM_HANDLER_TOOLS` set already filtered it out post-TB-145, so the cascade is a defensive no-op there; the load-bearing change is dropping it from `CONTROL_AGENT_TOOLS` itself.
(2) Update the `cron_edit` MCP tool's docstring to say "operator-CLI use via `ap2 cron edit`; not exposed to control agents (post-TB-146). Use `cron_propose` for agent-side proposals."
(3) Update the ideation prompt (`ap2/ideation.default.md`) to drop any instructions that reference adopting `cron_proposed` events via `cron_edit`. Replace with: "Surface unadopted cron proposals in your per-cycle assessment so the operator sees what's pending; do not adopt them yourself."
(4) Update `prompts.py` control-agent prompt header text that lists available tools — drop `cron_edit` from the explainer.
(5) Document the change in the architecture / readme so future contributors don't try to use it from agents.

## Verification

- `uv run pytest -q ap2/tests/` — full regression gate passes (gating)
- `python3 -c "from ap2.tools import CONTROL_AGENT_TOOLS, MM_HANDLER_TOOLS, TASK_AGENT_TOOLS; assert 'mcp__autopilot__cron_edit' not in CONTROL_AGENT_TOOLS + MM_HANDLER_TOOLS + TASK_AGENT_TOOLS"` — `cron_edit` is absent from every agent toolset. (TB-145 retired `MM_HANDLER_TOOLS_FULL`/`MM_HANDLER_TOOLS_RESTRICTED`; the canonical singular set is `MM_HANDLER_TOOLS`.)
- New unit test in `test_tools.py`: `CONTROL_AGENT_TOOLS` does NOT contain `mcp__autopilot__cron_edit`.
- New unit test in `test_ideation_defaults.py`: the ideation prompt does NOT instruct the agent to call `cron_edit`. Pin via grep that the rendered prompt mentions `cron_propose` events should be SURFACED (not adopted) in assessments.
- Existing `test_tools.py`: the `cron_edit` MCP tool's wiring stays intact (CLI still uses it via `cmd_cron_edit` if such a command exists; otherwise it stays callable from Python). Verify by direct call test.
- The diff updates `architecture.md` and any README sections that previously listed `cron_edit` as an agent tool.

## Out of scope

- Removing the `cron_edit` MCP tool entirely. The operator CLI may still wrap it (or call `do_cron_edit` directly); pulling the tool would force CLI restructuring. Out-of-scope here; can be a follow-up if the CLI never wraps it.
- Adding a new gate for ideation-adopted cron proposals (analogous to TB-121's `ap2 approve` for ideation-proposed tasks). With this change ideation can no longer adopt at all, so the gate isn't needed yet.
- Any change to `cron_propose` (the agent-side proposal path) — that stays as TB-123 designed it.

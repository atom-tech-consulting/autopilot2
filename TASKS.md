# Tasks

## Active


## Ready


## Backlog

- [ ] **TB-121** **Gate ideation-proposed tasks behind human review before dispatch** `#autopilot` `#ideation` `#safety` — Stop the daemon from autonomously dispatching tasks ideation just invented. Ideation emits each proposed task with (blocked on: review); auto-promotion already skips blocked tasks. New 'ap2 approve TB-N' CLI strips the clause atomically. Watchdog learns to distinguish 'pending review' from 'daemon broken'. Reuses existing blocker-clause primitive instead of adding a 7th board section or overloading #proposed. [→ brief](.cc-autopilot/tasks/gate-ideation-proposed-tasks-behind-huma.md)
- [ ] **TB-122** **Concurrent Mattermost handler with restricted toolset during in-flight tasks** `#autopilot` `#mattermost` `#concurrency` — Split daemon main_loop into _main_tick_loop + _mm_loop so MM polling doesn't block on long task agents. While a task is in flight, the handler gets MM_HANDLER_TOOLS_RESTRICTED — keeps reads + board_edit + mattermost_reply + log_event + daemon_control, drops cron_edit + ideation_state_write. Operator can pause / add / delete / freeze tasks mid-flight; can't mutate cron schedule or ideation state. Reuses the SDK allowlist primitive. [→ brief](.cc-autopilot/tasks/concurrent-mattermost-handler-with-restr.md)
- [ ] **TB-123** **Promote cron proposal to a task-agent MCP tool, drop report_result.cron** `#autopilot` `#mcp-tools` `#cron` — Replace report_result's stringified cron list with a dedicated mcp__autopilot__cron_propose(name, schedule, prompt, rationale) MCP tool. Decouples cron proposal from result reporting: better discoverability, structured args (no JSON-in-string), per-proposal events with rationale, and failure isolation (malformed cron no longer crashes result parsing). Symmetric with control agents' cron_edit. [→ brief](.cc-autopilot/tasks/promote-cron-proposal-to-a-task-agent-mc.md)

## Pipeline Pending


## Complete


## Frozen

- [ ] **TB-119** **Switch Board parser from regex to mistune AST + explicit emitter** `#autopilot` `#board` `#parser` — Finish TB-102 by moving Board's four regexes (SECTION_RE, TASK_LINE_RE, _BLOCKED_CLAUSE_RE, _BLOCKED_TOKEN_SPLIT_RE) onto mistune AST; keep emit through Task.render(). Frozen — preventive, not urgent; malformed-line event already covers the worst silent-failure mode. [→ brief](.cc-autopilot/tasks/switch-board-parser-from-regex-to-mistun.md)
- [ ] **TB-120** **Kernel-level fence on daemon-owned files via split sandbox users (ap2-control + ap2-task)** `#autopilot` `#sandbox` `#permissions` — Move the daemon-owned-file fence from application-layer (prompt + SDK disallowed_tools) to OS-level by splitting claude-agent into ap2-control (daemon/cron/ideation/MM) and ap2-task (task agents only); daemon-owned files become 0644 ap2-control-owned so task agents get Permission denied at the kernel for any write path. Frozen because current two-layer fence covers the non-adversarial failure mode (TB-144); thaw on observed Bash bypass, multi-tenant briefings, or unrelated MCP-transport refactor. [→ brief](.cc-autopilot/tasks/kernel-level-fence-on-daemon-owned-files.md)

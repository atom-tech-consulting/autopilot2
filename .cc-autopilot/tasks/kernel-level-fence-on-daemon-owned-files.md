# Kernel-level fence on daemon-owned files via split sandbox users

## Goal

Move the daemon-owned-file fence from application-layer (prompt + SDK
`disallowed_tools` Edit/Write) to OS-level. Split the current single
`claude-agent` sandbox into two users:

- **`ap2-control`** — runs the daemon, cron jobs, ideation, mattermost
  handlers.
- **`ap2-task`** — runs task agents only.

Daemon-owned files (`TASKS.md`, `CLAUDE.md`, `goal.md`,
`.cc-autopilot/{progress.md,events.jsonl,ideation_state.md,cron.yaml}`)
owned by `ap2-control`, mode 0644 so `ap2-task` reads but cannot write.
Task agents get "Permission denied" at the kernel for any write attempt
— through `Bash`, `Edit`, `Write`, or any other tool. The fence becomes
unbypassable by definition.

## Driver

Today's fence catches direct `Edit("goal.md", …)` but Bash workarounds
bypass it: `echo > goal.md`, `sed -i …`, `python -c
"open(…).write(…)"`. AST-parsing Bash to detect writes is a losing
arms race. Permission bits at the kernel are the right destination.

## Frozen because

The current two-layer fence (prompt fence in `prompts._TASK_HEADER` +
SDK `disallowed_tools` Edit/Write block in `run_task`) covers the
non-adversarial failure mode that actually surfaced — TB-144 ideation
feedback loop with goal.md. Bash bypass requires a deliberately
misbehaving agent, and we haven't seen one.

This is high-effort defensive infrastructure for a threat model that
has not materialized.

## Thaw triggers

Unfreeze when ANY of:

1. A task agent observed bypassing the SDK fence via Bash (event
   evidence, not theoretical).
2. A multi-tenant scenario where untrusted task briefings need stronger
   isolation — e.g. running ap2 against contributor PRs, or accepting
   briefings from outside the operator.
3. The MCP transport refactor (Hard Part 1 below) becomes attractive
   for an unrelated reason — e.g. moving the autopilot MCP server into
   a separate process for stability or hot-reloads.

## Hard parts

1. **Out-of-process MCP transport.** The daemon currently passes its
   MCP server in-process via
   `mcp_servers={"autopilot": mcp_server}` to `ClaudeAgentOptions`. If
   the SDK subprocess runs as a different user, the in-process server
   object isn't reachable. Forces a refactor to out-of-process MCP —
   unix socket or HTTP transport, with auth so `ap2-task` can call
   `pipeline_task_start` against the `ap2-control`-hosted server. This
   is the bulk of the engineering.

2. **Per-query sudo plumbing.** The daemon spawns
   `sudo -u ap2-task claude …` for task queries with a NOPASSWD grant
   from `ap2-control → ap2-task`. Inverse of the human → `claude-agent`
   grant that `ap2 sandbox user-setup` already installs. Plus shell
   env propagation (token, PATH).

3. **Cross-user git.** Task agents commit code as `ap2-task`; the
   daemon (as `ap2-control`) commits state files separately — already
   the application-layer pattern, but `git`'s `safe.directory` config
   plus working-tree ownership semantics need to be sorted out
   per-user. Likely needs a setup step that establishes both users on
   each project clone.

## Scope

- Add `ap2 sandbox dual-user-setup` (or extend `user-setup` with a
  flag) to provision both users + the cross-user sudo grant.
- Refactor the MCP server to a transport-aware shape — keep the
  in-process variant for tests and single-user mode; add an
  out-of-process variant for split-user mode.
- Update `run_task` to spawn the SDK subprocess as `ap2-task`.
- Update `_commit_state_files` and any other daemon writes to keep
  daemon-owned file ownership at `ap2-control:ap2-control` 0644.
- Doctor: verify the split, file ownership, sudo grant.
- Migration path for existing single-user installs (rename
  `claude-agent → ap2-control`? coexist? require fresh setup?).

## Out of scope

- Task agents writing to `.cc-autopilot/insights/` — those are
  task-agent-owned. Keep them writable.
- Task agents writing to source code — that's the whole point of task
  agents.
- Sandboxing the daemon itself further (e.g. capabilities, seccomp).
  This task is about cross-agent isolation, not host hardening.

## Verification (apply at thaw)

- [shell] `getent passwd ap2-control ap2-task` (both users exist)
- [shell] `stat -c '%U:%G %a' /Users/ap2-control/repos/<proj>/TASKS.md`
  → `ap2-control:ap2-control 644`
- Task agent in test fixture attempts `bash -c 'echo x > TASKS.md'` →
  exit non-zero, file unchanged. (gating)
- Task agent in test fixture commits a source file edit → succeeds,
  HEAD includes the diff. (gating — proves the split doesn't break
  the happy path)
- `pipeline_task_start` MCP call from `ap2-task` reaches the
  `ap2-control`-hosted server. (gating — proves the out-of-process
  transport works)
- All 423+ default tests still green. Smokes (real-SDK) green.

## Decision log

- 2026-04-29 (this briefing): Captured from TB-100 in stoch's previous
  board. Filed as Frozen on autopilot2's board. The application-layer
  fence (prompt + `disallowed_tools`) is doing the job for the threat
  model we actually face. Thaw on observed bypass or new requirement
  (multi-tenant briefings, MCP-transport refactor for unrelated
  reasons).

# ap2 architecture

Technical design for the autopilot v2 daemon. Companion to [`README.md`](README.md), which is the operator quickstart and reference.

## Design principles

These three constraints drive every other decision in the codebase.

**1. Each unit of work runs in a fresh SDK `query()` call.** A task agent gets a clean context with its briefing + recent events; it never sees other tasks' working memory. Same for the cron, mattermost, and ideation agents. This is the answer to v1's compaction-fatigue problem: long-running Claude Code sessions degraded as their context filled, and post-compaction agents lost track of what they had been doing. v2 has nothing to compact — the daemon is a Python scheduler, not a Claude session.

**2. Shared awareness lives in files, not in any agent's context.** `TASKS.md`, `events.jsonl`, `progress.md`, `cron_state.json`, the briefings, and `ideation_state.md` are all on disk. Each spawned agent gets the relevant files inlined into its prompt — typically a briefing + a tail of `events.jsonl`. No state crosses query boundaries via memory.

**3. Mutations go through narrow tools.** Control agents can write to the board, but only via the `board_edit` MCP tool — no `Write`/`Edit` access to `TASKS.md`. Same pattern for `ideation_state_write`, `pipeline_task_start`. Cron schedule mutation has no agent-facing tool at all (TB-146): the `cron_edit` MCP handler exists for the operator CLI's use only and is not exposed in any agent toolset. Broad reads, narrow writes. This keeps mutation paths auditable (each emits a structured event) and makes accidental clobbering impossible without going out of band.

## The daemon loop

`daemon.main_loop` (TB-122) runs two concurrent asyncio coroutines via `asyncio.gather`:

**`_main_tick_loop`** — scheduled work, default 30s (`AP2_TICK_S`). The tick is **phase-walked**: the component registry (see **Component model**) defines a `Phase` vocabulary, and `_tick` walks `default_registry().tick_hooks(<phase>)` at each stage — interleaving those component hooks with the core steps that stay inline (operator-queue drain, pipeline-pending sweep, task dispatch). Each registered hook self-gates on its component's `env_flag` and owns its own error surface, so the walk is a uniform iteration over both sync and async hooks (the daemon `await`s any coroutine a hook returns):

```
_tick(cfg, sdk, mcp_server):
  0a.  env hot-reload + effective-config snapshot                      (core)
  0.   operator-queue drain → commit applied ops                       (core)
  0.5  Phase.PRE_DISPATCH       — auto_unfreeze sweep, ideation roadmap-
                                  exhaustion halt, auto_approve pass
                                  (deterministic name-sorted order:
                                   auto_approve < auto_unfreeze < ideation)
  0.7  Phase.ATTENTION_EMISSION — attention detector → attention_raised events
                                  (before cron so a status-report fire sees them)
  1.   Phase.CRON_DISPATCH      — cron scheduler component: load_jobs + due_jobs,
                                  resolve each job.name to its registered handler
  2.   pipeline-pending sweep   — _sweep_pipeline_pending                (core)
  3.   task dispatch            — board.next_ready, else iterate dispatchable
                                  Backlog past auto-approve-gated heads → run_task
                                  (the per-task promote-time gate is inline)  (core)
  3.9  Phase.IDEATION           — ideation component: operator-forced force_ideate
                                  (hook-point), then natural empty-board _maybe_ideate
  5.   idle watchdog            — _maybe_auto_diagnose                    (core)
  6.   Phase.COMMUNICATION      — communication component: drain the `ap2.notify`
                                  queue → deliver outbound to internal channels
```

`Phase.POST_CRON` is in the vocabulary but `_tick` does **not** walk it — the cron scheduler owns the janitor cron job's invocation cadence (it resolves janitor's handler from the cron job-handler registry), so janitor's tick-callable is registered on `POST_CRON` only to keep the registry's phase-keyed view complete. `Phase.POST_DISPATCH` was **removed** (TB-388): its sole registrant was an auto_approve placeholder, which TB-383 promoted to a real `PRE_DISPATCH` loop pass, leaving the phase walked-but-empty every tick; the per-task dispatch-time auto-approve gate stays inline in step 3.

**`_mm_loop`** — Mattermost polling, default 10s (`AP2_MM_TICK_S`):

```
_mm_loop(cfg, sdk, mcp_server):
  - _check_inbound_messages → resolves the communication component's
    `poll_inbound` hook (TB-389 — core no longer walks a channel list)
    → asyncio.create_task(handle_message(...)) per mention
  - Every handler runs with the SAME fixed toolset (TB-145):
      MM_HANDLER_TOOLS  (CONTROL_AGENT_TOOLS minus
                         ideation_state_write, board_edit)
    Note: `cron_edit` is no longer in CONTROL_AGENT_TOOLS at all
    (TB-146 — operator-CLI-only via `ap2 cron edit`); the explicit
    filter in MM_HANDLER_TOOLS is kept as a defense-in-depth no-op.
    No board snapshot is taken at handler-spawn time — the previous
    TB-122 FULL/RESTRICTED toggle was a TOCTOU race against the
    main tick loop and was retired in TB-145.
```

The two loops share the same `Config`, SDK handle, and MCP server. Board mutations go through `locked_board()` (fcntl.flock), which serializes concurrent access. The pause flag (`<root>/.cc-autopilot/paused`, presence-only) short-circuits both loops.

Stages in `_tick` run sequentially. A failure in most stages emits an event and continues to the next — one broken cron job doesn't block task dispatch. Most stages are wrapped in try/except; the `PRE_DISPATCH` / `ATTENTION_EMISSION` walks intentionally are not, because each hook owns its own error surface (e.g. `auto_unfreeze_skipped reason=sweep_error`) and a uniform handler would mask that observable shape. Each loop body is wrapped so the daemon never exits on an unhandled error.

## Component model

The loop subsystems that used to be flat top-level modules wired into `_tick` by direct import are now **components**: opt-in subpackages under `ap2/components/<name>/`, each a `manifest.py` + `impl.py` pair, discovered and dispatched through a registry. Today's components: `attention`, `auto_approve`, `auto_unfreeze`, `janitor`, `cron`, `communication`, `ideation`.

**The registry (`registry.py`).** `Registry.discover()` walks `ap2/components/*/manifest.py` via `pkgutil.iter_modules` and reads each module's `MANIFEST` attribute — there is **no hardcoded list of component names** anywhere in core. A future migration ships a subpackage with a `MANIFEST` and the registry picks it up with zero registry-side edits. `default_registry()` caches the discovered set per process.

**`Manifest`.** A frozen dataclass declaring one component's shape: `name`, `env_flag`, `default_enabled`, `hook_points` (a `dict[str, Callable]` of named hooks the component provides), `tick_hooks` (a list of `(Phase, TickHook)` pairs), `dependencies`, and a `config_schema` (per-component TOML knobs). `tick_hooks(phase)` assembles the ordered hook list across all manifests for that phase, name-sorted by component for determinism.

**`env_flag` polarity.** `Manifest.is_enabled()` is the single source of truth (shared by the registry's enabled-walk and the `ap2 status` `## Components` enumeration):

- `env_flag is None` → always-on (subject to `default_enabled`). Only `attention` and `communication` are always-on today.
- `env_flag` set with `default_enabled=True` → the env var is a **kill switch** (suppress polarity): a truthy value DISABLES. The conventional shape is `*_DISABLED` (e.g. `AP2_JANITOR_DISABLED`, `AP2_CRON_DISABLED`, `AP2_AUTO_UNFREEZE_DISABLED`).
- `env_flag` set with `default_enabled=False` → the env var is an **opt-in toggle** (require polarity): a truthy value ENABLES (e.g. `AP2_AUTO_APPROVE`).

**Import-direction CI gate.** Core never statically imports `ap2/components/` — a component may import core freely (component → core), but core → component is forbidden. `tests/test_core_import_direction.py` AST-walks every `.py` under `ap2/` (except `ap2/components/` and `ap2/tests/`) and fails the build on any `Import` / `ImportFrom` referencing `ap2.components`. The only exempt path is `registry.py`, whose discovery uses dynamic `importlib.import_module(...)` (a `Call`, not a static import, so the gate is quiet by construction). This is what keeps the cleavage from eroding silently and is the prerequisite for an eventual OSS cut.

**The generic `contributions(point)` accessor.** A single fan-out accessor replaces the registry's former bespoke per-kind methods (`channel_adapters()`, `briefing_validators()`, `verifier_judge()` — all removed). It walks every manifest's `hook_points.get(point)` in name-sorted order and merges: dict-shaped points dict-merge (later manifests win on a key collision), list/scalar points list-merge. It is **fan-out only** — the registry assembles and returns the merged contributions and performs **no keyed dispatch**. Keying stays consumer-local: the cron scheduler does `handlers.get(job.name, DEFAULT)` itself. A surface earns a `contributions(point)` only when multiple owners feed it AND it stays in core (cron job handlers — fed by core + the janitor component).

**The loop-level boundary principle.** A component is a **top-level loop participant** — a tick phase, or a coarse loop surface (inbound/outbound communication). Three corollaries:

- **Sub-step leaves are adapters, not components.** An LLM judge invoked only as an internal sub-step of a deterministic core runner (the verification prose-judge, the briefing dep-coherence judge) is not a loop participant; it is an `AgentAdapter` the runner resolves via `select_adapter(...)`, not a component. (See **Judges are adapters, not components**.)
- **Internal multiplicity lives inside its owning component.** Multiple cron jobs live behind the one cron scheduler; multiple communication channels live inside the communication component. The core surface sees one phase, not the fan-out.
- **A surface owned wholly by one component is internal to it**, never a core extension point — which is why `channel_adapters()` was removed (channels are wholly the communication component's) while `cron_job_handlers` stays a `contributions(point)` (fed by both core and the janitor component).

### Cron: scheduler + job-handler registry

Cron is two layers. The **scheduler** is the `cron` component (`Phase.CRON_DISPATCH`): it owns *when* jobs run — `load_jobs` → `due_jobs` → per-job dispatch + the `cron_*` lifecycle events. *What* each job does is a **registered handler**, resolved by name from a cross-component registry that overlays `registry.contributions("cron_job_handlers")` (today only the janitor component's `{"janitor": …}`) on top of `cron_handlers.CORE_CRON_HANDLERS` (`status-report`, `real-sdk-smoke`), falling through to `cron_handlers.DEFAULT_CRON_HANDLER` (the generic LLM-cron path). This data-driven lookup replaced the pre-component `if job.name == …` switch. The reusable interval-engine primitives (`CronJob`, `parse_interval`, `load_jobs`, `due_jobs`, `mark_run`, …) stay in core `ap2/cron.py` because core consumers depend on them and the import-direction gate forbids core from importing the component; the scheduler component drives that core library on the tick.

### Communication: one component owns both directions

The `communication` component owns inbound + outbound as tick-phase work and holds its channel adapters (Mattermost today; Slack / email later) in an **internal** registry (`communication.channels.channel_registry`) that core cannot see. Outbound is event-driven: a core call site appends to the `ap2.notify` queue (`notify.py` — a pure filesystem write, no `ap2.components.*` import), and the component's `Phase.COMMUNICATION` tick hook drains and delivers. Inbound: `_mm_loop` resolves the component's `poll_inbound` hook. The former core surface — `registry.channel_adapters()` and a one-off `inbound_poll` core walk — is **gone** (TB-389); channel multiplicity is no longer a kernel concern. (`ap2/components/mattermost/` survives as a channel-adapter subpackage with no `manifest.py`, so the registry does not discover it as a top-level component; the communication component imports its handlers internally.)

### Judges are adapters, not components

Neither LLM judge is a component:

- The **verification prose-judge** (`verify._judge_prose_bullet`) is an internal sub-step of the deterministic `verify_task` runner.
- The **briefing dependency-coherence judge** (`briefing_validators._check_dependency_coherence` / `_judge_dep_coherence_default`) is an internal sub-step of the deterministic `_validate_briefing_structure` runner.

TB-382 / TB-316 had briefly modeled these as `verifier_judge/` and `validator_judge/` components; TB-386 demoted both back into their core runners because a sub-step leaf is not a loop-level participant. Each still resolves its backend via `select_adapter("verifier_judge", cfg)` / `select_adapter("validator_judge", cfg)` (see **Agent backends**) — the adapter seam stays; only the redundant component wrapper is gone. Each has a config off-switch (`AP2_VERIFY_JUDGE_DISABLED` / `AP2_VALIDATOR_JUDGE_DISABLED`) so a deployment can verify with shell bullets / structural checks alone; with the judge disabled, prose bullets fall through to the non-gating `unverified` path while the deterministic checks still gate. The pipeline subsystem (`pipeline_sweep.py`, `pipeline_task_start`) likewise stays in core — it is loop plumbing, not an opt-in component.

## Agent kinds

There are four kinds of SDK queries, each with its own prompt builder, tool allowlist, and lifecycle event vocabulary. Each dispatch is routed through the backend-adapter layer — `select_adapter(kind, cfg)` resolves the kind's backend (see **Agent backends**) — rather than calling `claude_agent_sdk` directly.

| Kind | Trigger | Prompt builder | Tools | Timeout |
|---|---|---|---|---|
| **Task** | `run_task` (step 3) | `prompts.build_task_prompt` | `TASK_AGENT_TOOLS` (Read/Edit/Write/Bash + `pipeline_task_start`) | `AP2_TASK_TIMEOUT_S` (1200s) |
| **Cron** | `run_cron` (step 1, `Phase.CRON_DISPATCH`) | `prompts.build_control_prompt` | `CONTROL_AGENT_TOOLS` (board/mm/log_event/daemon_control/ideation_state_write — `cron_edit` dropped TB-146) | `AP2_CONTROL_TIMEOUT_S` (1200s) |
| **Mattermost** | `handle_message` (`_mm_loop`) | `prompts.build_mattermost_prompt` | `MM_HANDLER_TOOLS` (CONTROL_AGENT_TOOLS minus ideation_state_write/board_edit — TB-145, was TB-122's RESTRICTED; `cron_edit` separately dropped from CONTROL_AGENT_TOOLS in TB-146) | `AP2_CONTROL_TIMEOUT_S` |
| **Ideation** | `_maybe_ideate` (step 3.9, `Phase.IDEATION`) | `prompts.build_control_prompt` + `ap2/ideation.default.md` body | `IDEATION_TOOLS` (CONTROL_AGENT_TOOLS minus operator_queue_append; TB-291) | `AP2_CONTROL_TIMEOUT_S` |

Task agents are the only kind that gets `Write`/`Edit`. They commit code; everything else mutates state through MCP tools.

Ideation and cron share the same prompt builder (`build_control_prompt`) — the framing is `## Control job: <name>`, deliberately neutral on whether the run is on a schedule. Ideation has its own lifecycle and event vocabulary on top of that shared prompt (see "TB-98" below).

### Shared SDK plumbing

`daemon._run_control_agent(label, prompt, allowed_tools, max_turns)` is the shared dispatch plumbing for cron + ideation. It does:

- `_prep_debug_dumps(label)` — write the prompt to `.cc-autopilot/debug/<ts>-<label>.prompt.md`.
- `select_adapter(<kind>, cfg)` — resolve the control surface's backend (see **Agent backends**) and drive the streaming `adapter.run(...)` instead of calling `claude_agent_sdk` directly. Under the default map the resolved adapter is a `ClaudeCodeAdapter` wrapping the daemon's already-imported `sdk` handle, so behavior is unchanged.
- `_make_stderr_sink()` — 200-line ring buffer passed via the normalized `AgentOptions.stderr` (the Claude adapter threads it onto `ClaudeAgentOptions.stderr`) so an opaque SDK subprocess crash leaves us a tail to diagnose.
- A bounded consume (`timeout=cfg.control_timeout_s`) — the adapter's `run_to_result` owns the `asyncio.wait_for` wrapper that used to live inline here.
- Returns `(timed_out, error, stderr_tail, prompt_dump)`.

The caller owns the surrounding event vocabulary (`cron_*` for `run_cron`, `ideation_*` for `_maybe_ideate`), the cooldown bookkeeping (`mark_run`), and the state commit. This split is what keeps ideation off the `cron_*` event channel without duplicating the SDK plumbing.

`run_task` doesn't use `_run_control_agent` because it has a salvage path: on timeout or crash, `_infer_result_from_head` checks `git log` for a commit prefixed with the task ID, and if found, treats the task as completed (the agent committed before the SDK subprocess died). That branch is too divergent to share cleanly.

## Agent backends

Every agent dispatch flows through a backend-agnostic adapter layer
(`ap2/adapters/`) rather than calling `claude_agent_sdk` directly. The
`AgentAdapter` ABC (`ap2/adapters/base.py`) is the single seam: a caller hands
it a `prompt`, an `AgentTools` (allow/deny tool policy + MCP servers), and a
normalized `AgentOptions` (model / effort / max_turns / timeout / cwd /
permission mode / stderr), and `run()` yields normalized `AgentEvent`s — one per
backend stream envelope — ending in a terminal `AgentResult` (status / text /
usage). The concrete `run_to_result()` drains that stream and folds in the
per-run `asyncio.wait_for` timeout / error handling the daemon used to inline
(`status="timeout"` / `"error"`).

Two concrete adapters implement the contract:

- **`ClaudeCodeAdapter`** (`backend = "claude"`, the default) — wraps
  `claude_agent_sdk.query()` against the bundled Claude Code binary, reproducing
  the daemon's original consume loop bit-for-bit (same `ap2.message_dump`
  summary/full/text triple feeding `.stream.jsonl` / `.messages.jsonl`, same
  usage derivation). It is the behavior reference.
- **`CodexAdapter`** (`backend = "codex"`) — drives OpenAI's Codex agent via the
  official `openai_codex` SDK (`thread_start` → `turn` → notification stream),
  normalizing each codex `Notification` into the same `AgentEvent` shape so the
  cost guards / `task_run_usage` emission / `ap2 status` read one usage record
  regardless of backend.

`select_adapter(kind, cfg)` (`ap2/adapters/select.py`) returns the adapter
instance backing an agent kind, reading the merged backend id from
`Config.get_agent_backend(kind)`. Resolution order, high → low:

1. `AP2_AGENT_BACKEND_<KIND>` env override (`<KIND>` upper-cased — e.g. the
   `task` kind reads `AP2_AGENT_BACKEND_TASK`).
2. The `[agent_backends]` TOML table (e.g. `task = "codex"`).
3. `DEFAULT_AGENT_BACKEND = "claude"`.

An unknown / typo'd backend id degrades to the Claude adapter rather than
crashing dispatch, so a default install behaves exactly as it did before the
adapter layer existed. Selection is per-kind across the canonical `AGENT_KINDS`
inventory (`task`, `ideation`, `status_report`, `cron`, `mattermost`, plus the
`verifier_judge` / `ideation_scrub` / `validator_judge` / `janitor_judge`
component calls), so an operator can route, say, `task` to codex while
everything else stays on claude.

Because the Claude SDK's in-process `create_sdk_mcp_server` is Claude-specific,
the codex adapter exposes ap2's custom tools (`report_result`, `cron_propose`,
…) to a live Codex agent over an **external stdio MCP bridge**: `CodexAdapter`
translates the tool policy into a `mcp_servers` config entry that launches
`python -m ap2.mcp_stdio` (`ap2/mcp_stdio.py`, serving the same tool set) and
merges it into `thread_start(config=...)`. Both adapters record their registered
tool short-names so `registered_tool_names()` enumerates one identical toolset
across backends.

The daemon-start auth gate walks the resolved backend set and refuses to start
unless every referenced backend's credentials are present (Claude:
`CLAUDE_CODE_OAUTH_TOKEN`; Codex: `OPENAI_API_KEY` or a `~/.codex/auth.json`
ChatGPT-login session). See the **ap2-config** skill
(`skills/ap2-config/SKILL.md`) for the operational config (the
`[agent_backends]` table, the `AP2_AGENT_BACKEND_<KIND>` override, per-backend
auth, and the daemon-start gate).

## Task lifecycle

A task moves through the board sections:

```
Backlog → Ready → Active → Complete  (happy path; auto-promotion at the
                                       Backlog→Ready boundary)
              ↓        ↓
         (skipped     Backlog (status: blocked / failed)
          if blocked      ↓
          on TB-X)    Frozen (after AP2_MAX_RETRIES)
```

`run_task`:
1. `move_to_active` (board lock).
2. Build the prompt: header + briefing + recent events + RESULT format spec.
3. Dispatched through `select_adapter("task", cfg)` → `adapter.run(...)` (a `ClaudeCodeAdapter` over `sdk.query()` by default; see **Agent backends**) and consumed turn-by-turn; messages dumped to `.stream.jsonl` + `.messages.jsonl` for diagnosis (TB-85).
4. Capture the agent's `report_result(...)` MCP tool call — `status` + `commit` + `summary` + `files_changed` + `tests_passed` + optional `cron` list. If the agent didn't call it, daemon sets `status="unknown"` and routes through HEAD-recovery (step 7).
5. Two-tier verify:
   - Per-task verification (`verify.verify_task`) runs the briefing's `## Verification` bullets — shell bullets via subprocess, prose bullets via SDK judge.
   - Project-wide gate (`AP2_VERIFY_CMD`, e.g. `uv run pytest -q`) runs after the per-task verify. `#no-verify` tag opts out.
6. `move_to_complete` on success, `move_to_backlog` on `blocked` (with a Retry counter increment), `move_to_frozen` after `AP2_MAX_RETRIES`.
7. `state_commit._commit_state_files` stages + commits all daemon-owned files with subject `state: TB-N → <section>`.

Failure paths (`task_timeout`, `task_error`) try `_infer_result_from_head` first — if the agent committed before the crash, we keep the work and emit `task_implicit_commit` (with `reason=timeout_recovered` / `error_recovered`). This is what unstuck stoch's TB-58/TB-59 retry loops where the agent kept re-doing already-committed work.

## State files and ownership

| File | Owner | Lock | Committed |
|---|---|---|---|
| `TASKS.md` | daemon (via `do_board_edit`) | `fcntl.flock` per-board mutation | yes (state-file commits) |
| `.cc-autopilot/events.jsonl` | daemon + tools (append-only) | none (line-atomic write) | no (gitignored) |
| `.cc-autopilot/progress.md` | daemon (`_append_progress`) | none (single-writer) | yes |
| `.cc-autopilot/cron.yaml` | operator (via `ap2 cron edit` → `do_cron_edit`); no agent toolset has `cron_edit` (TB-146) | none (single-writer) | yes |
| `.cc-autopilot/cron_state.json` | daemon (`mark_run`) | `fcntl.flock` | no (gitignored) |
| `.cc-autopilot/retry_state.json` | daemon | `fcntl.flock` | no |
| `.cc-autopilot/mm_state.json` | daemon | none (single-writer) | no |
| `.cc-autopilot/auto_diagnose_state.json` | daemon | none | no |
| `.cc-autopilot/operator_queue.jsonl` | CLI / MM-handler (via `do_operator_queue_append`); not fenced from task agents (TB-141) | `board_file_lock` covers _allocate_id + queue append for add ops | no (gitignored) |
| `.cc-autopilot/operator_queue_state.json` | daemon (drain bookkeeping; applied uuids) | none (single-writer) | no (gitignored) |
| `.cc-autopilot/ideation_state.md` | ideation agent (via `ideation_state_write`) | atomic write (tmpfile + rename) | yes |
| `.cc-autopilot/tasks/<TB-N>.md` | operator + ideation + `do_board_edit` | none | yes |
| `.cc-autopilot/insights/<topic>.md` | task agents + operator | none | yes |
| `.cc-autopilot/insights/_index.md` | daemon (`maybe_regenerate_index`) | none | yes |
| `.cc-autopilot/pipelines/<name>-<pid>.log` | detached pipeline subprocess | none | gitignored |
| `.cc-autopilot/debug/<ts>-<label>.{prompt,stream,messages}` | daemon (`_prep_debug_dumps`) | none | gitignored |
| `CLAUDE.md` | operator (Next task ID auto-bumped by daemon at drain time — TB-141 deferred from per-add to once per drain pass) | none | yes |
| `.cc-autopilot/focus_pointer.json` | daemon (`ideation_halt.maybe_halt_on_exhaustion`; TB-226 / TB-345) | `fcntl.flock` (`locked_inplace`) | no (gitignored) |

State-file commits land with subject `state: TB-N → Complete` (per task) or `state: cron <name>` / `state: ideation` (per cron/ideation run). They ride alongside the task agent's source commit so `git log` tracks board evolution next to code evolution.

## Module map

**Core** is intentionally flat — sibling modules instead of subpackages — so a reader can `ls ap2/*.py` and see the surface in one screen, and each split (TB-262 → `tools.py`; TB-263 → `daemon.py`; TB-264 → `cli.py`; TB-265 → `web.py`) lands as new top-level files rather than nested namespaces. The **components** (see **Component model**) are the exception: each loop subsystem lives in its own `ap2/components/<name>/` subpackage (a `manifest.py` + `impl.py` pair) walked by the registry at startup. The descriptions below name the load-bearing public symbols each module hosts so a reader can re-attribute a stale `tools.py:684`-style citation to its new home. Note what is **not** a component: the LLM judges (the verification prose-judge in `verify.py`, the briefing dep-coherence judge in `briefing_validators.py`) are core sub-steps reached via `select_adapter(...)`, the cron interval engine stays in core `cron.py`, and the pipeline subsystem (`pipeline_sweep.py`) stays in core.

```
ap2/
├── registry.py           # Manifest, Phase, Registry.discover (pkgutil walk of components/*/manifest.py),
│                         # default_registry, contributions(point) — the component registry (no hardcoded
│                         # component list; core resolves every component through this seam)
├── config_loader.py      # ConfigKey + validate_config (per-component [components.<name>] TOML schema)
├── components/           # opt-in loop subsystems, each a manifest.py + impl.py subpackage the
│   │                     # registry discovers; core never statically imports this tree (CI-gated)
│   ├── attention/        # proactive attention-condition detector (Phase.ATTENTION_EMISSION) — the
│   │                     # task_stuck / task_frozen / validator_judge_noisy / auto_approve_paused /
│   │                     # cost_cap_approach conditions; emits attention_raised events (one per fresh
│   │                     # condition) + optional immediate MM push (always-on; env_flag=None)
│   ├── auto_approve/      # evaluate_auto_approve_decision + the promote-time gate chain (Phase.PRE_DISPATCH
│   │                     # pass); opt-in via AP2_AUTO_APPROVE (default_enabled=False)
│   ├── auto_unfreeze/     # _maybe_auto_unfreeze sweep consuming `BriefingFix:` lines (Phase.PRE_DISPATCH);
│   │                     # kill switch AP2_AUTO_UNFREEZE_DISABLED
│   ├── cron/             # the cron SCHEDULER (Phase.CRON_DISPATCH): load_jobs → due_jobs → resolve each
│   │                     # job.name to its handler; kill switch AP2_CRON_DISABLED (interval engine stays in cron.py)
│   ├── communication/    # owns inbound (poll_inbound hook) + outbound (Phase.COMMUNICATION, drains ap2.notify);
│   │                     # wraps its channel adapters (mattermost/ subpackage) in an internal registry (always-on)
│   ├── ideation/         # the proposal engine (Phase.IDEATION run_ideation_tick → _maybe_ideate) + the
│   │                     # roadmap-exhaustion halt (Phase.PRE_DISPATCH); kill switch AP2_IDEATION_DISABLED
│   ├── janitor/          # repo-hygiene findings (TB-217 family); contributes {"janitor": handler} to the
│   │                     # cron job-handler registry + status_findings_counts; kill switch AP2_JANITOR_DISABLED
│   └── mattermost/       # channel-adapter subpackage (NO manifest.py — not a discovered component);
│                         # owned by communication; check_new_messages / reply / thread-read handlers
├── _shared.py            # cross-module utilities (locks, parsers) imported by both daemon and tools without cycling
├── audit.py              # ap2 audit retrospective-walk helpers (TB-248)
├── automation_stats.py   # /stats aggregation helpers (windows, sparklines, top-N expensive tasks)
├── automation_status.py  # collect_auto_approve_state, status-line composer for ap2 status / web home
├── backfill.py           # ap2 backfill-proposals — historical proposal-record reconstruction (TB-195)
├── board.py              # Board (TASKS.md parser), locked_board, board_file_lock, malformed_lines,
│                         # next_ready, next_dispatchable
├── board_edits.py        # do_board_edit (TB-262 split out of tools.py): the MCP write-path handler for
│                         # TASKS.md mutations — add/move/update/delete rows with the board lock held
├── briefing_validators.py # _validate_briefing_structure (TB-262 split out of tools.py — the TB-154 / TB-161 /
│                         # TB-164 / TB-171 deterministic checks plus the TB-235 dep-coherence LLM judge
│                         # body itself: _check_dependency_coherence / _judge_dep_coherence_default /
│                         # _parse_dep_judge_response, demoted here from the validator_judge component by TB-386),
│                         # IMPACT_VERDICTS (TB-189 single source of truth), _briefing_section_names,
│                         # extract_goal_anchor, extract_why_now, write_ideation_proposal_record
├── check.py              # ap2 check — pre-flight diagnostic on a project tree
├── cli.py                # build_parser (argparse tree); cmd_* handlers live in the cli_* siblings below
├── cli_board.py          # cmd_add / cmd_update / cmd_backlog / cmd_unfreeze / cmd_delete / cmd_reject /
│                         # cmd_classify / cmd_approve (TB-264 split: the board-mutation operator verbs)
├── cli_daemon.py         # cmd_start / cmd_stop / cmd_status / cmd_pause / cmd_resume / cmd_web
│                         # (TB-264 split: the daemon-lifecycle operator verbs)
├── cli_diagnostic.py     # cmd_init / cmd_check / cmd_doctor / cmd_logs / cmd_cron_list / cmd_cron_edit
│                         # (TB-264 split: the read-only diagnostic + cron operator verbs)
├── cli_review.py         # cmd_audit / cmd_ideate / cmd_update_goal / cmd_rollback / cmd_backfill_proposals /
│                         # cmd_ack (TB-264 split: the review / ack operator verbs)
├── config.py             # Config dataclass, env-var resolution, .cc-autopilot/env loader
├── channel.py            # ChannelAdapter ABC + core stdout/file fallback adapters (TB-312) — the
│                         # destination abstraction the communication component's channels build on
├── cron.py               # the cron INTERVAL ENGINE library: CronJob dataclass, load_jobs, due_jobs,
│                         # mark_run, bootstrap (stays in core so core consumers can use it; the
│                         # SCHEDULER that drives it on the tick is the cron component)
├── cron_handlers.py      # CORE_CRON_HANDLERS (status-report, real-sdk-smoke) + DEFAULT_CRON_HANDLER
│                         # (generic LLM-cron) + JobHandler type — the core side of the cron job-handler
│                         # registry the scheduler aggregates with the janitor component's contribution
├── daemon.py             # main_loop, _tick, _main_tick_loop, _mm_loop, run_task, run_cron, handle_message,
│                         # _run_control_agent, _make_stderr_sink, _handle_failure, _recover_orphans,
│                         # _infer_result_from_head (post-TB-263: state-commit / pipeline-sweep /
│                         # auto-approve / auto-unfreeze / watchdog logic now lives in the sibling modules
│                         # below)
├── daemon_state.py       # daemon-side state load/save helpers shared by run_task + run_cron + _tick
├── diagnose.py           # build_report, render_markdown (watchdog informant — pure)
├── doctor.py             # ap2 doctor: user_audit + project_audit + auto_approve_audit + auto_unfreeze_audit +
│                         # CLI presence
├── env_reload.py         # .cc-autopilot/env hot-reload (TB-258 — re-reads Config without daemon restart)
├── events.py             # append-only JSONL writer, tail(), MEANINGFUL_EVENT_TYPES
├── goal.py               # goal.md parsing + roadmap_exhausted predicate consumed by ideation / dispatch /
│                         # auto-approve gates
├── ideation.py           # back-compat `__getattr__` shim (TB-391: the proposal engine moved to the
│                         # ideation component) + the read-layer / shared data that stays core: prompt
│                         # loader (load_prompt → ideation.default.md), decision/focus parsers, the
│                         # *_DEFAULT knobs
├── ideation_halt.py      # back-compat shim (TB-391: maybe_halt_on_exhaustion moved to the ideation
│                         # component's Phase.PRE_DISPATCH hook; focus_pointer.json bookkeeping +
│                         # roadmap_complete emission live there now)
├── init.py               # init_project (gitignores, dirs, board templates) + BRIEFING_TEMPLATE
├── insights.py           # maybe_regenerate_index (.cc-autopilot/insights/_index.md)
├── json_extract.py       # extract_rightmost_json_object — shared JSON tail-parse helper (TB-261)
│                         # used by the validator-judge parsers and any other LLM-response consumer
├── message_dump.py       # .stream.jsonl / .messages.jsonl per-run debug-dump writers (TB-85)
├── notify.py             # outbound notification queue (TB-389) — core call sites append a JSONL record
│                         # here instead of walking a channel list; the communication component drains it
├── operator_log.py       # operator_log.md append helpers (operator-decision audit trail)
├── operator_queue.py     # do_operator_queue_append (TB-262 split out of tools.py), drain_operator_queue,
│                         # _apply_operator_op, enqueue_operator_ack — the TB-131/TB-141 queue-routed
│                         # board-mutation path used by CLI + MM handler
├── pipeline_sweep.py     # _sweep_pipeline_pending, _pipeline_alive (TB-263 split out of daemon.py —
│                         # per-tick Pipeline Pending sweep + post-pipeline verifier re-run)
├── prompts.py            # build_task_prompt, build_control_prompt, build_mattermost_prompt
├── result.py             # parse RESULT block (status/commit/summary/files/cron)
├── retry.py              # retry counter (fcntl-locked .json)
├── rollback.py           # linear_rollback_to + helpers (TB-110 + TB-111 + TB-115)
├── sandbox.py            # claude-agent setup, project-clone, MM creds, statusline
├── state_commit.py       # _commit_state_files (TB-263 split out of daemon.py), _filter_state_paths,
│                         # _snapshot_state_paths — the "state: TB-N → <section>" commit author
├── status_report.py      # status-report cron-job body (the Mattermost digest)
├── tools.py              # MCP server build_mcp_server + the handlers that did NOT move out in TB-262:
│                         # do_pipeline_task_start, do_cron_edit (operator-CLI only — TB-146),
│                         # do_task_complete, do_cron_propose, do_git_log_grep, do_ideation_state_write,
│                         # do_log_event, do_daemon_control, do_mattermost_reply, do_mattermost_thread_read.
│                         # Also defines CONTROL_AGENT_TOOLS / TASK_AGENT_TOOLS / MM_HANDLER_TOOLS and
│                         # re-exports the moved symbols (do_board_edit, do_operator_queue_append,
│                         # IMPACT_VERDICTS, _validate_briefing_structure, …) so pre-TB-262 callers
│                         # continue to resolve `from ap2.tools import …`.
├── verify.py             # parse_verification_section, verify_task (per-task gate), _judge_prose_bullet
│                         # (the optional LLM prose-bullet judge — a core sub-step of verify_task; TB-382
│                         # had modeled it as a verifier_judge component, TB-386 demoted it back here)
├── verify_harness.py     # _maybe_per_task_verify, _run_verify, VerifyResult (the run_task →
│                         # per-task verify → project-wide verify orchestration shim)
├── watchdog.py           # _maybe_auto_diagnose (TB-263 split out of daemon.py — daemon-silent-for-Nh
│                         # MM-post watchdog)
├── web.py                # local read-only web UI router (TB-99 + TB-93 thaw): /, /events, /tasks,
│                         # /task/<TB-N>, /pipelines, /insights, /insight/<name>, /ideation_state,
│                         # /commits, /stats, /usage, /attention. Page renderers live in the web_* siblings below.
├── web_attention.py      # /attention pull-surface — operator-legible per-condition bullets from
│                         # attention.detect_attention_conditions (TB-296 — pull counterpart to the
│                         # status-report cron's push of TB-282's `## Attention needed` bullets)
├── web_chrome.py         # _layout / _row_class / _events_table — shared HTML chrome + the events-table
│                         # renderer the home + events pages compose into (TB-265 split out of web.py)
├── web_events.py         # /events page + the per-run debug-dump viewers (_render_task_run, run-stream JSON)
├── web_home.py           # / (home page) renderer — operator decisions panel, focus card,
│                         # automation/ideation/auto-approve status block (TB-265 split out of web.py)
├── web_insights.py       # /insights + /insight/<name> page renderers
├── web_stats.py          # /stats page renderer + JSON shape (TB-259 windows, duration buckets,
│                         # verifier/ideation/cron sub-sections)
├── web_tasks.py          # /tasks + /task/<TB-N> + /pipelines + /ideation_state + /commits page renderers
├── web_usage.py          # /usage page renderer (TB-218 — cost / cache-hit / model-split SVG charts)
├── ideation.default.md   # the ideation prompt body (load-bearing)
├── cron.default.yaml     # bootstrapped cron jobs (status-report)
├── README.md             # operator quickstart + CLI reference
└── architecture.md       # this file
# (the operator + agent manual — CLI verbs, env knobs, event schema, fix-shapes —
#  lives in the auto-triggered skills/ap2-*/SKILL.md bundles, not an ap2/*.md file)
```

Cycles to watch out for:
- `daemon` → components: `daemon._tick` resolves every component purely through the registry (`default_registry().tick_hooks(<phase>)` and the `force_ideate` hook-point) — it never statically imports `ap2/components/`, and the CI import-direction gate enforces that. A component may import core freely (e.g. the ideation component's `impl.py` imports `ap2.ideation` / `ap2.daemon` to reach `_run_control_agent` + `state_commit._commit_state_files`); only core → component is forbidden.
- `daemon` ↔ `tools` / `board_edits` / `operator_queue`: `daemon` imports `tools` (which now re-exports the TB-262-split sibling handlers) and directly imports `do_board_edit` from `board_edits`; none of those tool modules import `daemon`. Tool handlers receive a `Config` and read events directly.
- `tools` ↔ `briefing_validators` / `board_edits` / `operator_queue`: `tools.py` lazy-imports the split siblings AFTER its own `_ok`/`_err`/`slugify` definitions so the siblings can resolve `from .tools import _ok, _err, slugify` against `tools`'s partial-load state. The bottom-of-file re-export block (`from .briefing_validators import IMPACT_VERDICTS, _validate_briefing_structure, …`) keeps the pre-TB-262 `ap2.tools` import surface intact for callers (tests + ideation) that haven't been migrated to the new homes.

## Custom MCP tools

The daemon hands ap2's custom tool set to the resolved adapter's `build_tool_server(...)`, which exposes it to the backend (see **Agent backends**): the `ClaudeCodeAdapter` wraps it in a `claude_agent_sdk.create_sdk_mcp_server` and threads it through `AgentTools.mcp_servers` as `{"autopilot": mcp_server}` (the default path); the `CodexAdapter` serves the identical tool set to a live Codex agent over the `python -m ap2.mcp_stdio` external stdio bridge. Two tool pools, partitioned by `allowed_tools`:

```python
CONTROL_AGENT_TOOLS = [
    # Filesystem (broad reads)
    "Read", "Glob", "Grep",
    # Custom MCP (narrow writes)
    "mcp__autopilot__board_edit",
    # Note: `cron_edit` is intentionally absent (TB-146). Cron schedule
    # mutation is operator-CLI-only via `ap2 cron edit`; task agents
    # emit `cron_proposed` events via `cron_propose` for operator
    # review.
    "mcp__autopilot__mattermost_reply",
    "mcp__autopilot__log_event",
    "mcp__autopilot__daemon_control",
    "mcp__autopilot__ideation_state_write",
    "mcp__autopilot__git_log_grep",          # TB-126: replaces `Bash("git log --grep=…")`
    "mcp__autopilot__operator_log_append",   # TB-141: append to operator_log.md (MM `done: …`)
    "mcp__autopilot__operator_queue_append", # TB-131: queue-routed board mutation
    "mcp__autopilot__status_report_run",     # TB-144: on-demand status-report routine
]

# MM_HANDLER_TOOLS = CONTROL_AGENT_TOOLS minus { ideation_state_write,
# board_edit } (TB-145), plus the handler-only `mattermost_thread_read`
# (TB-149) — cron / ideation have no thread to read so it's never
# widened into CONTROL_AGENT_TOOLS.

# IDEATION_TOOLS = CONTROL_AGENT_TOOLS minus { operator_queue_append }
# (TB-291). Ideation only fires when Active == 0, so the queue-path
# TOCTOU defense is unnecessary; fencing keeps the proposal-path
# event vocabulary single-channel (ideation_proposal_recorded) for
# the empty-cycles counter's reset signal.

TASK_AGENT_TOOLS = [
    "Read", "Glob", "Grep", "Bash", "Edit", "Write",
    "mcp__autopilot__pipeline_task_start",
    "mcp__autopilot__report_result",   # TB-101: the completion-signal call
    "mcp__autopilot__cron_propose",    # TB-123: surface "this should fire on a schedule"
]
```

The "broad reads, narrow writes" split is what makes the system auditable. Every state mutation goes through a typed handler that emits a structured event. The agent can't silently rewrite `TASKS.md` because it doesn't have `Write` access to it (control agents) or because the file is daemon-owned and the agent's prompt forbids touching it (task agents).

`do_log_event` is the escape hatch: an agent can emit any custom event type with a summary. This is how the ideation agent emits `ideation_complete` (its success summary) and how status-report emits `cron_complete` from inside the prompt.

## Two-tier verification

A task is verified twice before landing in Complete:

**Per-task verification** (`ap2/verify.py`). The briefing's `## Verification` section is parsed for shell bullets (`[shell] cmd` or backtick-quoted `\`cmd\``) and prose bullets (free text). Shell bullets run via `subprocess` in the project root; prose bullets go to a small SDK judge that returns `{status, rationale}`. Verdicts: `pass`, `partial` (some unverified, none failed — proceeds to Complete with a `verification_partial` event), `fail` (routes through retry).

The verifier picks the **last** `## Verification` section in the briefing. (Pre-TB-115 two-tier pipeline-launch briefings used this property — they embedded a `validation_briefing` sub-document with its own `## Verification` earlier in the markdown so the launch task's own bullets came last. Post-TB-115 there's only one `## Verification` per briefing — `_sweep_pipeline_pending` re-runs it post-pipeline against the populated work tree.)

**Project-wide regression gate** (`AP2_VERIFY_CMD`). Runs after a successful per-task verify. Default unset = skip. Typical values: `uv run pytest -q`, `cargo test`, `npm test`. Failure routes the task through `_handle_failure` like any other crash. `--no-verify` on the original `ap2 add` opts the task out (tag `#no-verify`).

This split lets the per-task gate stay narrow ("did the agent do THIS task's work") while the project-wide gate stays generic ("did the project break") — the two answer different questions.

## Pipelines (`pipeline_task_start`)

Long-running work (>~5 min wall-clock — sweeps, full-history backtests, Polygon-class data fetches, ML training, anything with rate-limited APIs) goes through `pipeline_task_start(name, command)` instead of being run inline. The tool:

1. Spawns the command via `Popen(shell=True, start_new_session=True)`.
2. Captures `psutil.Process(pid).create_time()` for PID-recycling defense.
3. Writes a `pipeline_start` event with the pid + log path.

The daemon walks the agent's SDK message stream during `_consume`, pairs `pipeline_task_start` tool_use blocks with their tool_result blocks (by `tool_use_id`) to capture pid + started_at, and on `report_result(status="complete")` moves the launching task to a `Pipeline Pending` board section (TB-115 — 4th of 6 sections) instead of Complete, emitting `task_pipeline_pending` with the captured pids.

`_sweep_pipeline_pending` runs each tick (between cron and Ready dispatch). For each Pipeline Pending task, it looks up the most recent `task_pipeline_pending` event for that task and checks every pid via `os.kill(pid, 0)` + psutil `create_time` (pid-recycling defense). When all dead, it re-runs the briefing's project-wide and per-task verifiers against the post-pipeline working tree — pass moves to Complete (with `_append_progress`), fail routes through `_handle_failure(status="verification_failed")` → Backlog → Frozen at retry exhaustion. An agent can call `pipeline_task_start` multiple times in one turn for parallel pipelines; the sweep waits for ALL of them.

Pre-TB-115's two-task split (launch + auto-created Backlog validation with `(blocked on: pid:N@TS)`) was retired; `pipelines.is_blocking` was retired with it (TB-117) once stoch's last pre-TB-115 validation tasks drained off the live board.

## Failure modes and recovery

**SDK subprocess crash with empty stderr** — pre-TB-94, `cron_error` events fired with the useless "Check stderr output for details" sentinel. Now every SDK call routes through a stderr-sink ring buffer (`_make_stderr_sink`), and `task_error` / `cron_error` / `ideation_error` carry `stderr_tail` + `prompt_dump` paths so the operator can replay the prompt and see what actually broke.

**Agent committed but didn't emit RESULT** — `_infer_result_from_head` checks `git log -1` for a subject prefixed with the task ID. If found, the daemon synthesizes a `complete` result and emits `task_implicit_commit` with reason `status_unknown` / `timeout_recovered` / `error_recovered`. This was load-bearing for stoch's TB-59.

**Task in Active when the daemon crashes** — `_recover_orphans` runs at startup, moves any Active task back to Ready, increments its retry counter, and emits `orphan_recovery`. Without this, a crashed daemon would leave its in-flight task wedged.

**Failing task that retry-exhausts** — bumps to Frozen via `move_to_frozen`. Operator unfreezes with `ap2 unfreeze TB-N`, which atomically moves to Backlog and resets the retry counter inside the same `locked_board()`.

**Daemon goes silent for >3h** — the watchdog (`_maybe_auto_diagnose`) builds a `DiagnoseReport` (board summary + recent failures + cron staleness + board health), renders it as Mattermost-friendly markdown, and posts to `AP2_MM_CHANNELS[0]`. Cooldown 6h. Skips when no MM destination is configured (sticky one-shot warning so it doesn't spam).

**Stuck-blocker** — `Board._is_blocker_satisfied` checks each `(blocked on: ...)` token. `TB-N` blockers are satisfied when the named task is in Complete; unknown schemes fail-safe (including the retired `pid:N@TS` scheme — any straggler from a pre-TB-115 / pre-TB-117 board sits in Backlog until the operator removes the clause). `diagnose.board_health["unsatisfiable_blocks"]` surfaces the corner case where a Backlog task is blocked on a Frozen task (will never auto-promote).

**Roadmap exhaustion** — `ideation_halt.maybe_halt_on_exhaustion` (TB-226; merged to the core `ap2/ideation_halt.py` module in TB-345, called directly from the daemon's PRE_DISPATCH phase rather than via the component registry) advances the in-memory focus-list pointer (`.cc-autopilot/focus_pointer.json`) as each `## Current focus:` heading in goal.md exhausts; when the pointer crosses past the last heading, the daemon emits `roadmap_complete` (once). TB-275: this parks the ideation TRIGGER only (`_maybe_ideate` emits `ideation_skipped reason=roadmap_complete`); task dispatch is NOT affected, so already-queued Backlog tasks (operator-added via `ap2 add`, operator-approved via `ap2 approve`, or previously auto-approved) continue to auto-promote and drain. Operator extends the roadmap via `ap2 update-goal` (adds new `## Current focus:` headings, re-arms ideation) or dismisses the notice via `ap2 ack roadmap_complete`. `ap2 pause` remains the explicit full-stop. The two event types — `focus_advanced` and `roadmap_complete` — provide the audit trail. Operator can re-engage a previously-exhausted focus via `ap2 rewind-focus TITLE [--reason TEXT]` (TB-295), which atomically resets the pointer + emits a synthetic `focus_advanced trigger=operator_rewind` event so the empty-cycles counter cutoff respects the rewind. Direct `.cc-autopilot/focus_pointer.json` edits are NOT supported — they produce no event and leave pre-rewind empty cycles counting against the rewound focus's window.

**Malformed task line** — `Board._parse` flags any line that doesn't match `TASK_LINE_RE`; the daemon emits a deduped `board_malformed_line` event in step 3 of `_tick`. Without this, an out-of-band edit (e.g. a `(<sha>)` annotation between `**TB-N**` and `**Title**`) silently strands every task that depends on the affected one.

## Sandbox model

`ap2 sandbox` creates a separate OS user (default `claude-agent`) with:
- Its own home directory (`/Users/claude-agent` on macOS).
- A NOPASSWD sudoers grant for the human user to run `ap2 sandbox …` and `sudo -u claude-agent -i …`.
- Its own `.claude/` config tree (statusline, settings, OAuth token in `~/.zshenv`).
- `~/repos/<project>/` clones of each managed project.

The daemon runs as `claude-agent`. Its tools can't reach the human's `~/.ssh`, keychain, git config, or other repos. Mattermost creds and Anthropic OAuth token live in `~claude-agent/.zshenv` so non-GUI shells (the daemon's `Popen`) get them — the macOS keychain is locked for non-GUI sessions, so token-via-keychain doesn't work.

Per-project Mattermost channel routing lives in `<project>/.cc-autopilot/env` (`AP2_MM_CHANNELS=<id>`), so different projects post to different channels without polluting `~/.zshenv` with project-specific config.

## Continuity & evolution

The daemon is intentionally not transactional across ticks. State files are point-in-time snapshots; recovery is "do the right thing on the next tick." Examples:

- A daemon restart mid-task → orphan recovery on startup; the task gets retried.
- A pipeline that died while the daemon was off → the next tick's `_sweep_pipeline_pending` notices the pid is gone and runs the launching task's verifier against the post-pipeline working tree.
- A cron run that crashed mid-run → next tick checks `cron_state.json` and re-fires when due.
- An ideation run that crashed before writing `ideation_state.md` → cooldown still advances (`mark_run` always fires) so the broken agent doesn't get hammered every tick. Operator can `ap2 logs` to see `ideation_error` and decide whether to manually retry.

This convergence model — every tick is idempotent and corrective — is why the daemon can run for weeks without operator attention and self-heal from most local failures.

## Tests

Three tiers, increasing in fidelity and cost.

### Default — fast, no API cost

`uv run pytest -q ap2/tests` runs the full suite (currently 2000+ tests). All FakeSDK-based or pure-Python; no network, no API credits. Notable files:
- `tests/test_board.py` — TASK_LINE_RE, malformed-line detection, blocked_on parsing.
- `tests/test_cron.py` / `test_cron_defaults.py` — cron yaml parsing + bootstrapped jobs.
- `tests/test_ideation_defaults.py` — pins on `ideation.default.md` content (Step 0 / Step 0.5 / Step 1.5 phrases — load-bearing for ideation behavior).
- `tests/test_verify.py` / `test_briefing.py` — per-task verification + last-`## Verification`-section parsing.
- `tests/test_diagnose.py` — watchdog report shape.
- `tests/test_mcp_inventory.py` — every advertised MCP tool ↔ allowlist match. Catches the "decorated but not in `tools=[...]`" bug class without a real SDK.
- `tests/e2e/test_single_tick.py` / `test_multi_tick_cron.py` / `test_pipeline.py` / `test_mattermost_cron.py` — full `_tick` exercises with `FakeSDK`.

The e2e tests use `FakeSDK` (`tests/e2e/_fakes.py`) — a programmable mock that responds to prompt substrings with canned message streams. Lets a single tick run through `run_task` / `run_cron` / `handle_message` deterministically without spawning a real subprocess.

### Real-SDK smokes — opt-in, cost a few cents per run

`AP2_REAL_SDK=1 uv run pytest ap2/tests/smoke/ -v -s` invokes the real Claude Agent SDK against tiny synthetic tasks. Validates what FakeSDK can't: tool advertisement reaches the agent, the agent actually calls the tool, and the daemon's stream-walking captures the structured payload. Default `pytest` skips them via a module-level `pytest.mark.skipif(not AP2_REAL_SDK)`.

- `tests/smoke/test_report_result_real_sdk.py` — agent calls `report_result` MCP tool → daemon synthesizes valid `TaskResult`. Pins the TB-101 protocol.
- `tests/smoke/test_pipeline_task_start_real_sdk.py` — agent calls `pipeline_task_start` → real OS subprocess spawns, `pipeline_start` event fires. Pins the launch surface (post-TB-115 contract: just name + command, launching task parks in Pipeline Pending).
- `tests/smoke/test_prose_judge_real_sdk.py` — `verify._judge_prose_bullet` against obvious-pass and obvious-fail diffs. Catches the `verification_partial`-from-judge-crash class (TB-146 round 2).

When to run: after any change to MCP tool registration (`tools.py`), agent prompt (`prompts.py`, `ideation.default.md`), or the verifier judge (`verify._judge_prose_bullet`). Total ~30 seconds and a few cents.

### Production smoke — stoch as a continuous validator

The stoch daemon runs continuously and exercises every code path against real briefings. `events.jsonl` is the canonical signal: structured `task_complete` / `cron_complete` / `ideation_complete` / `pipeline_start` events confirm the contracts hold. The watchdog (`diagnose.MEANINGFUL_EVENT_TYPES`) flags daemon stalls; `verification_failed` events flag verifier regressions.

## Reading order for new contributors

1. `ap2/README.md` — what it is, how to use it.
2. This file — why it's shaped this way.
3. `ap2/daemon.py` — `_tick` is the entry point; everything fans out from there.
4. `ap2/board.py` — the `Board` model and `locked_board` are the core data structure.
5. `ap2/tools.py` (plus the TB-262 siblings `board_edits.py` / `operator_queue.py` / `briefing_validators.py` — the last of which also hosts the TB-235 dep-coherence LLM judge after TB-386 demoted it out of the `validator_judge` component) — the MCP tools are the only mutation surface; reading them tells you the system's full state-change vocabulary. `tools.py` registers the server + hosts the remaining handlers and re-exports the siblings, so it's still the entry point.
6. `ap2/ideation.default.md` — the load-bearing prompt that drives the only path that creates new work.

The `.cc-autopilot/tasks/*.md` briefings are per-task historical records of design decisions; reach for them when you want to understand why a specific feature exists, not how it works today.

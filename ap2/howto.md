# ap2 — how it operates this project (Claude session quick-reference)

A condensed view of `ap2/README.md` + `ap2/architecture.md`, written for a
Claude Code session running inside an ap2-managed project (most often as
the `claude-agent` sandbox user). Covers what ap2 is, what's on disk,
the agent's contract, the operator-facing surfaces, and where to look
when answering questions like "why did TB-N fail" or "what's the
daemon doing right now."

## What ap2 is

A Python daemon (`ap2`) that drives a project through a list of tasks
without keeping any long-lived Claude session. Each unit of work — task,
cron, ideation, mattermost reply — runs as a fresh `claude_agent_sdk`
`query()` call. Shared state lives on disk in `TASKS.md`,
`.cc-autopilot/events.jsonl`, briefings, and a few state files. The
daemon never accumulates context.

Three design principles drive every other choice:
1. **Each unit of work runs in a fresh SDK query** — no compaction
   fatigue, no shared memory across runs.
2. **Shared awareness lives in files** — every spawned agent gets the
   relevant files inlined into its prompt (typically a briefing + a
   tail of `events.jsonl`).
3. **Mutations go through narrow MCP tools** — agents don't get
   `Write`/`Edit` access to daemon-owned files. Every state change goes
   through a typed handler that emits a structured event.

## On-disk layout

After `ap2 init`, the project gains:

```
TASKS.md                       # 5-section board, daemon-owned
goal.md                        # operator-curated mission (read by ideation)
CLAUDE.md                      # project conventions; daemon bumps Next task ID
.cc-autopilot/
├── progress.md                # append-only session log (per-task entries)
├── events.jsonl               # structured event stream (the canonical timeline)
├── cron.yaml                  # scheduled-job registry (status-report by default)
├── cron_state.json            # last-fired timestamps per cron
├── retry_state.json           # per-task retry counts
├── mm_state.json              # mattermost cursor + thread cache
├── auto_diagnose_state.json   # watchdog cooldown
├── ideation_state.md          # ideation's per-cycle progress assessment
├── daemon.pid                 # daemon process id (when running)
├── paused                     # presence-only: pause flag
├── env                        # KEY=VAL project-scoped overrides
├── tasks/                     # per-TB-N briefings (Goal/Scope/Verification)
├── insights/                  # project-output knowledge files (+ auto-index)
├── pipelines/                 # detached-pipeline logs (PID-named)
└── debug/                     # per-run prompt + stream + messages dumps
```

The 5-section board has a fixed order:
**Active → Ready → Backlog → Complete → Frozen**.

## Authoring goal.md — see the ap2-ideation-goals skill

The operator-facing guide to authoring `goal.md` — what each of the five
operator-curated sections (Mission / Done when / Current focus / Non-goals
/ Constraints) is for, how ideation reads them, the **delete-test** for
`## Done when` bullets, the worked fictional-project examples, and the
queue-time validators (TB-161 anchor, TB-164 Why-now, TB-235
dependency-coherence) that key off the content — was carved into the
auto-triggered **ap2-ideation-goals** skill
(`skills/ap2-ideation-goals/SKILL.md`, TB-403). `ap2/ideation.default.md`
remains the canonical daemon source for the ideation agent's own
briefing-authoring conventions; the skill is operator-session tooling.

## Task-agent contract — see the ap2-task skill

The task-agent contract — what a Claude session must do when the daemon
dispatches it against a briefing (read the briefing first, check for prior
work, the off-limits file list, the `<TASK_ID>:` commit-subject convention,
the single `report_result(...)` completion signal, and the
`pipeline_task_start` long-running-work path) — was consolidated into the
auto-triggered **ap2-task** skill (`skills/ap2-task/SKILL.md`, TB-400)
alongside the `ap2 add` briefing-authoring flow that produces those
briefings. `ap2/ideation.default.md` remains the canonical daemon source
for the briefing-authoring rules; the skill mirrors them for operators.

## What the daemon does each tick (~30s)

```
1. Mattermost — poll @claude-bot mentions → spawn handler agent per message
2. Cron       — run any due jobs from cron.yaml (status-report etc.)
3. Tasks      — pick next Ready, or auto-promote next dispatchable Backlog
                → run task agent
4. Ideation   — fire `_maybe_ideate` if Ready+Backlog count is below
                `AP2_IDEATION_TRIGGER_TASK_COUNT` (default 3, TB-160)
                + cooldown elapsed (default 2h). Operator can also
                trigger manually via `ap2 ideate [--force]` (TB-159),
                bypassing the cooldown / disable / queue-depth gates.
5. Watchdog   — `_maybe_auto_diagnose` posts to mattermost when daemon
                idle > 3h
```

Steps run sequentially. A failure in any step emits an event and
proceeds; one broken cron doesn't block task dispatch.

## Verification — what the daemon checks before Complete

Two layers wrap every successful task:

**Per-task** (`ap2/verify.py`). Parses the briefing's `## Verification`
section via mistune AST. Each bullet:
- **Shell** (` `cmd` ` or `` `` `cmd` `` ``) — runs via subprocess in
  the project root; exit 0 = pass.
- **Prose** (free text) — sent to an SDK judge that returns `pass` /
  `fail`; on judge crash or unparseable response, falls back to
  `unverified`.

Verdicts: `pass` → Complete. `partial` (some unverified, no fails)
→ Complete + `verification_partial` event. `fail` (any) → Backlog →
retry → Frozen at retry exhaustion.

The verifier picks the **last** `## Verification` heading. (Pre-TB-115
two-tier pipeline briefings used this property to keep the launch task's
own checks last while a sub-`validation_briefing` carried output-artifact
bullets earlier; the two-tier split is retired post-TB-115 — now the
single `## Verification` runs both at synchronous-completion time AND
post-pipeline as `_sweep_pipeline_pending` re-runs it.)

**Project-wide gate** (`AP2_VERIFY_CMD`, optional). Runs after the
per-task gate. Typical: `uv run pytest -q`. `--no-verify` tag opts
specific tasks out (e.g. docs-only changes).

For prose-judge parse-failure diagnostics — length signals,
`parse_error` categories, and the on-disk response dumps — see the
**ap2-observability** skill (`skills/ap2-observability/SKILL.md`).

## Authoring `## Verification` bullets — see the ap2-task skill

The briefing `## Verification`-bullet authoring convention — the `Prose:`
prefix for codespan-leading judge bullets and the four shell-bullet
authoring pitfalls (literal backticks, the absence-check `!` prefix,
directory-walking `grep -r`, and the `Prose:` complement) with the worked
example combining all four — was consolidated into the **ap2-task** skill
(`skills/ap2-task/SKILL.md`, TB-400). The
`test_skill_still_carries_all_four_pitfalls` companion check in
`ap2/tests/test_tb273_ideation_pitfalls_sync.py` now reads the skill.
`ap2/ideation.default.md`'s `## Shell-bullet pitfalls to AVOID` section
remains the canonical daemon copy; the skill mirrors it for operators.

## Failure modes the daemon recovers from — see the ap2-failure-recovery skill

The auto-recovery catalogue — which failures the daemon detects and heals
itself (SDK subprocess crash, agent-committed-but-didn't-report, Active
task on daemon restart, retry exhaustion → Frozen, idle-watchdog
`DiagnoseReport`, stuck blocker, malformed task line) and the event /
operator-verb each surfaces — was carved out of this manual into the
auto-triggered **ap2-failure-recovery** skill
(`skills/ap2-failure-recovery/SKILL.md`, TB-402), alongside the
operator-question playbook below.

## Operator board-ops reference — see the ap2-board-ops skill

The operator-action reference — the `autopilot` custom MCP tool catalogue
(the `report_result` / `board_edit` / `operator_queue_append` /
`cron_propose` pools, partitioned by agent toolset) and the full
`ap2 <verb>` operator-CLI verb table with its WHY / when-to-use companion —
was carved out of this manual into the auto-triggered **ap2-board-ops**
skill (`skills/ap2-board-ops/SKILL.md`, TB-399). The
`test_every_cli_verb_documented` drift gate in
`ap2/tests/test_docs_drift.py` now reads the skill, and
`test_every_mcp_tool_documented` accepts the skill alongside
`ap2/architecture.md`'s `CONTROL_AGENT_TOOLS` / `TASK_AGENT_TOOLS` literal
enumeration — so an operator looks up a CLI verb or an agent's MCP tool
surface by reading that one skill, not by grepping this file.

## Retrospective audit workflow — see the ap2-ideation-goals skill

The operator's retrospective review surface — the `ap2 audit` verb
(TB-248) for the "I just came back from a week of unattended operation,
what shipped and what's worth a verdict?" walk, its `--interactive`
per-task classify/skip walkthrough, the `--since` / `--frozen-only` /
`--auto-approved-only` filter flags, the `--json` shape, the no-new-state
cursor/reviewed-set derivation, and the TB-258 natural-cadence return
surfaces — was carved into the auto-triggered **ap2-ideation-goals** skill
(`skills/ap2-ideation-goals/SKILL.md`, TB-403). For the `ap2 audit` /
`ap2 classify` / `ap2 approve` / `ap2 reject` verb table see the
**ap2-board-ops** skill; for the impact verdicts see the **ap2-task**
skill's `## Classify verdicts` reference.

## Components enumeration (`ap2 status`)

TB-319 closes the goal.md L235-237 Progress signal that named
`ap2 status` as the natural surface for discovering which components
are wired into the daemon. Before TB-319 the only way to find out was
to `ls ap2/components/` and read each manifest by hand; after, every
`ap2 status` invocation appends a `## Components` block listing every
discovered manifest with its on/off state and the env-flag string
that controls it.

**Text-mode rendering.** After the operator-attention cluster +
`auto-approve:` block + `next:` line, `ap2 status` prints:

    ## Components
      attention: on (env_flag=None)
      auto_approve: on (env_flag=None)
      auto_unfreeze: on (env_flag=None)
      focus_advance: on (env_flag=None)
      janitor: on (AP2_JANITOR_DISABLED unset)
      mattermost: on (AP2_MM_CHANNELS=channel-id)

Entries are alphabetic by manifest name (matching
`default_registry().components` iteration so a reader's mental model
of "in what order do hooks fire?" lines up with what they see in
status). Always emitted — unlike the operator-attention cluster's
omit-on-empty rule, the registry walk is deterministic and the same
set of components ships on every project, so suppressing the section
would itself be a regression worth surfacing.

**`<state>` is `on` or `off`** — resolved via `Manifest.is_enabled()`
against the live process env, so a hot-reloaded `.cc-autopilot/env`
takes effect on the next invocation. The polarity convention
(`Registry._is_enabled` since TB-309, now consolidated on
`Manifest.is_enabled` by TB-319):

- **`env_flag=None`** (attention, auto_approve, auto_unfreeze,
  focus_advance) — always-on per `default_enabled=True`. There's no
  master kill switch for these four; whether to add one is the
  open ideation question logged at the `## Decisions needed` surface.
- **Suppress polarity** (`*_DISABLED` env_flag, `default_enabled=True`
  — janitor / `AP2_JANITOR_DISABLED`) — the env var is a kill switch:
  truthy disables, unset/empty/falsy keeps the component on. (TB-386
  demoted the validator_judge / verifier_judge LLM judges out of
  `ap2/components/`; their `AP2_VALIDATOR_JUDGE_DISABLED` /
  `AP2_VERIFY_JUDGE_DISABLED` off-switches survive as plain config knobs
  read directly by the core briefing-validation / verify runners, NOT as
  component env_flags, so they no longer appear in this list.)
- **Require polarity** (opt-in env_flag, `default_enabled=False`
  — mattermost / `AP2_MM_CHANNELS`) — the env var is an opt-in
  toggle: unset/empty disables, non-empty enables.

**`<env_flag_desc>`** renders the env-flag state in operator-legible
form: `env_flag=None` for always-on manifests, `<NAME> unset` when
the env var is absent or empty, or `<NAME>=<value>` when set
(truncated at 32 chars with an ellipsis so a long channel-id list /
opaque token doesn't blow up the status block width).

**JSON parity (`--json`).** A top-level `components` key carries
one entry per discovered manifest, each with the four documented
keys (`name`, `enabled`, `env_flag`, `default_enabled`). ALWAYS
present (parser-stability mirror of the `auto_approve` / `audit` /
`attention` blocks), and the text + JSON branches walk the same
`default_registry().components` snapshot inside one `cmd_status`
call so they can never disagree about a component's enabled state.

**Out of scope** (deferred to follow-up TBs if operator asks):

- A `/components` web pull page parallel to TB-296's `/attention`.
- Per-component diagnostic info (tick counts, last-fired timestamps,
  recent events).
- New env knobs to filter the enumeration; the walk shows every
  discovered manifest unconditionally. Master kill-switch flags for
  the four `env_flag=None` manifests are the open ideation question
  named above.

## Classify verdicts — see the ap2-task skill

The `ap2 classify TB-N --impact <verdict>` reference — the four
`IMPACT_VERDICTS` buckets (`advanced-goal` / `pro-forma` / `negative` /
`unclear`), the two delete-tests that pick the verdict, and the
load-bearing `pro-forma` ↔ `negative` harm-dimension distinction (TB-251)
— was consolidated into the **ap2-task** skill
(`skills/ap2-task/SKILL.md`, TB-400). `IMPACT_VERDICTS` stays the source
of truth at `ap2/briefing_validators.py` (re-exported via
`ap2.tools.IMPACT_VERDICTS`); the `## Retrospective audit workflow`
section above and `ap2 audit --interactive`'s `[c]lassify` action both
reference that verdict vocabulary.

## Operator-question playbook — see the ap2-failure-recovery skill

The "where do I look?" lookup table for the questions an operator asks
when intervening (is the daemon running, why did TB-N fail, what did the
agent commit, is a pipeline still running, what did ideation propose) plus
the read-only `ap2 web` UI for scanning visually — was carved out of this
manual into the auto-triggered **ap2-failure-recovery** skill
(`skills/ap2-failure-recovery/SKILL.md`, TB-402), alongside the
auto-recovery catalogue above.

## Configuration reference — see the ap2-config skill

The operator-facing configuration reference — the full `AP2_*`
environment-knob catalogue, the typed `.cc-autopilot/config.toml` key
reference (`[core]` / `[components.*]` / `[agent_backends]`), and the Codex
agent-backend install + auth setup — was carved out of this manual into the
auto-triggered **ap2-config** skill (`skills/ap2-config/SKILL.md`, TB-398).
The `test_every_env_knob_documented` and `test_every_config_key_documented`
drift gates in `ap2/tests/test_docs_drift.py` now enforce that every `AP2_*`
source knob and every `ConfigKey` schema key is documented there — so an
operator discovers or tunes any knob by reading that one skill, not by
grepping this file. The `.cc-autopilot/env` and `.cc-autopilot/config.toml`
scaffolds `ap2 init` writes point at it too.

## Sandbox model

The daemon runs as a separate OS user (`claude-agent` by default) so
its tools can't reach the human's `~/.ssh`, keychain, git config, or
other repos. OAuth token + Mattermost creds live in
`~claude-agent/.zshenv` (the macOS keychain is locked for non-GUI
shells, so token-via-keychain doesn't work for the daemon's `Popen`).
Per-project Mattermost channel routing lives in
`<project>/.cc-autopilot/env`.

## Convergence model

The daemon is intentionally not transactional across ticks. Every tick
is idempotent and corrective:
- Mid-task crash → `_recover_orphans` on next start, task retries.
- Pipeline died while daemon was off → next tick's
  `_sweep_pipeline_pending` notices and runs verification.
- Cron run crashed mid-run → next tick re-fires when due.
- Ideation crashed before writing state → cooldown still advances so
  the broken agent doesn't get hammered every tick.

This is why ap2 can run for weeks without operator attention.

## Reading order if you want depth

1. This file — what's on disk, what each thing means, where to look.
2. `.cc-autopilot/progress.md` (tail) — recent task outcomes in
   operator-readable prose.
3. `.cc-autopilot/events.jsonl` (tail) — the structured timeline.
4. `git log --oneline -30` — what code shipped.
5. The full ap2 docs — `ap2/README.md` and `ap2/architecture.md` in the
   ap2 source tree (https://github.com/lzhang/autopilot2) — for design
   rationale, agent kinds, MCP tool wiring, dependency graph.

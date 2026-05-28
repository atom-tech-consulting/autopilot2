"""Project-init scaffolding: gitignores, dirs, marker files, board templates.

The single source of truth for what an ap2-managed project should ignore vs.
track, and what the bare minimum on-disk skeleton looks like. Replaces the
manual transcribe-from-skill-markdown flow that left stoch's `cron.yaml`
untracked for weeks and silently accumulated `.lock` / `.bak` files.

Idempotent: re-running unions gitignores, never touches existing TASKS.md /
progress.md / CLAUDE.md content (only writes if missing or appends a new
`## Autopilot` block to a CLAUDE.md that lacks one).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import (
    AUTOPILOT_DIR_NAME,
    DEFAULT_CONTROL_TIMEOUT_S,
    DEFAULT_IDEATION_MAX_TURNS,
    DEFAULT_TASK_MAX_TURNS,
    DEFAULT_TASK_TIMEOUT_S,
    DEFAULT_VERIFY_TIMEOUT_S,
)


TASKS_TEMPLATE = (
    "# Tasks\n\n"
    "## Active\n\n"
    "## Ready\n\n"
    "## Backlog\n\n"
    "## Pipeline Pending\n\n"
    "## Complete\n\n"
    "## Frozen\n"
)

PROGRESS_TEMPLATE = "# Progress\n"

CLAUDE_AUTOPILOT_TEMPLATE = (
    "## Autopilot\n\n"
    "- Task list: `TASKS.md`\n"
    "- Task briefings: `.cc-autopilot/tasks/`\n"
    "- Progress log: `.cc-autopilot/progress.md`\n"
    "- Next task ID: TB-1\n"
)

# Project-level goals/non-goals anchor read by the ideation cron (TB-70). The
# template is intentionally short — humans fill it in. Empty/skeleton values
# are tolerated by the ideation prompt, which falls back to inferring goals
# from CLAUDE.md + progress.md when the file is missing or all-placeholder.

# TB-154: canonical `##`-level section names every briefing must carry.
# Single source of truth — used by:
#   - `BRIEFING_TEMPLATE` below (the empty-add scaffold and editor-mode buffer).
#   - `ap2/tools.py::_validate_briefing_structure` (queue-append-time hard gate).
#   - `ap2/check.py::_check_briefing_structure` (on-disk lint, warning-level).
#   - `ap2/prompts.py` MM handler instructions + `operator_queue_append`
#     docstring (so the agent reads the requirement before authoring).
# Order is rendered order in the template; the validator only checks
# presence, not order — extension is fine, omission/rename is not.
BRIEFING_REQUIRED_SECTIONS: tuple[str, ...] = (
    "Goal",
    "Scope",
    "Design",
    "Verification",
    "Out of scope",
)

# TB-161: goal.md heading prefixes the briefing's `## Goal` body must cite
# (as a substring) for the goal-anchor check. Single source of truth — used
# by:
#   - `ap2/tools.py::_goal_md_anchors` (queue-append-time hard gate inside
#     `_validate_briefing_structure`).
#   - `ap2/check.py::_check_briefing_structure` (warning-level lint for
#     legacy on-disk briefings).
#   - `ap2/prompts.py` MM handler instructions + `operator_queue_append`
#     docstring + `ap2/ideation.default.md` so the briefing-author agent
#     reads the requirement before authoring.
# Default headings are `Current focus` and `Done when`. Prefix match is
# case-insensitive on a `##`-level heading title — `## Current focus:
# ideation quality` and `## Done when` both qualify. Bullet anchors are
# only mined from `Done when` sections; for `Current focus` only the full
# heading title is used (the body is typically prose, not enumerable
# anchors). Extension is allowed but explicitly out-of-scope this cycle.
GOAL_ANCHOR_HEADINGS: tuple[str, ...] = (
    "Current focus",
    "Done when",
)

# TB-164: minimum non-marker character count for the "Why now" rationale
# that every briefing's `## Goal` body must include. Pulled out as a
# named constant so the validator (`ap2/tools.py`), the lint
# (`ap2/check.py`), and the unit tests pin against the same number; a
# future tweak to the threshold flows from one place. 40 chars rejects
# trivial passes like `Why now: yes` while staying short enough that
# templates don't feel padded — the briefing's "if we delete this and
# the goal still ships, was it useful?" delete-test (goal.md lines
# 61-70) needs a sentence, not an essay. The marker check itself is
# line-anchored so the rule isn't matched mid-prose.
WHY_NOW_MIN_CHARS: int = 40

BRIEFING_TEMPLATE = (
    "# {task_id} — {title}\n\n"
    "Tags: \n\n"
    "## Goal\n\n"
    "{description}\n\n"
    "Why now (delete-test): (one sentence answering goal.md's "
    "delete-test — \"if we delete this and the goal still ships, was "
    "it useful?\" — name the failure mode this closes or the gap it "
    "fills, not just \"this would be nice to have\")\n\n"
    "## Scope\n\n"
    "- (file / module to change)\n\n"
    "## Design\n\n"
    "(how this will be built — surface, data flow, edge cases)\n\n"
    "## Verification\n\n"
    "Concrete acceptance criteria the daemon's per-task verifier (TB-69)\n"
    "runs after the agent's commit. Auto-verifiable bullets only (TB-138)\n"
    "— every bullet must be auto-verifiable: (1) a backticked shell command\n"
    "the verifier can `/bin/sh -c`, (2) a unit/e2e test name the regression\n"
    "gate covers, or (3) a prose claim naming a concrete file/symbol an SDK\n"
    "judge can confirm against the diff. No `Manual:` bullets — the\n"
    "verifier runs unattended and cannot observe a live operator action;\n"
    "if a behavior cannot be auto-verified, it belongs in `## Out of scope`.\n\n"
    "- `uv run pytest -q` — full suite passes\n\n"
    "## Out of scope\n\n"
    "- (filled in)\n"
)


def render_briefing(
    *,
    task_id: str,
    title: str,
    description: str = "",
) -> str:
    """Fill `BRIEFING_TEMPLATE` for a new task.

    Pure formatter — no side effects, idempotent for the same inputs.

    History: pre-TB-135 this was called from `do_board_edit` /
    `do_operator_queue_append` to auto-populate a skeleton briefing when
    `add_backlog` was invoked with no payload. That auto-fill path was
    retired (TB-135) — `## Verification` lacking real bullets meant the
    per-task verifier scored prose placeholders against an empty diff and
    "passed" with zero scope-specific evidence, completing tasks like
    TB-131 on regression-gate pass alone. Briefing authorship is now the
    caller's responsibility (CLI: `--briefing-file`; ideation / MM
    handler: build the payload directly). The function survives only as
    a convenience formatter — the CLI uses it (`ap2 add` editor mode,
    pending) and the test suite pins its shape.
    """
    desc = description.strip() or "(one-paragraph description of what success looks like)"
    return BRIEFING_TEMPLATE.format(
        task_id=task_id,
        title=title,
        description=desc,
    )


# TB-199: `## Done when` sits between Mission and Current focus so the
# strategic framing (Mission + Done-when = what success looks like) is
# grouped before the tactical state (Current focus + Constraints).
# Mirrors this project's own goal.md ordering. The section is load-
# bearing for TWO surfaces:
#   - ideation's done-signal — all-met Done-when criteria are how
#     ideation recognizes "stop proposing here" on a finished project.
#   - the TB-161 goal-anchor validator — `_goal_md_anchors` mines
#     Done-when bullets for substring anchors a briefing's `## Goal`
#     body must cite. Pre-TB-199 the shipped template omitted the
#     section, so fresh `ap2 init` projects could only anchor against
#     `## Current focus` until an operator hand-added Done-when.
#
# Placeholder body shape is deliberate: explanatory prose describing
# what belongs (mentions "criterion", names the "stop proposing here"
# done-signal, gives an example, references the model) followed by a
# single `- (TODO)` stub bullet. The stub keeps the "bulleted list"
# hint visible but its body normalizes to <3 words, so
# `_bullet_anchor_phrase` rejects it — the placeholder template
# contributes zero live anchors. That preserves the TB-161 day-one
# fresh-project skip path: a project whose `goal.md` is still the
# all-placeholder template doesn't reject every proposal on day one.
# Anchors emerge naturally the moment the operator replaces the stub
# with real shipped-when criteria (the section is "live" the instant
# real content lands; the placeholder is just inert).
GOAL_TEMPLATE = (
    "# Project Goals\n\n"
    "## Mission\n"
    "(one-sentence statement of what this project is FOR)\n\n"
    "## Done when\n"
    "(Fill in a bulleted list of concrete \"the project ships when X\"\n"
    "criteria — e.g. \"the API handles N requests/sec at p99 latency\n"
    "Xms in production\". Ideation reads these as the done-signal:\n"
    "all-met criteria mean \"stop proposing here\". See goal-draft.md's\n"
    "own Done-when examples for shape.)\n\n"
    "- (TODO)\n\n"
    "## Current focus\n"
    "- (area or theme actively in flight now)\n\n"
    "## Non-goals\n"
    "- (explicit things this project is NOT trying to do, so ideation\n"
    "  doesn't propose them)\n\n"
    "## Constraints\n"
    "- (hard constraints — tech stack, deadlines, dependencies,\n"
    "  blast-radius limits)\n"
)


# Living progress assessment maintained by ideation each cycle (TB-87).
# Ideation reads the prior assessment + goal.md + Complete tail, then
# OVERWRITES this file with a fresh assessment that grounds the cycle's
# proposals in cited TB-Ns. Schema is fixed by the cron prompt — this is
# just a placeholder so first-cycle reads don't fail.
# Placeholder for `.cc-autopilot/insights/_index.md` — overwritten by
# `ap2/insights.py::regenerate_index` at the first ideation cron tick after a
# real insight file is added. Tracked in git so cloning the repo is enough to
# get a runnable scaffold (TB-89).
INSIGHTS_INDEX_PLACEHOLDER = (
    "# Insights index\n"
    "_Auto-generated by ap2 before each ideation cron tick; do not edit._\n\n"
    "(no insights yet — drop markdown files into this directory with YAML\n"
    "front matter to surface them here.)\n"
)


IDEATION_STATE_TEMPLATE = (
    "# Ideation State\n\n"
    "_Not yet generated. Will be written on the next ideation cron tick.\n"
    "Schema (set by `ap2/cron.default.yaml`'s ideation prompt):\n"
    "Mission alignment / Current focus assessment / Non-goal risk check /\n"
    "Considered & deferred / Cycle observations / Decisions needed from\n"
    "operator / Proposals._\n"
)


# TB-278: documented `.cc-autopilot/env` scaffolding written by `ap2 init`
# to fresh projects. Every common knob is listed with its code default
# shown inline and the `KEY=VALUE` line commented out — the file
# documents-by-default without overriding anything (the code defaults
# still apply unless the operator uncomments).
#
# Idempotent + non-clobbering: `init_project` only writes this when
# `.cc-autopilot/env` is ABSENT (operators put secrets / channel IDs in
# it; an init re-run on an existing project must not stomp the file).
#
# The defaults shown here are pulled from `config.DEFAULT_*` constants
# (DEFAULT_TASK_MAX_TURNS, DEFAULT_IDEATION_MAX_TURNS,
# DEFAULT_CONTROL_TIMEOUT_S, DEFAULT_TASK_TIMEOUT_S,
# DEFAULT_VERIFY_TIMEOUT_S) so the template never drifts from the source
# of truth — a future bump to any constant updates the rendered template
# the next time a fresh project is init'd. AGENT_MODEL / AGENT_EFFORT /
# MM_CHANNELS / IDEATION_TRIGGER_TASK_COUNT have non-constant or "(unset)"
# defaults that are inlined as string literals (matching how `ap2/README.md`
# and `ap2/howto.md`'s `## Configuration knobs` enumerate them).
#
# Note: `.cc-autopilot/env` is gitignored (`NESTED_GITIGNORE_BLOCKS`
# above), so the generated file is per-project local — the TEMPLATE
# constant here is the committed source-of-truth; the rendered file is
# not.
ENV_TEMPLATE = f"""\
# .cc-autopilot/env — per-project tunables for the ap2 daemon.
#
# Lines are KEY=VALUE. Blank lines and `#` comments are ignored. Quoted
# values (single or double) are stripped. Shell exports take precedence
# over this file at daemon-start (TB-271 `note_initial_applied`); a key
# set ONLY in shell will keep that value even if added here later
# (un-export + restart, or set here before daemon-start, to fix).
#
# Every knob below is commented out — the code default (shown inline)
# applies unless you uncomment. Edit + save: the daemon hot-reloads
# tunables at the top of every tick (TB-271 `env_reload`); a restart is
# only required for the lifecycle knobs (`AP2_WEB_PORT`,
# `AP2_WEB_DISABLED`, `AP2_MM_CHANNELS`) that wire stateful resources.
#
# See `ap2/howto.md` `## Configuration knobs` for the full list with
# descriptions, and `ap2/config.py` for the in-source DEFAULT_* constants.

# Project-wide regression gate. Runs after every successful task agent
# commit; failure routes the task through retry like any other crash.
# Unset (default) = no project-wide gate runs.
# AP2_VERIFY_CMD=uv run pytest -q

# Timeout (s) for `AP2_VERIFY_CMD`. Bump if your suite outgrows the
# default; `ap2 doctor` warns when set below observed-typical successful
# verify duration.
# AP2_VERIFY_TIMEOUT_S={DEFAULT_VERIFY_TIMEOUT_S}

# Per-task SDK query timeout (s). Bigger refactors blow past the
# default; this project's own env bumps to 3600 (TB-122 hit
# `error_max_turns` at 51 turns and the wall-clock cap also bit).
# AP2_TASK_TIMEOUT_S={DEFAULT_TASK_TIMEOUT_S}

# Max turns per task agent. Default raised from 50 → 200 in TB-278 after
# TB-122 hit the old wall at 51 turns; bump further (e.g. 500) for
# heavy-refactor projects.
# AP2_TASK_MAX_TURNS={DEFAULT_TASK_MAX_TURNS}

# Per-control-agent (mattermost / cron / ideation) SDK query timeout (s).
# Default raised from 300 → 1200 in TB-278 — `xhigh`-effort ideation
# routinely blew the old 5-min wall against a populated
# progress.md / operator_log.md.
# AP2_CONTROL_TIMEOUT_S={DEFAULT_CONTROL_TIMEOUT_S}

# Max turns for the ideation agent. Default raised from 30 → 100 in
# TB-278 after a goal.md rewrite mid-cycle hit `error_max_turns` at 31.
# `AP2_CONTROL_TIMEOUT_S` still bounds runaway wall-clock.
# AP2_IDEATION_MAX_TURNS={DEFAULT_IDEATION_MAX_TURNS}

# Fire ideation when Ready+Backlog count is BELOW this threshold (and
# Active is empty). Doubles as the per-cycle proposal-slot budget. Set
# to 1 for the legacy "fire only when working queue is fully empty"
# behavior; raise for projects with very fluid scope.
# AP2_IDEATION_TRIGGER_TASK_COUNT=3

# Model passed to `ClaudeAgentOptions` for task / control / verifier /
# janitor agents. Empty-string env DOES propagate (only an ABSENT key
# falls through to the default).
# AP2_AGENT_MODEL=claude-opus-4-7

# Global reasoning-effort level (low|medium|high|xhigh|max). Per-job
# sub-knobs (`AP2_STATUS_REPORT_EFFORT`, `AP2_VERIFY_JUDGE_EFFORT`,
# `AP2_JANITOR_JUDGE_EFFORT`) override this for their respective agents.
# AP2_AGENT_EFFORT=xhigh

# Comma-separated Mattermost channel IDs the daemon polls for `@bot`
# mentions. Unset = no Mattermost integration. This is a LIFECYCLE knob
# (FIXED_KNOBS) — changing it requires `ap2 stop && ap2 start` to
# re-subscribe.
# AP2_MM_CHANNELS=

# Opt-in immediate Mattermost push when the attention detector emits a
# fresh `attention_raised` (TB-297). Default OFF — the
# status-report cron stays the routine push channel for fresh projects.
# Flip to `1` once you've sampled the detector cadence (`ap2 attention`
# / `ap2 logs --type attention_raised`) and confirmed the rate is low
# enough not to noise the channel. Requires `AP2_MM_CHANNELS` above to
# be set; debounce piggybacks on `AP2_ATTENTION_DEBOUNCE_S` (default 6h).
# AP2_ATTENTION_IMMEDIATE_PUSH=0

# Per-task token ceiling for auto-approved tasks (TB-224). Unset / 0 =
# cap disabled (operators who haven't budgeted their project don't get
# a hardcoded ceiling surprising them). When set, an auto-approved
# task whose `task_run_usage` reports input+output tokens above this
# value trips `auto_approve_paused:per_task_cap` — dispatch halts until
# `ap2 ack auto_approve_window_resume`.
# AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP=

# 24h-rolling cumulative token ceiling across all auto-approved tasks
# (TB-224). Unset / 0 = cap disabled. When set, the sum of input+output
# tokens across auto-approved tasks in the last 24h crossing this value
# trips `auto_approve_paused:window_token_cap_exceeded`. The pre-trip
# nudge (`cost_cap_approach`, TB-290) fires at
# `AP2_AUTO_APPROVE_COST_APPROACH_PCT` (default 75)% of this cap so the
# walk-away operator gets a budget-spending heads-up before the halt.
# AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP=
"""


# TB-305: knobs intentionally absent from `ENV_TEMPLATE` above. Each
# entry carries a `# reason: ...` comment on the same line categorizing
# why operators don't need it in the per-project scaffold — debug /
# test only, an internal default rarely tuned, an integration secret
# set via shell export, covered by a sibling global, etc. The
# `test_every_env_knob_in_template_or_exempt` CI gate
# (`ap2/tests/test_docs_drift.py`) asserts every `AP2_*` knob in
# source is EITHER a substring of `ENV_TEMPLATE` above OR a member of
# this set; a future knob-adder's PR fails the gate until one of those
# is true. The `# reason:` comment is the audit trail for a future
# reader asking "should this graduate to the template?" — the gate
# forces the decision at knob-add time instead of letting drift
# compound silently the way it did between TB-278 (which authored the
# template) and TB-305 (which added this gate after ~40 knobs
# accumulated outside it).
#
# Pattern parallels `_DOCS_DRIFT_EXEMPT_ENV_KNOBS` in
# `ap2/tests/test_docs_drift.py` (which exempts private-constant
# look-alikes from the howto-mention gate) and
# `HOT_RELOADABLE_KNOBS` / `FIXED_KNOBS` in `ap2/env_reload.py` (which
# split the same source-of-truth knob universe along the
# can-hot-reload axis). Living next to `ENV_TEMPLATE` keeps the
# template-vs-exempt decision a one-file edit for the knob-adder.
_TEMPLATE_EXEMPT_KNOBS: frozenset[str] = frozenset({
    "AP2_ATTENTION_DEBOUNCE_S",              # reason: detector-sensitivity tuning, default 6h rarely tuned
    "AP2_AUTO_APPROVE",                      # reason: opt-in main toggle; operators flip via shell export after sampling the dry-run audit surface
    "AP2_AUTO_APPROVE_COST_APPROACH_PCT",    # reason: internal default (75%), rarely tuned
    "AP2_AUTO_APPROVE_DRY_RUN",              # reason: debug/test only
    "AP2_AUTO_APPROVE_FREEZE_THRESHOLD",     # reason: internal default, rarely tuned
    "AP2_AUTO_APPROVE_GATE_TAGS",            # reason: internal default, rarely tuned
    "AP2_AUTO_APPROVE_NOISY_PAUSE_DISABLED", # reason: debug/test only
    "AP2_AUTO_DIAGNOSE_COOLDOWN_S",          # reason: internal default, rarely tuned
    "AP2_AUTO_DIAGNOSE_IDLE_THRESHOLD_S",    # reason: internal default, rarely tuned
    "AP2_AUTO_UNFREEZE_DRY_RUN",             # reason: debug/test only
    "AP2_AUTO_UNFREEZE_FIX_SHAPES",          # reason: operator opt-in allowlist; set via shell export so a per-project template doesn't broaden the auto-patch surface by default
    "AP2_AUTO_UNFREEZE_MAX_PER_DAY",         # reason: internal default, rarely tuned
    "AP2_AUTO_UNFREEZE_MAX_PER_TASK",        # reason: internal default, rarely tuned
    "AP2_CHANNEL_FILE_PATH",                 # reason: TB-312 core sibling channel adapter target; default path is fine for most projects, only set via shell export when explicitly wiring `FileAppendChannelAdapter` to a non-default location
    "AP2_CONTROL_MAX_TURNS",                 # reason: internal default, rarely tuned
    "AP2_EVENT_CONTEXT",                     # reason: internal default, rarely tuned
    "AP2_FOCUS_ADVANCE_EMPTY_CYCLES",        # reason: internal default, rarely tuned
    "AP2_FOCUS_AUTO_ADVANCE_DISABLED",       # reason: debug/test only
    "AP2_IDEATION_COOLDOWN_S",               # reason: internal default, rarely tuned
    "AP2_IDEATION_DISABLED",                 # reason: debug/test only
    "AP2_IDEATION_SCRUB_MODEL",              # reason: covered by global AP2_AGENT_MODEL for most projects
    "AP2_JANITOR_DISABLED",                  # reason: debug/test only — kill switch for the janitor component (TB-309); operators flip via shell export, not the per-project template
    "AP2_JANITOR_JUDGE_MAX_TURNS",           # reason: internal default, rarely tuned
    "AP2_JANITOR_MAX_FINDINGS_LLM",          # reason: internal default, rarely tuned
    "AP2_MAX_RETRIES",                       # reason: internal default, rarely tuned
    "AP2_MM_BOT_USER_ID",                    # reason: integration secret; set via shell export alongside AP2_MM_CHANNELS
    "AP2_MM_MENTION",                        # reason: integration default, rarely tuned
    "AP2_MM_REPORT_CHANNEL",                 # reason: integration secret; set via shell export alongside AP2_MM_CHANNELS
    "AP2_MM_TEAM_ID",                        # reason: integration secret; set via shell export alongside AP2_MM_CHANNELS
    "AP2_MM_TICK_S",                         # reason: internal default, rarely tuned
    "AP2_PROJECT_NAME",                      # reason: defaults to project_root.name; operator renames via shell export
    "AP2_TASK_FROZEN_RECENCY_S",             # reason: internal default, rarely tuned
    "AP2_TASK_STUCK_THRESHOLD_S",            # reason: internal default, rarely tuned
    "AP2_TICK_S",                            # reason: internal default, rarely tuned
    "AP2_VALIDATOR_JUDGE_DISABLED",          # reason: debug/test only
    "AP2_VALIDATOR_JUDGE_MAX_TOKENS",        # reason: internal default, rarely tuned
    "AP2_VALIDATOR_JUDGE_MAX_TURNS",         # reason: internal default, rarely tuned
    "AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD",   # reason: internal default, rarely tuned
    "AP2_VALIDATOR_JUDGE_TIMEOUT_S",         # reason: internal default, rarely tuned
    "AP2_VERIFY_JUDGE_MAX_TURNS",            # reason: internal default, rarely tuned
    "AP2_WEBHOOK_URL",                       # reason: TB-312 integration secret for the `WebhookChannelAdapter`; set via shell export alongside the project-specific webhook destination (Slack incoming webhook, Discord, etc.)
})


# Lines that go into <project>/.cc-autopilot/.gitignore. Grouped by purpose so
# diffs against an existing file are minimal and readable.
NESTED_GITIGNORE_BLOCKS: list[tuple[str, list[str]]] = [
    ("Runtime — per-user, not committed", [
        "flag",
        "checkpoints/",
        "sessions/",
        "metrics/",
        "decisions.log",
        "context.json",
        "events.jsonl",
        "daemon.pid",
        "daemon.log",
        "paused",
        # Cron last-fired / mm cursor / auto-diagnose cooldown stay
        # gitignored: rollback (TB-111) should NOT re-fire crons,
        # replay mattermost, or re-trigger the watchdog. Ephemeral
        # runtime state lives outside git so it flows forward across
        # rollback boundaries.
        "cron_state.json",
        "mm_state.json",
        "auto_diagnose_state.json",
        # `retry_state.json` was here pre-TB-112; it's now committed
        # as part of `_STATE_FILE_NAMES` so rollback restores per-task
        # retry budgets coherently.
        # TB-131: operator-queue jsonl (CLI / MM-handler appends drained
        # at the next tick) and its applied-uuid state file. Ephemeral
        # runtime — the drained ops materialize as TASKS.md edits which
        # ARE committed; the queue itself shouldn't ride along.
        "operator_queue.jsonl",
        "operator_queue_state.json",
        # TB-226: focus-list runtime pointer (which `## Current focus:`
        # heading in goal.md is active, the heuristic empty-cycles
        # counter, exhausted titles, roadmap-complete ack idx). In-memory
        # runtime state — goal.md itself stays operator-owned and IS
        # committed; the pointer flowing forward across rollbacks would
        # also re-fire `focus_advanced` events redundantly, so the
        # pointer (like `cron_state.json`) should restart fresh.
        "focus_pointer.json",
        # TB-260: per-daemon-lifetime env-file-mtime stash powering the
        # `.cc-autopilot/env` stale-detection surface. Rewritten at
        # every daemon start by `_capture_env_mtime_at_start`; the
        # pinned mtime is only meaningful for the CURRENT daemon
        # process, so an `ap2 rollback` that restored a prior value
        # would either resurrect a stale mtime baseline (false-positive
        # WARN against the live env file) or paper over the next real
        # bump. Runtime-only, like its `cron_state.json` /
        # `operator_queue_state.json` neighbors above.
        "daemon_state.json",
        # TB-297: sticky `warned_no_destination` flag for the opt-in
        # immediate-MM-push surface (`daemon._maybe_push_attention`).
        # Same runtime-state semantics as `auto_diagnose_state.json` —
        # an `ap2 rollback` should not resurrect a stale "we already
        # warned about a missing AP2_MM_CHANNELS" flag, and a fresh
        # daemon process should re-warn against a freshly-misconfigured
        # env file.
        "attention_push_state.json",
    ]),
    ("Per-run prompt + stream dumps for failure diagnosis (kept only on failure)", [
        "debug/",
    ]),
    ("Pipeline log dirs (TB-81) — local debug-only, never committed", [
        "pipelines/",
    ]),
    ("Local/sandbox-specific env (secrets, channel IDs) — keep out of git", [
        "env",
    ]),
    ("Runtime fcntl locks (cron_state.json.lock, retry_state.json.lock, etc.)", [
        "*.lock",
    ]),
    ("On-disk backups created during ap2 upgrades", [
        "*.bak",
    ]),
]

# Lines that go into the project's ROOT .gitignore (above .cc-autopilot/).
# Only entries for files ap2 creates outside .cc-autopilot/.
ROOT_GITIGNORE_BLOCKS: list[tuple[str, list[str]]] = [
    ("ap2 board lock (runtime)", [
        "TASKS.md.lock",
    ]),
]


@dataclass
class InitReport:
    project_root: Path
    nested_gitignore_added: list[str] = field(default_factory=list)
    root_gitignore_added: list[str] = field(default_factory=list)
    tasks_dir_created: bool = False
    tasks_md_created: bool = False
    progress_md_created: bool = False
    claude_md_created: bool = False
    claude_md_autopilot_added: bool = False
    goal_md_created: bool = False
    ideation_state_md_created: bool = False
    insights_dir_created: bool = False
    # TB-278: track whether `.cc-autopilot/env` got the documented template
    # this run. False on re-init when the operator already has a populated
    # env file (the template MUST NOT clobber operator secrets / channel
    # IDs); True only on first init of a fresh project.
    env_template_created: bool = False

    def print(self) -> None:
        if self.nested_gitignore_added:
            print(f"  .cc-autopilot/.gitignore: +{len(self.nested_gitignore_added)} entries")
            for line in self.nested_gitignore_added:
                print(f"    + {line}")
        else:
            print("  .cc-autopilot/.gitignore: up to date")
        if self.root_gitignore_added:
            print(f"  .gitignore: +{len(self.root_gitignore_added)} entries")
            for line in self.root_gitignore_added:
                print(f"    + {line}")
        else:
            print("  .gitignore: up to date")
        print(f"  .cc-autopilot/tasks/: {'created' if self.tasks_dir_created else 'exists'}")
        print(f"  TASKS.md: {'created' if self.tasks_md_created else 'exists'}")
        print(f"  .cc-autopilot/progress.md: {'created' if self.progress_md_created else 'exists'}")
        print(f"  goal.md: {'created (template — fill in)' if self.goal_md_created else 'exists'}")
        print(f"  .cc-autopilot/ideation_state.md: {'created (placeholder)' if self.ideation_state_md_created else 'exists'}")
        print(f"  .cc-autopilot/insights/: {'created (placeholder index)' if self.insights_dir_created else 'exists'}")
        print(f"  .cc-autopilot/env: {'created (commented template)' if self.env_template_created else 'exists (kept)'}")
        if self.claude_md_created:
            print(f"  CLAUDE.md: created (with ## Autopilot)")
        elif self.claude_md_autopilot_added:
            print(f"  CLAUDE.md: appended ## Autopilot section")
        else:
            print(f"  CLAUDE.md: ## Autopilot section already present")


def _existing_entries(text: str) -> set[str]:
    """Pattern entries already in a gitignore (skip blank lines and comments)."""
    return {
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def _union_gitignore(path: Path, blocks: list[tuple[str, list[str]]]) -> list[str]:
    """Append missing entries from `blocks` to `path`. Returns the lines added.

    If `path` doesn't exist, it's created and every block is written. If it
    exists, only entries not already present (by exact-string match) are
    appended, each grouped under its header. Headers are written only when the
    block contributes at least one new entry, so re-runs don't accumulate
    empty header sections.
    """
    text = path.read_text() if path.exists() else ""
    existing = _existing_entries(text)
    added: list[str] = []
    chunks: list[str] = []
    for header, entries in blocks:
        new_entries = [e for e in entries if e not in existing]
        if not new_entries:
            continue
        chunks.append(f"\n# {header}\n" + "\n".join(new_entries) + "\n")
        added.extend(new_entries)
        existing.update(new_entries)  # protect against intra-block dups

    if not added:
        return []

    path.parent.mkdir(parents=True, exist_ok=True)
    if text and not text.endswith("\n"):
        text += "\n"
    path.write_text(text + "".join(chunks))
    return added


# Word-boundary + any-trailing-content match (TB-102): tolerates
# `## Autopilot (per-project)` etc. while still rejecting look-alikes
# like `## AutopilotPlus`.
_AUTOPILOT_HEADER_RE = re.compile(r"^##\s+Autopilot\b[^\n]*$", re.M)


def _ensure_file(path: Path, content: str) -> bool:
    """Write `content` to `path` only if `path` does not already exist."""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return True


def _ensure_claude_md_autopilot(claude_md: Path) -> tuple[bool, bool]:
    """Ensure CLAUDE.md exists and has a `## Autopilot` section.

    Returns `(claude_md_created, autopilot_appended)`:
    - `(True, False)` — CLAUDE.md was missing; created with header + Autopilot.
    - `(False, True)` — CLAUDE.md existed without Autopilot; we appended.
    - `(False, False)` — Autopilot section already present; nothing changed.
    """
    if not claude_md.exists():
        claude_md.parent.mkdir(parents=True, exist_ok=True)
        claude_md.write_text("# CLAUDE.md\n\n" + CLAUDE_AUTOPILOT_TEMPLATE)
        return True, False
    text = claude_md.read_text()
    if _AUTOPILOT_HEADER_RE.search(text):
        return False, False
    sep = "" if text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
    claude_md.write_text(text + sep + CLAUDE_AUTOPILOT_TEMPLATE)
    return False, True


def init_project(project_root: Path) -> InitReport:
    """Scaffold ap2 gitignores, tasks dir, board template, autopilot config.

    Idempotent — every step skips if the target already exists. Never clobbers
    user content; CLAUDE.md only gains a fresh `## Autopilot` block when the
    file lacks one entirely.
    """
    project_root = project_root.resolve()
    autopilot_dir = project_root / AUTOPILOT_DIR_NAME

    nested_added = _union_gitignore(autopilot_dir / ".gitignore", NESTED_GITIGNORE_BLOCKS)
    root_added = _union_gitignore(project_root / ".gitignore", ROOT_GITIGNORE_BLOCKS)

    tasks_dir = autopilot_dir / "tasks"
    tasks_dir_created = not tasks_dir.exists()
    tasks_dir.mkdir(parents=True, exist_ok=True)

    # TB-89: insights directory + placeholder index. Lazy regen kicks in
    # the first time a cron tick fires after a real insight file lands.
    insights_dir = autopilot_dir / "insights"
    insights_dir_created = not insights_dir.exists()
    insights_dir.mkdir(parents=True, exist_ok=True)
    _ensure_file(insights_dir / "_index.md", INSIGHTS_INDEX_PLACEHOLDER)

    tasks_md_created = _ensure_file(project_root / "TASKS.md", TASKS_TEMPLATE)
    progress_md_created = _ensure_file(autopilot_dir / "progress.md", PROGRESS_TEMPLATE)
    goal_md_created = _ensure_file(project_root / "goal.md", GOAL_TEMPLATE)
    ideation_state_md_created = _ensure_file(
        autopilot_dir / "ideation_state.md", IDEATION_STATE_TEMPLATE,
    )
    # TB-278: scaffold the documented `.cc-autopilot/env` template ONLY when
    # the file is absent. `_ensure_file` returns False without writing when
    # the path exists, preserving operator secrets / channel IDs / tuned
    # overrides on every re-init. The path itself is gitignored
    # (`NESTED_GITIGNORE_BLOCKS`); the TEMPLATE source above is committed.
    env_template_created = _ensure_file(autopilot_dir / "env", ENV_TEMPLATE)
    claude_md_created, autopilot_added = _ensure_claude_md_autopilot(project_root / "CLAUDE.md")

    return InitReport(
        project_root=project_root,
        nested_gitignore_added=nested_added,
        root_gitignore_added=root_added,
        tasks_dir_created=tasks_dir_created,
        tasks_md_created=tasks_md_created,
        progress_md_created=progress_md_created,
        claude_md_created=claude_md_created,
        claude_md_autopilot_added=autopilot_added,
        goal_md_created=goal_md_created,
        ideation_state_md_created=ideation_state_md_created,
        insights_dir_created=insights_dir_created,
        env_template_created=env_template_created,
    )

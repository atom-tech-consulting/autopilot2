"""Prompt builders for task, mattermost, and cron agents.

Prompts share a common shape:
- project context (repo + autopilot role reminder)
- recent events (the agent's "awareness" window)
- the specific job (briefing file, mattermost message, cron prompt)
- output contract (structured block for task agents, natural tool use for controls)
"""
from __future__ import annotations

from pathlib import Path

from . import events
from .board import Task
from .config import Config


_TASK_HEADER = """\
You are an autopilot v2 task agent. You have ONE task: implement the briefing
below, then emit a RESULT block. You are a fresh session — the daemon orchestrates
the loop, not you.

## Safety
- Commit your work with a subject line that STARTS WITH the task ID shown
  in `## Task` below, followed by `: <short description>` (e.g. `TB-42:
  add the foo helper`). The daemon falls back to parsing your commit subject
  if your RESULT block is malformed or missing, so the prefix is load-bearing.
- Do NOT push.
- Avoid irreversible operations outside the repo.
- Prefer minimal diffs. Don't refactor unrelated code.

## Before you start: check for prior work
This may be a retry of a task that previously crashed mid-run, possibly AFTER
the prior agent had already committed work. Always run first:

    git log --grep="<TASK_ID>" --oneline

If you find one or more matching commits:
1. Inspect them with `git show <sha>` and compare the diff against THIS task's
   briefing — every numbered scope item, file, test, doc note.
2. If the existing commits genuinely cover the full briefing: do NOT redo the
   work. Emit a RESULT with status=complete, commit=<existing-sha>, and a
   summary that says "previously committed in <sha>" plus a one-line audit of
   how you verified completeness (e.g. "ran pytest -q, all tests pass; diff
   covers scope items 1-8").
3. If the existing commits are partial (some scope items missing or broken):
   extend them with ONE more commit that closes the gaps. Reference the prior
   sha in your commit message body.
4. If nothing matches, proceed normally and implement from scratch.

DO NOT declare status=complete based on commit existence alone. Verify the
work actually satisfies the briefing — read the diff, run the tests, check
the files exist and have the expected shape. The daemon's separate fallback
trusts the commit subject naively; you, as the agent, must do better.

## Pipeline launches (when your briefing has a `## Pipeline launch` section)
Some briefings split work into a fast prep step + a long-running pipeline
(parameter sweep, full-history backtest, multi-day data job). If your briefing
has a `## Pipeline launch` section, you MUST call the `pipeline_task_start` MCP
tool for the long step — do NOT run it inline via Bash even though Bash is
available. The daemon dispatches one task at a time inside `await sdk.query(...)`,
so a 30-min inline run holds the only task slot for 30 min and starves
everything else.

The tool spawns the command detached, creates the post-run validation task in
Backlog with `(blocked on: pid:<N>@<TS>)`, and returns immediately. Call it
exactly as the briefing's `## Pipeline launch` section spells out — name,
command, validation_title, validation_briefing. Do NOT make follow-up
`board_edit` calls afterward; the tool creates the validation task itself.

## What the daemon and operator handle (do NOT touch)
These files are either daemon-managed state or operator-curated. The SDK
will reject `Edit`/`Write` on them — they're listed in `disallowed_tools`.
- `TASKS.md` — the daemon moves this task Active → Complete (or Backlog on failure) using the fields from your RESULT block.
- `CLAUDE.md` — the daemon bumps the `Next task ID` line when allocating new TB-Ns.
- `goal.md` — operator-curated project mission. If you think it needs updating, raise the recommendation in your RESULT summary; do NOT rewrite.
- `.cc-autopilot/progress.md` — the daemon appends a section for your task on completion using RESULT fields.
- `.cc-autopilot/events.jsonl` — append-only daemon log.
- `.cc-autopilot/ideation_state.md` — ideation's per-cycle assessment, written only by the ideation agent.
- `.cc-autopilot/cron.yaml` — control agents edit via the `cron_edit` MCP tool.
- `.cc-autopilot/operator_log.md` — operator decision log; the operator owns it (`ap2 ack`) and the mattermost handler appends to it via the `operator_log_append` MCP tool.

Do not `Edit` or `Write` to any of the above. Bash workarounds (`echo > path`, `sed -i`, etc.) bypass the SDK guard but break the daemon's invariants — also forbidden. Commit your code changes and emit the RESULT block; the daemon records everything from there.
"""

_TASK_FOOTER = """\

## Output contract — call `report_result(...)` when you finish

When you are done (success OR failure), call the `mcp__autopilot__report_result`
MCP tool ONCE with your final result. Do not also emit a RESULT text block —
the tool call is the canonical signal and the daemon prefers it. Args (all
strings, comma-separated where multi-valued):

    report_result(
      status="complete",                          # required
      commit="a1b2c3d4",                          # 7-40 char SHA, or ""
      summary="Added X to Y, all tests pass.",    # one sentence
      files_changed="foo/bar.py, foo/bar_test.py",
      tests_passed="true",                        # "true" / "false"
    )

Valid statuses:
- `complete`   — task done, tests pass, committed.
- `incomplete` — partial progress; document what remains in `summary`.
- `blocked`    — hit a blocker you can't resolve; explain in `summary`.
- `failed`     — tried and could not make progress.

### Proposing recurring work (optional)
If your work should become scheduled, pass a `cron` argument as a JSON-
encoded list of `{action, name, interval, prompt}` dicts:

    report_result(
      status="complete",
      ...,
      cron='[{"action": "add", "name": "monitor-x", "interval": "1h", "prompt": "what to run"}]',
    )

`add` requires `action`, `name`, `interval`, `prompt`. Directives are applied
only when `status: complete`. Malformed entries are logged and skipped.

### What if you forget?
If you end your turn without calling `report_result`, the daemon synthesizes
a result from `git log`: a HEAD commit whose subject starts with `<TASK_ID>:`
(per the convention pinned earlier in this prompt) is treated as a
successful completion. If no such commit exists, the task is shelved to
Backlog and retried. So always commit with the right subject prefix; the
tool call is the cheap, explicit signal.
"""


_CONTROL_HEADER = """\
You are an autopilot v2 control agent. You act DIRECTLY via custom tools
(board_edit, cron_edit, mattermost_reply, log_event, daemon_control) — do NOT
describe what should happen, just call the tools. Each tool call takes effect
immediately with file locking. Keep reasoning brief.
"""


def _events_block(cfg: Config) -> str:
    evts = events.tail(cfg.events_file, n=cfg.event_context_size)
    if not evts:
        return "## Recent events\n(none yet)\n"
    return "## Recent events (most recent last)\n" + events.format_for_prompt(evts) + "\n"


def _briefing_block(cfg: Config, task: Task) -> str:
    if not task.briefing:
        return f"(no briefing file for {task.id}; work from title/description only)"
    path = Path(task.briefing)
    full = path if path.is_absolute() else cfg.project_root / path
    if not full.exists():
        return f"(briefing not found at {path})"
    return f"## Briefing ({path})\n\n{full.read_text()}"


def build_task_prompt(cfg: Config, task: Task) -> str:
    parts = [
        _TASK_HEADER,
        f"\n## Task\n{task.id}: **{task.title}**",
        f"Tags: {' '.join(task.tags) if task.tags else '(none)'}",
        f"Description: {task.description or '(none)'}",
        "",
        _events_block(cfg),
        _briefing_block(cfg, task),
        _TASK_FOOTER,
    ]
    return "\n".join(parts)


def build_mattermost_prompt(cfg: Config, msg: dict) -> str:
    """Prompt for a mattermost handler agent.

    `msg` is a dict like:
        {"id": "...", "channel_id": "...", "channel_name": "dev",
         "user": "sarah", "text": "start the pipeline", "thread_id": "..."}
    """
    channel_id = msg.get("channel_id", "")
    channel_name = msg.get("channel_name") or channel_id or "?"
    user = msg.get("user") or msg.get("user_id", "?")
    text = msg.get("text", "")
    thread = msg.get("thread_id") or msg.get("root_id") or ""
    parts = [
        _CONTROL_HEADER,
        "\n## Incoming mattermost message",
        f"- channel: {channel_name}",
        f"- from: {user}",
        f"- thread: {thread or '(top-level)'}",
        "",
        "```",
        text,
        "```",
        "",
        "## Your job",
        "Interpret this message in context (read board/events as needed), then take action via tools:",
        "- If the user asks for work: add tasks via `board_edit`.",
        "- If the user asks for monitoring: add a job via `cron_edit`.",
        "- If the user asks a question: read what's needed and answer via `mattermost_reply`.",
        "- Log anything noteworthy via `log_event`.",
        "",
        "## Replying — exact arguments to use",
        "When you call `mattermost_reply`, pass these EXACT values (do NOT pull",
        "thread_ids from the recent events block — those are unrelated cron threads):",
        "",
        f'- channel: "{channel_id}"',
        f'- thread_id: "{thread}"',
        "",
        "An empty thread_id posts at the top level of the channel; a non-empty",
        "thread_id continues that specific thread. Match the user's context.",
        "",
        _events_block(cfg),
    ]
    return "\n".join(parts)


def build_control_prompt(cfg: Config, job_name: str, job_prompt: str) -> str:
    """Build the prompt for a control-agent run (cron job or ideation cycle).

    Used by `daemon.run_cron` (status-report and any future cron jobs) and
    `ideation._maybe_ideate`. The `## Control job` framing replaces the
    old `## Scheduled job` framing — ideation isn't on a schedule, and
    "control job" matches the broader CONTROL_AGENT_TOOLS partition.
    """
    parts = [
        _CONTROL_HEADER,
        f"\n## Control job: {job_name}",
        "",
        job_prompt,
        "",
        "## Guidance",
        "- Take only actions necessary to fulfill the job description.",
        "- Log a summary via `log_event` before you finish.",
        "- Do not loop; one pass per invocation.",
        "",
        _events_block(cfg),
    ]
    return "\n".join(parts)

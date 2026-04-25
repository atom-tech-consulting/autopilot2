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

## What the daemon handles (do NOT touch)
The daemon manages state files for you — do not edit them:
- `TASKS.md` — the daemon moves this task Active → Complete (or Backlog on failure) using the fields from your RESULT block.
- `.cc-autopilot/progress.md` — the daemon appends a section for your task on completion using RESULT fields.
- `.cc-autopilot/events.jsonl` — append-only daemon log.

Do not `Edit` or `Write` to any of the above. Just commit your code changes and emit the RESULT block; the daemon records everything from there.
"""

_TASK_FOOTER = """\

## Output contract
When you are finished (success OR failure), end your FINAL message with a single
fenced RESULT block — the daemon parses this and updates the board. Example:

```
RESULT:
status: complete
commit: a1b2c3d
summary: Added X to Y, all tests pass.
files_changed: foo/bar.py, foo/bar_test.py
tests_passed: true
```

Valid statuses:
- `complete`  — task done, tests pass, committed.
- `incomplete` — partial progress; document what remains in the summary.
- `blocked`  — ran into a blocker you can't resolve; explain in summary.
- `failed`   — tried and could not make progress.

### Proposing recurring work (optional)
If the work you did should become scheduled, include one or more `cron:` lines
inside the RESULT block. Each is a single line, `key=value` pairs with shell
quoting allowed:

    cron: add name=<name> interval=<1h|2d|30m|...> prompt="what to run"
    cron: remove name=<name>
    cron: update name=<name> interval=<...>

`add` requires `name`, `interval`, and `prompt`. Directives are applied only
when `status: complete`. Malformed directives are logged and skipped.
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
    channel = msg.get("channel_name") or msg.get("channel_id", "?")
    user = msg.get("user") or msg.get("user_id", "?")
    text = msg.get("text", "")
    thread = msg.get("thread_id") or msg.get("root_id") or ""
    parts = [
        _CONTROL_HEADER,
        "\n## Incoming mattermost message",
        f"- channel: {channel}",
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
        "- Always acknowledge on the same channel/thread via `mattermost_reply` when you act.",
        "- Log anything noteworthy via `log_event`.",
        "",
        _events_block(cfg),
    ]
    return "\n".join(parts)


def build_cron_prompt(cfg: Config, job_name: str, job_prompt: str) -> str:
    parts = [
        _CONTROL_HEADER,
        f"\n## Scheduled job: {job_name}",
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

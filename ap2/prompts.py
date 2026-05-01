"""Prompt builders for task, mattermost, and cron agents.

Prompts share a common shape:
- project context (repo + autopilot role reminder)
- recent events (the agent's "awareness" window)
- the specific job (briefing file, mattermost message, cron prompt)
- output contract (structured block for task agents, natural tool use for controls)
"""
from __future__ import annotations

import datetime as _dt
import subprocess
from pathlib import Path

from . import events
from .board import Board, Task
from .config import Config
from .tools import CONTROL_AGENT_TOOLS, MM_HANDLER_TOOLS_FULL, MM_HANDLER_TOOLS_RESTRICTED


# TB-128: status-report cron was emitting reports with stale headline
# timestamps (the agent re-rendered text from a prior context's cache
# rather than the current moment). Fix: inject a deterministic "right
# now" snapshot — UTC timestamp, board counts, recent commits — at the
# top of every control prompt. The status-report prompt then references
# this block by name ("use the `now:` timestamp verbatim"), so there's
# no ambiguity about which timestamp belongs in the headline.
def _current_state_block(cfg: Config) -> str:
    now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    counts_line = "(board not found)"
    if cfg.tasks_file.exists():
        try:
            board = Board.load(cfg.tasks_file)
            c = {s: len(board.sections.get(s, [])) for s in
                 ["Active", "Ready", "Backlog", "Pipeline Pending",
                  "Complete", "Frozen"]}
            counts_line = (
                f"{c['Active']}A / {c['Ready']}R / {c['Backlog']}B / "
                f"{c['Pipeline Pending']}P / {c['Complete']}C / "
                f"{c['Frozen']}F"
            )
        except Exception as e:  # noqa: BLE001
            counts_line = f"(board load error: {type(e).__name__})"

    commits = "(git log unavailable)"
    if (cfg.project_root / ".git").exists():
        try:
            proc = subprocess.run(
                [
                    "git", "-c", "safe.directory=*",
                    "-C", str(cfg.project_root),
                    "log", "--oneline", "-n", "10",
                ],
                capture_output=True, text=True, timeout=5,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                commits = proc.stdout.strip()
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    return (
        "## Current state (rendered just before this prompt was sent)\n"
        f"- now: {now}\n"
        f"- board: {counts_line} (Active/Ready/Backlog/Pipeline-Pending/Complete/Frozen)\n"
        "- recent commits (HEAD~10):\n"
        + "\n".join(f"  {ln}" for ln in commits.splitlines())
        + "\n"
    )


# TB-128: the status-report cron has historically posted reports with
# headline timestamps copied from prior runs' contexts. The fix is two-
# pronged: (1) the daemon-injected `## Current state` block above gives
# a deterministic `now:` value; (2) this addendum tells the agent
# explicitly to use that value verbatim, re-read events.jsonl + the
# board fresh, and skip posting if nothing has changed since the last
# `status_report` event. The deterministic skip-gate in
# `daemon.run_cron` is belt-and-braces in case the agent ignores this.
_STATUS_REPORT_CONTRACT = """\
## Status-report contract (TB-128)
- The headline timestamp in your post MUST be the literal `now:` value
  from the `## Current state` block above. Do NOT reuse a timestamp from
  the events tail, your own prior turns, or any cached briefing.
- Re-read `.cc-autopilot/events.jsonl` (last 50 lines) and `TASKS.md`
  with the `Read` tool before composing the post — the embedded events
  tail is a courtesy, not a substitute. The board counts in the snapshot
  block above are authoritative.
- Skip the Mattermost post entirely if nothing meaningful has happened
  since the last `status_report` event in the tail (i.e. no new
  task_start / task_complete / verification_failed / pipeline_* /
  retry_exhausted / daemon_pause / daemon_resume / operator_ack /
  cron_proposed / ideation_complete events). When you skip, still call
  `log_event(type="status_report", summary="skipped: no activity since
  <ts>")` so the daemon sees a marker.
- Always call `log_event(type="status_report", summary=...)` before
  finishing — posted or skipped.
"""


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

## Long-running work — use `pipeline_task_start`
Before you start, estimate how long your work will take to run end-to-end.
The daemon dispatches one task at a time inside `await sdk.query(...)` and
will hard-cap the run at `AP2_TASK_TIMEOUT_S` (default 1h). If your work
will exceed ~5 minutes of wall-clock time — Polygon-class data fetches,
full-history backtests, parameter sweeps, ML training, anything against a
rate-limited external API — you MUST dispatch it via the
`pipeline_task_start(name, command)` MCP tool instead of running it inline
via Bash.

How it works:
- The tool spawns `command` as a detached subprocess (via shell), returns
  immediately with the pid + log path. Your turn is NOT blocked on the
  subprocess; you finish your turn right after dispatching.
- After your `report_result(status="complete", ...)`, the daemon moves THIS
  task to a `Pipeline Pending` board section (it doesn't go to Complete
  yet). On every subsequent tick, the daemon checks whether all of your
  pipelines have died. Once they have, the daemon re-runs your briefing's
  `## Verification` against the post-pipeline working tree. Pass → Complete.
  Fail → Backlog (retry), or Frozen on retry exhaustion.
- You can call `pipeline_task_start` multiple times in one turn to spawn
  parallel pipelines (use distinct `name` values per call). The daemon
  waits for ALL of them to die before verifying.

Examples that should pivot to pipeline mode:
- `python -m stoch fetch --tickers SPY,AAPL,MSFT,...` (rate-limited,
  multi-hour).
- `uv run python sweep.py --params 256 --cores 8` (parameter sweep over
  many runs).
- `python train.py --epochs 100` (model training).

Examples that stay inline:
- Running tests (`uv run pytest -q`).
- Editing source files + committing.
- Reading existing data and emitting a one-shot report.

When you DO pivot to pipeline mode, your `report_result` should briefly
state what you dispatched, e.g.:
    report_result(
      status="complete",
      summary="Dispatched pipeline 'spy-cache-prep' (pid 12345). Daemon will verify against the briefing's `## Verification` once the fetch completes."
    )

Do NOT also commit empty source changes just to satisfy the daemon — the
daemon only re-runs verification (no extra commit needed). Do NOT ALSO
attempt the work inline. The pipeline alone is the work.

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
- `.cc-autopilot/operator_queue.jsonl` — operator-staged board ops (TB-131); the CLI / MM-handler write path appends to it and the daemon drains.
- `.cc-autopilot/operator_queue_state.json` — applied-uuid bookkeeping for the operator queue; the daemon owns it.

(TB-143: `events.jsonl` and `operator_queue.jsonl` are listed
above for defense in depth — the SDK still rejects `Edit`/`Write`
on them — but the daemon's post-hoc snapshot check (TB-110) is
exempt for both, because the daemon / operator legitimately appends
to them during in-flight task runs and a hash diff would
false-positive. Don't take that exemption as license to mutate
either file — both are explicitly listed as off-limits and the
prompt-level fence still applies.)

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

### Proposing recurring work (optional) — `cron_propose`
If, while working on this task, you noticed that some operation should fire
on a schedule (e.g. a weekly perf snapshot, an hourly health check), call
the `mcp__autopilot__cron_propose` MCP tool — once per proposal. It is its
own tool, NOT an argument of `report_result`. It does NOT mutate
`cron.yaml` directly; the proposal is queued for operator review.

    cron_propose(
      name="weekly-perf-snapshot",     # short stable identifier
      schedule="1d",                   # interval like "1h" / "1d" / "30m"
      prompt="Run the perf suite ...", # the prompt body the cron will use
      rationale="Catches drift early — operator wanted weekly visibility.",
    )

You may call `cron_propose` multiple times in one task — each proposal is
independent and gets its own `cron_proposed` event with its own rationale.
Skip the call entirely if your task didn't surface a scheduling need; this
is a side-channel, not part of the result contract.

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


def pick_mm_toolset(cfg: Config) -> list[str]:
    """Return MM_HANDLER_TOOLS_FULL (idle) or MM_HANDLER_TOOLS_RESTRICTED (task in flight).

    TB-122: while a task agent is running (any Active task on the board), the
    Mattermost handler gets a narrower toolset — drops `cron_edit` and
    `ideation_state_write` to avoid cron-schedule mutations and ideation-state
    conflicts while another agent owns the working tree. Operator-facing tools
    (board_edit, daemon_control, mattermost_reply, operator_log_append) are
    kept regardless — the whole point of concurrent MM polling is that the
    operator can pause, queue, approve, or ack mid-task.
    """
    if not cfg.tasks_file.exists():
        return MM_HANDLER_TOOLS_FULL
    board = Board.load(cfg.tasks_file)
    if list(board.iter_tasks("Active")):
        return MM_HANDLER_TOOLS_RESTRICTED
    return MM_HANDLER_TOOLS_FULL


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


def build_mattermost_prompt(
    cfg: Config,
    msg: dict,
    *,
    task_in_flight: bool = False,
) -> str:
    """Prompt for a mattermost handler agent.

    `msg` is a dict like:
        {"id": "...", "channel_id": "...", "channel_name": "dev",
         "user": "sarah", "text": "start the pipeline", "thread_id": "..."}

    `task_in_flight` (TB-122): set True when a task agent is running
    concurrently. The prompt explains that `cron_edit`,
    `ideation_state_write`, and (TB-142) `board_edit` are off-limits for
    this turn (the daemon's `_main_tick_loop` will pick those up once the
    board is idle, and queued board ops drain between tick stages so they
    never overlap with the running task's snapshot window), and the "your
    job" rubric is rewritten to route mutations through
    `operator_queue_append` instead of `board_edit`. The handler still has
    `operator_queue_append` (covers add / move_to_backlog / unfreeze /
    delete / approve — TB-142), `daemon_control`, `mattermost_reply`,
    `log_event`, and `operator_log_append` — enough to pause, queue
    add/delete/approve mutations, ack operator decisions, and reply.
    """
    channel_id = msg.get("channel_id", "")
    channel_name = msg.get("channel_name") or channel_id or "?"
    user = msg.get("user") or msg.get("user_id", "?")
    text = msg.get("text", "")
    thread = msg.get("thread_id") or msg.get("root_id") or ""
    parts: list[str] = [_CONTROL_HEADER]

    if task_in_flight:
        # Pinned phrasing — `tests/test_prompts.py` asserts these phrases
        # stay in the prompt so the restriction signal can't silently drift.
        parts.append(
            "\n## Note: restricted toolset (task currently in flight)\n"
            "A task agent is currently running (the daemon's main loop and "
            "this Mattermost handler are concurrent — TB-122). For this turn "
            "your toolset is restricted: `cron_edit`, `ideation_state_write`, "
            "and `board_edit` are off-limits because they would race the "
            "running task's view of the schedule / ideation state / TASKS.md "
            "snapshot (TB-142 — direct `board_edit` mutations during the "
            "in-flight window trip TB-110's state-violation check and roll "
            "back the running task). They'll be available again once the "
            "daemon is idle.\n"
            "\n"
            "Use `operator_queue_append` for ALL board mutations this turn — "
            "it's the queue-routed equivalent of `board_edit`. The daemon "
            "drains queued ops between tick stages, so the running task's "
            "snapshot window never sees the mutation. Supported ops: "
            "`add_ready` / `add_backlog` / `add_frozen` / `move_to_backlog` "
            "/ `unfreeze` / `delete` / `approve` (TB-142 — strips "
            "`@blocked:review` from an ideation-proposed task so it "
            "dispatches). For `add_*` ops the TB-N is allocated synchronously "
            "and you can mention it in your reply.\n"
            "\n"
            "Still available directly: `daemon_control` (pause / resume — "
            "pause takes effect on the **next** tick; the running task "
            "continues to completion, then no further dispatch), "
            "`operator_log_append` (the operator's veto channel — "
            "\"@claude-bot ack: ...\" style messages), `mattermost_reply`, "
            "`log_event`, plus all reads.\n"
            "\n"
            "If the user asked for cron / ideation-state changes or for a "
            "board op the queue doesn't cover (e.g. `move_to_frozen` / "
            "`move_to_complete`), reply via `mattermost_reply` explaining "
            "the restriction and that the request will be handled once the "
            "daemon is idle. Do not try to call the disabled tools — the "
            "SDK will reject them and the rejection lands in events.jsonl "
            "as noise."
        )

    parts.extend([
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
    ])
    if task_in_flight:
        parts.extend([
            "- If the user asks to add / approve / delete / backlog / unfreeze a task: use `operator_queue_append` (NOT `board_edit` — it's disabled this turn, TB-142). The daemon drains the queue between tick stages, so your op lands at the next tick boundary without racing the running task's snapshot window. **Approving an ideation-proposed task** (TB-121) is `op=\"approve\"` with `task_id=TB-N` — the drain-side handler strips the `@blocked:review` codespan (and any legacy `(blocked on: review)` description prose) so the task dispatches at the next tick.",
            "- If the user asks for ops the queue doesn't cover (e.g. `freeze` → `move_to_frozen`, `move_to_complete`): reply via `mattermost_reply` explaining they'll be handled once the daemon is idle.",
            "- If the user asks to pause/resume the daemon: use `daemon_control`.",
            "- If the user is acknowledging a decision (\"ack: …\" / \"done: …\" / \"decided: …\"): use `operator_log_append`.",
            "- If the user asks for cron / ideation-state changes: do NOT call cron_edit / ideation_state_write — reply via `mattermost_reply` explaining they'll be handled once the daemon is idle.",
            # TB-144: status-report queries route through the shared MCP
            # tool so chat-triggered and cron-triggered reports stay in
            # sync (one prompt, one freshness contract, one event audit
            # trail). Pinned phrasing — `tests/test_prompts.py` asserts
            # the recognition pattern + the tool name.
            "- If the user asks for a **status report** (recognize: \"status\", \"status?\", \"what's going on\", \"how are things\", \"how's the daemon\", \"any updates\"): call `status_report_run({\"reason\": \"<short paraphrase of what they asked>\"})` instead of composing your own reply. The routine handles posting (or skipping if nothing has changed) and emits the audit events. Don't call it more than once per turn — the skip-gate fires fast, but the SDK turn isn't free. Your `mattermost_reply` after the call can mention the result (\"posted to #ap2\" / \"skipped: nothing has changed since the last report\").",
            "- If the user asks a question: read what's needed and answer via `mattermost_reply`.",
            "- Log anything noteworthy via `log_event`.",
        ])
    else:
        parts.extend([
            "- If the user asks for work: add tasks via `board_edit`. **Approving an ideation-proposed task** (TB-121) is action `approve` on a Backlog task — strips the `@blocked:review` codespan (and any legacy `(blocked on: review)` description prose) so it dispatches.",
            "- If the user asks for monitoring: add a job via `cron_edit`.",
            # TB-144: status-report queries route through the shared MCP
            # tool. Same shape in both restricted and full toolsets — the
            # MM handler doesn't reinvent the report format.
            "- If the user asks for a **status report** (recognize: \"status\", \"status?\", \"what's going on\", \"how are things\", \"how's the daemon\", \"any updates\"): call `status_report_run({\"reason\": \"<short paraphrase of what they asked>\"})` instead of composing your own reply. The routine handles posting (or skipping if nothing has changed) and emits the audit events. Don't call it more than once per turn.",
            "- If the user asks a question: read what's needed and answer via `mattermost_reply`.",
            "- Log anything noteworthy via `log_event`.",
        ])

    parts.extend([
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
    ])
    return "\n".join(parts)


def build_control_prompt(cfg: Config, job_name: str, job_prompt: str) -> str:
    """Build the prompt for a control-agent run (cron job or ideation cycle).

    Used by `daemon.run_cron` (status-report and any future cron jobs) and
    `ideation._maybe_ideate`. The `## Control job` framing replaces the
    old `## Scheduled job` framing — ideation isn't on a schedule, and
    "control job" matches the broader CONTROL_AGENT_TOOLS partition.

    TB-128: a `## Current state` block (now/board/recent commits) is
    injected above the job prompt so the agent has a deterministic
    "right now" snapshot. The status-report job additionally gets an
    explicit timestamp / freshness contract appended.
    """
    parts = [
        _CONTROL_HEADER,
        "",
        _current_state_block(cfg),
        f"\n## Control job: {job_name}",
        "",
        job_prompt,
        "",
        "## Guidance",
        "- Take only actions necessary to fulfill the job description.",
        "- Log a summary via `log_event` before you finish.",
        "- Do not loop; one pass per invocation.",
        "",
    ]
    if job_name == "status-report":
        parts.append(_STATUS_REPORT_CONTRACT)
        parts.append("")
    parts.append(_events_block(cfg))
    return "\n".join(parts)

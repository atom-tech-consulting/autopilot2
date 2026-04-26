"""Autopilot v2 daemon — the main loop.

This is a Python scheduler (not a Claude session). Each tick it:
  1. Pulls new mattermost messages → spawns a handler agent per message.
  2. Runs any due cron jobs.
  3. Picks the next Ready task off the board and runs it.

Each unit of work is a fresh SDK `query()` call, so contexts never accumulate.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import diagnose, events, prompts, retry, tools, verify
from .board import Board
from .config import Config
from .cron import (
    CronJob,
    bootstrap as bootstrap_cron,
    due_jobs,
    load_jobs,
    load_state,
    mark_run,
)
from .mattermost import check_new_messages
from .result import TaskResult, parse as parse_result
from .tools import (
    CONTROL_AGENT_TOOLS,
    TASK_AGENT_TOOLS,
    build_mcp_server,
    do_board_edit,
    do_cron_edit,
)


RUNNING = True

# Module-level dedup so we don't re-emit board_malformed_line every tick for
# the same offending line. Cleared on daemon restart.
_SEEN_MALFORMED: set[str] = set()


def _handle_signal(signum, frame):  # noqa: ARG001
    global RUNNING
    RUNNING = False


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


async def run_task(cfg: Config, sdk, task) -> None:
    """Execute a single Ready task in an isolated SDK query()."""
    prompt = prompts.build_task_prompt(cfg, task)
    events.append(cfg.events_file, "task_start", task=task.id, title=task.title)
    do_board_edit(cfg, {"action": "move_to_active", "task_id": task.id})

    # Pre-flight debug dump: if the SDK subprocess crashes with an empty
    # stderr (observed on stoch's TB-58/TB-59), these files are the only way
    # to reproduce the failure. Dumped BEFORE the query starts so even a
    # SIGKILL-before-write leaves us the prompt.
    prompt_dump, stream_dump = _prep_debug_dumps(cfg, task.id)
    prompt_dump.write_text(prompt)

    # Ring buffer for SDK subprocess stderr — without this the SDK raises
    # ProcessError with a useless "Check stderr output for details" message.
    stderr_lines: list[str] = []

    def _stderr_sink(line: str) -> None:
        stderr_lines.append(line)
        if len(stderr_lines) > 200:
            del stderr_lines[: len(stderr_lines) - 200]

    # Ring buffer of stream-message descriptors so we can attach the last few
    # messages to the error event; dumps to disk for full history.
    stream_log: list[dict] = []

    def _log_message(msg) -> None:
        entry = {
            "type": type(msg).__name__,
            "text_preview": _extract_text(msg)[:500] or None,
        }
        stream_log.append(entry)
        if len(stream_log) > 200:
            del stream_log[: len(stream_log) - 200]
        with stream_dump.open("a") as f:
            import json as _json
            f.write(_json.dumps(entry) + "\n")

    async def _consume() -> str:
        text = ""
        async for msg in sdk.query(
            prompt=prompt,
            options=sdk.ClaudeAgentOptions(
                cwd=str(cfg.project_root),
                allowed_tools=TASK_AGENT_TOOLS,
                disallowed_tools=["Bash(git push*)", "Bash(rm -rf *)"],
                permission_mode="bypassPermissions",
                max_turns=int(os.environ.get("AP2_TASK_MAX_TURNS", 50)),
                setting_sources=["project"],
                stderr=_stderr_sink,
            ),
        ):
            _log_message(msg)
            t = _extract_text(msg)
            if t:
                text = t
        return text

    try:
        result_text = await asyncio.wait_for(_consume(), timeout=cfg.task_timeout_s)
    except asyncio.TimeoutError:
        # Even on timeout, the agent may have committed before stalling — check
        # HEAD before declaring failure. Same recovery path as the post-run
        # status=unknown fallback below.
        inferred = _infer_result_from_head(cfg, task)
        if inferred is not None:
            events.append(
                cfg.events_file, "task_implicit_commit",
                task=task.id, commit=inferred.commit,
                subject=inferred.summary, reason="timeout_recovered",
            )
            result_text = ""  # bypass parse_result; the fallback below uses inferred
            parsed_override = inferred  # type: ignore[assignment]
        else:
            events.append(
                cfg.events_file,
                "task_timeout",
                task=task.id,
                timeout_s=cfg.task_timeout_s,
                stderr_tail="\n".join(stderr_lines[-30:]),
                last_messages=stream_log[-10:],
                prompt_dump=str(prompt_dump),
                stream_dump=str(stream_dump),
            )
            _handle_failure(cfg, task, status="timeout")
            return
    except Exception as e:  # noqa: BLE001
        # Same logic for an opaque SDK subprocess crash (the "exit code 1 /
        # empty stderr_tail" pattern observed on stoch's TB-58/59). If the
        # agent reached the commit turn before the crash, HEAD will name the
        # task and we can salvage the run.
        inferred = _infer_result_from_head(cfg, task)
        if inferred is not None:
            events.append(
                cfg.events_file, "task_implicit_commit",
                task=task.id, commit=inferred.commit,
                subject=inferred.summary, reason="error_recovered",
            )
            result_text = ""
            parsed_override = inferred  # type: ignore[assignment]
        else:
            events.append(
                cfg.events_file,
                "task_error",
                task=task.id,
                error=f"{type(e).__name__}: {e}",
                stderr_tail="\n".join(stderr_lines[-30:]),
                last_messages=stream_log[-10:],
                prompt_dump=str(prompt_dump),
                stream_dump=str(stream_dump),
            )
            _handle_failure(cfg, task, status="error")
            return

    # If we recovered from a crash/timeout via HEAD, parsed_override is set;
    # otherwise parse the agent's RESULT block as usual and try the fallback
    # for the status=unknown case.
    parsed = parsed_override if 'parsed_override' in locals() else parse_result(result_text)
    if parsed.status not in _VALID_RESULT_STATUSES:
        inferred = _infer_result_from_head(cfg, task)
        if inferred is not None:
            events.append(
                cfg.events_file,
                "task_implicit_commit",
                task=task.id,
                commit=inferred.commit,
                subject=inferred.summary,
                reason="status_unknown",
            )
            parsed = inferred
    commit_hash = parsed.commit
    final_status = parsed.status
    if parsed.status == "complete":
        # Project-wide regression gate: when AP2_VERIFY_CMD is set, run it
        # against HEAD (post-agent-commit) before declaring Complete. Failure
        # routes through `_handle_failure` like any other task failure
        # (Backlog → retry → Frozen on exhaustion). Skipped when the env var
        # is empty, when the task carries `#no-verify`, or when an explicit
        # crash-recovery / timeout-recovery path produced this result (the
        # agent's HEAD already represents the recovered state and we don't
        # want a verify failure to mask a successful recovery — see
        # _infer_result_from_head).
        verify_res = _run_verify(cfg, task)
        if verify_res is not None and not verify_res.passed:
            events.append(
                cfg.events_file,
                "verification_failed",
                task=task.id,
                command=verify_res.command,
                exit_code=verify_res.exit_code,
                stderr_tail=verify_res.stderr_tail,
                duration_s=round(verify_res.duration_s, 2),
            )
            _handle_failure(cfg, task, status="verification_failed")
            final_status = "verification_failed"
        else:
            # Per-task verification (TB-69): run the briefing's `## Verification`
            # bullets after the project-wide gate (TB-66) but before
            # move_to_complete. Skip when no briefing or no section.
            per_verdict = await _maybe_per_task_verify(cfg, sdk, task)
            if per_verdict is not None and per_verdict.overall == "fail":
                events.append(
                    cfg.events_file,
                    "verification_failed",
                    task=task.id,
                    kind="per_task",
                    overall=per_verdict.overall,
                    criteria=[
                        {"kind": c.kind, "status": c.status,
                         "bullet": c.bullet[:200], "notes": c.notes[:200]}
                        for c in per_verdict.criteria
                    ],
                    duration_s=round(per_verdict.duration_s, 2),
                )
                _handle_failure(cfg, task, status="verification_failed")
                final_status = "verification_failed"
            else:
                if per_verdict is not None and per_verdict.overall == "partial":
                    events.append(
                        cfg.events_file,
                        "verification_partial",
                        task=task.id,
                        criteria=[
                            {"kind": c.kind, "status": c.status,
                             "bullet": c.bullet[:200], "notes": c.notes[:200]}
                            for c in per_verdict.criteria
                        ],
                    )
                do_board_edit(cfg, {"action": "move_to_complete", "task_id": task.id})
                retry.reset_attempt(cfg.retry_state_file, task.id)
                _append_progress(cfg, task, parsed)
                _dispatch_cron_directives(cfg, task.id, parsed.cron)
                # Delete the per-run debug dumps on success — only keep evidence for
                # failures (parsed.status in {incomplete, blocked, failed}) or crashes.
                for p in (prompt_dump, stream_dump):
                    try:
                        p.unlink()
                    except FileNotFoundError:
                        pass
    else:
        _handle_failure(cfg, task, status=parsed.status, parsed=parsed)
    events.append(
        cfg.events_file,
        "task_complete",
        task=task.id,
        status=final_status,
        commit=commit_hash,
        summary=parsed.summary[:300],
    )
    # Commit state-file updates (TASKS.md, progress.md, CLAUDE.md) right after
    # the task agent's own source-code commit. Reflects the post-task board
    # location so reverts/bisects stay semantic.
    board_after = Board.load(cfg.tasks_file)
    loc = board_after.find(task.id)
    dest = loc[0] if loc else "?"
    _commit_state_files(cfg, f"state: {task.id} → {dest}")


def _handle_failure(
    cfg: Config,
    task,
    *,
    status: str,
    parsed: TaskResult | None = None,
) -> None:
    """Move a failed task to Backlog, or Frozen if it has exhausted retries."""
    attempts = retry.bump_attempt(cfg.retry_state_file, task.id)
    if attempts >= cfg.max_retries:
        do_board_edit(cfg, {"action": "move_to_frozen", "task_id": task.id})
        events.append(
            cfg.events_file,
            "retry_exhausted",
            task=task.id,
            attempts=attempts,
            last_status=status,
        )
    else:
        do_board_edit(cfg, {"action": "move_to_backlog", "task_id": task.id})
    if parsed is not None:
        _append_attempts(cfg, task, parsed)


def _dispatch_cron_directives(cfg: Config, task_id: str, directives: list[dict]) -> None:
    """Apply any `cron:` directives from a successful RESULT via do_cron_edit."""
    for d in directives:
        if "_error" in d:
            events.append(
                cfg.events_file,
                "cron_proposal_rejected",
                task=task_id,
                reason=d.get("_error"),
                raw=d.get("_raw", "")[:200],
            )
            continue
        res = do_cron_edit(cfg, d)
        if res.get("isError"):
            events.append(
                cfg.events_file,
                "cron_proposal_error",
                task=task_id,
                action=d.get("action"),
                name=d.get("name"),
                error=res["content"][0]["text"][:300],
            )
        else:
            events.append(
                cfg.events_file,
                "cron_proposed",
                task=task_id,
                action=d.get("action"),
                name=d.get("name"),
            )


def _recover_orphans(cfg: Config) -> None:
    """Move any task left in Active back to Ready (crashed mid-run)."""
    if not cfg.tasks_file.exists():
        return
    board = Board.load(cfg.tasks_file)
    orphans = [t.id for t in board.iter_tasks("Active")]
    for tid in orphans:
        do_board_edit(cfg, {"action": "move_to_ready", "task_id": tid})
        events.append(cfg.events_file, "orphan_recovery", task=tid)
    if orphans:
        _commit_state_files(cfg, f"state: recovered {len(orphans)} orphan(s): {', '.join(orphans)}")


async def handle_message(cfg: Config, sdk, mcp_server, msg: dict) -> None:
    prompt = prompts.build_mattermost_prompt(cfg, msg)
    events.append(
        cfg.events_file,
        "mattermost",
        channel=msg.get("channel_name"),
        user=msg.get("user"),
        thread_id=msg.get("thread_id"),
        summary=(msg.get("text") or "")[:300],
    )

    async def _consume() -> None:
        async for _ in sdk.query(
            prompt=prompt,
            options=sdk.ClaudeAgentOptions(
                cwd=str(cfg.project_root),
                mcp_servers={"autopilot": mcp_server},
                allowed_tools=CONTROL_AGENT_TOOLS,
                permission_mode="bypassPermissions",
                max_turns=int(os.environ.get("AP2_CONTROL_MAX_TURNS", 15)),
                setting_sources=["project"],
            ),
        ):
            pass

    try:
        await asyncio.wait_for(_consume(), timeout=cfg.control_timeout_s)
    except asyncio.TimeoutError:
        events.append(
            cfg.events_file,
            "mattermost_timeout",
            timeout_s=cfg.control_timeout_s,
            thread_id=msg.get("thread_id"),
        )
    except Exception as e:  # noqa: BLE001
        events.append(
            cfg.events_file,
            "mattermost_error",
            error=f"{type(e).__name__}: {e}",
        )


async def run_cron(cfg: Config, sdk, mcp_server, job: CronJob) -> None:
    prompt = prompts.build_cron_prompt(cfg, job.name, job.prompt)
    events.append(cfg.events_file, "cron_start", job=job.name)
    allowed = job.allowed_tools or CONTROL_AGENT_TOOLS

    async def _consume() -> None:
        async for _ in sdk.query(
            prompt=prompt,
            options=sdk.ClaudeAgentOptions(
                cwd=str(cfg.project_root),
                mcp_servers={"autopilot": mcp_server},
                allowed_tools=allowed,
                permission_mode="bypassPermissions",
                max_turns=job.max_turns,
                setting_sources=["project"],
            ),
        ):
            pass

    try:
        await asyncio.wait_for(_consume(), timeout=cfg.control_timeout_s)
    except asyncio.TimeoutError:
        events.append(
            cfg.events_file,
            "cron_timeout",
            job=job.name,
            timeout_s=cfg.control_timeout_s,
        )
    except Exception as e:  # noqa: BLE001
        events.append(
            cfg.events_file,
            "cron_error",
            job=job.name,
            error=f"{type(e).__name__}: {e}",
        )
    finally:
        mark_run(cfg.cron_state_file, job.name)
        events.append(cfg.events_file, "cron_complete", job=job.name)
        # No-op for crons that didn't touch the board (e.g. status-report).
        # Ideation proposals add Backlog tasks via board_edit, so commit those.
        _commit_state_files(cfg, f"state: cron {job.name}")


def _extract_text(msg) -> str:
    """Best-effort extraction of an assistant message's final text block."""
    content = getattr(msg, "content", None)
    if isinstance(content, list):
        for part in reversed(content):
            text = getattr(part, "text", None)
            if isinstance(text, str) and text.strip():
                return text
    result = getattr(msg, "result", None)
    if isinstance(result, str):
        return result
    return ""


def _append_progress(cfg: Config, task, r: TaskResult) -> None:
    """Append a complete task entry to progress.md as a self-contained section.

    Section header is `## [YYYY-MM-DD] TB-N: Title` so the log stays a coherent
    reverse-time-ordered log and tools can parse sections cleanly.
    """
    cfg.progress_file.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"\n## [{_today()}] {task.id}: {task.title}"]
    if r.commit:
        lines.append(f"- **Commit:** `{r.commit[:8]}`")
    if r.summary:
        lines.append(f"- **Summary:** {r.summary}")
    if r.files_changed:
        lines.append(f"- **Files:** {', '.join(r.files_changed)}")
    if r.tests_passed is not None:
        lines.append(f"- **Tests:** {'pass' if r.tests_passed else 'fail'}")
    with cfg.progress_file.open("a") as f:
        f.write("\n".join(lines) + "\n")


def _append_attempts(cfg: Config, task, r: TaskResult) -> None:
    if not task.briefing:
        return
    p = Path(task.briefing)
    full = p if p.is_absolute() else cfg.project_root / p
    if not full.exists():
        return
    text = full.read_text()
    header = "\n## Attempts\n"
    entry = (
        f"\n### {_today()} — {r.status}\n"
        f"{r.summary or '(no summary)'}\n"
    )
    if header in text:
        full.write_text(text.rstrip() + entry)
    else:
        full.write_text(text.rstrip() + header + entry)


def _today() -> str:
    import datetime as dt

    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


# parse_result returns one of these for a well-formed RESULT block. Anything
# else (most often "unknown") triggers the commit-inference fallback below.
_VALID_RESULT_STATUSES = {"complete", "incomplete", "blocked", "failed"}


def _infer_result_from_head(cfg: Config, task) -> "TaskResult | None":
    """Synthesize a TaskResult from HEAD if its commit subject names `task.id`.

    Used as a fallback when the agent's RESULT block is malformed or absent.
    Returns None if not a git repo, log fails, or HEAD's subject doesn't
    mention the task ID — keeping the existing failure path untouched.
    """
    if not (cfg.project_root / ".git").exists():
        return None
    root = str(cfg.project_root)
    log = subprocess.run(
        ["git", "-C", root, "log", "-1", "--format=%H%x00%s"],
        capture_output=True, text=True,
    )
    if log.returncode != 0:
        return None
    out = log.stdout.strip()
    if "\x00" not in out:
        return None
    sha, subject = out.split("\x00", 1)
    if task.id not in subject:
        return None
    show = subprocess.run(
        ["git", "-C", root, "show", "--name-only", "--format=", sha],
        capture_output=True, text=True,
    )
    files = (
        [l for l in show.stdout.splitlines() if l.strip()]
        if show.returncode == 0 else None
    )
    return TaskResult(
        status="complete",
        commit=sha[:8],
        summary=subject,
        files_changed=files,
        tests_passed=None,
        cron=[],
        raw=f"<inferred from HEAD {sha[:8]}>",
    )


@dataclass
class VerifyResult:
    """Outcome of running the project-wide AP2_VERIFY_CMD against HEAD.

    Returned by `_run_verify` when the gate is configured. `exit_code=None`
    means the command exceeded `AP2_VERIFY_TIMEOUT_S`. stderr/stdout are
    tail-truncated to 2k chars to keep events.jsonl entries bounded.
    """

    passed: bool
    command: str
    exit_code: int | None
    stderr_tail: str
    stdout_tail: str
    duration_s: float


def _run_verify(cfg: Config, task) -> "VerifyResult | None":
    """Execute the project-wide regression gate, returning a result or None.

    Returns None (skip path) when:
      - AP2_VERIFY_CMD is unset or blank — the default; preserves pre-TB-66
        behavior so projects that haven't opted in see no change.
      - The task carries `#no-verify` — operator opt-out for tasks the gate
        can't meaningfully check (docs-only, infra changes the project's
        test command can't see, etc.).

    Otherwise runs `cfg.verify_cmd` via `shell=True` in `cfg.project_root`
    and returns a `VerifyResult`. Note `shell=True` is intentional: the
    command is operator-supplied configuration (not agent-supplied input),
    so shell parsing of forms like `uv run pytest -q` is the desired
    behavior, not an injection risk.
    """
    if not cfg.verify_cmd:
        return None
    if "#no-verify" in (task.tags or []):
        return None
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            cfg.verify_cmd,
            shell=True,
            cwd=str(cfg.project_root),
            capture_output=True,
            text=True,
            timeout=cfg.verify_timeout_s,
        )
    except subprocess.TimeoutExpired as e:
        # `e.stderr` and `e.stdout` may be bytes or str depending on Python
        # version + capture path. Normalize to str so callers don't care.
        def _to_str(x) -> str:
            if x is None:
                return ""
            if isinstance(x, bytes):
                return x.decode("utf-8", errors="replace")
            return x

        return VerifyResult(
            passed=False,
            command=cfg.verify_cmd,
            exit_code=None,
            stderr_tail=_to_str(e.stderr)[-2000:],
            stdout_tail=_to_str(e.stdout)[-2000:],
            duration_s=time.monotonic() - t0,
        )
    return VerifyResult(
        passed=proc.returncode == 0,
        command=cfg.verify_cmd,
        exit_code=proc.returncode,
        stderr_tail=proc.stderr[-2000:],
        stdout_tail=proc.stdout[-2000:],
        duration_s=time.monotonic() - t0,
    )


async def _maybe_per_task_verify(cfg: Config, sdk, task) -> "verify.VerifyVerdict | None":
    """Run the per-task verifier (TB-69) for `task` if its briefing has a
    `## Verification` section. Returns None to mean "skip" (legacy task or
    no concrete criteria) — the caller proceeds to move_to_complete unchanged.
    """
    if not task.briefing:
        return None
    p = Path(task.briefing)
    full = p if p.is_absolute() else cfg.project_root / p
    if not full.exists():
        return None
    text = full.read_text()
    return await verify.verify_task(
        briefing_text=text,
        project_root=cfg.project_root,
        timeout_s=cfg.verify_timeout_s,
        sdk=sdk,
    )


def _prep_debug_dumps(cfg: Config, task_id: str) -> tuple[Path, Path]:
    """Build paths for the per-run prompt + stream dumps.

    `.cc-autopilot/debug/` isn't tracked. Files named with UTC timestamp +
    task id so concurrent tasks (if ever allowed) don't clobber each other.
    Failures keep the files; `run_task` deletes them on successful complete.
    """
    import datetime as dt

    debug_dir = cfg.project_root / ".cc-autopilot" / "debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    prompt = debug_dir / f"{ts}-{task_id}.prompt.md"
    stream = debug_dir / f"{ts}-{task_id}.stream.jsonl"
    return prompt, stream


# Files the daemon is authoritative for. Committed together per semantic unit
# (a completed task, a cron ideation run, an orphan recovery) so the git log
# tracks board evolution alongside the task agents' source-code commits.
_STATE_FILE_NAMES = ("TASKS.md", ".cc-autopilot/progress.md", "CLAUDE.md")


def _commit_state_files(cfg: Config, message: str) -> None:
    """Stage + commit the daemon-owned state files if any are modified.

    Silently no-ops when nothing is staged (e.g. a status-report cron that
    didn't touch the board). Failures emit `state_commit_error` events but
    don't raise — a broken commit shouldn't wedge the daemon.
    """
    # Silent no-op when the project isn't a git repo — lets tests and non-git
    # experimentation use ap2 without every tick emitting a commit error.
    if not (cfg.project_root / ".git").exists():
        return
    rel_paths: list[str] = []
    for name in _STATE_FILE_NAMES:
        p = cfg.project_root / name
        if p.exists():
            rel_paths.append(name)
    if not rel_paths:
        return
    root = str(cfg.project_root)
    add = subprocess.run(
        ["git", "-C", root, "add", "--"] + rel_paths,
        capture_output=True, text=True,
    )
    if add.returncode != 0:
        events.append(cfg.events_file, "state_commit_error",
                      stage="add", message=message, error=add.stderr[:300])
        return
    diff = subprocess.run(
        ["git", "-C", root, "diff", "--cached", "--quiet", "--"] + rel_paths,
        capture_output=True,
    )
    if diff.returncode == 0:
        return  # nothing staged is actually different from HEAD
    commit = subprocess.run(
        ["git", "-C", root, "commit", "-m", message, "--"] + rel_paths,
        capture_output=True, text=True,
    )
    if commit.returncode != 0:
        events.append(cfg.events_file, "state_commit_error",
                      stage="commit", message=message, error=commit.stderr[:300])


async def main_loop(cfg: Config) -> None:
    cfg.ensure_dirs()
    if bootstrap_cron(cfg.cron_file):
        events.append(cfg.events_file, "cron_bootstrap", path=str(cfg.cron_file))
    _recover_orphans(cfg)
    _import_sdk_or_die()
    import claude_agent_sdk as sdk  # type: ignore

    mcp_server = build_mcp_server(cfg)
    events.append(cfg.events_file, "daemon_start", pid=os.getpid())
    cfg.pid_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.pid_file.write_text(str(os.getpid()))

    try:
        while RUNNING:
            if cfg.pause_flag.exists():
                await asyncio.sleep(cfg.tick_interval_s)
                continue
            await _tick(cfg, sdk, mcp_server)
            # Short sleep between ticks so we can shut down promptly.
            slept = 0
            while slept < cfg.tick_interval_s and RUNNING:
                await asyncio.sleep(1)
                slept += 1
    finally:
        events.append(cfg.events_file, "daemon_stop")
        try:
            cfg.pid_file.unlink()
        except OSError:
            pass


async def _tick(cfg: Config, sdk, mcp_server) -> None:
    # 1. Mattermost
    try:
        for msg in check_new_messages(cfg):
            await handle_message(cfg, sdk, mcp_server, msg)
    except Exception as e:  # noqa: BLE001
        events.append(cfg.events_file, "mm_poll_error", error=f"{type(e).__name__}: {e}")

    # 2. Cron
    try:
        jobs = load_jobs(cfg.cron_file)
        state = load_state(cfg.cron_state_file)
        for job in due_jobs(jobs, state, cfg.project_root):
            await run_cron(cfg, sdk, mcp_server, job)
    except Exception as e:  # noqa: BLE001
        events.append(cfg.events_file, "cron_error", error=f"{type(e).__name__}: {e}")

    # 3. Next Ready task (auto-promote top-of-Backlog if Ready is empty)
    try:
        board = Board.load(cfg.tasks_file)
        # Surface task-shaped lines that fail the parser. A common cause is a
        # manual edit inserting `(<sha>)` between **TB-N** and **Title**, which
        # makes the task invisible to `completed_ids()` and silently blocks
        # every Backlog task that depends on it.
        for section, line in board.malformed_lines:
            sig = f"{section}:{line}"
            if sig in _SEEN_MALFORMED:
                continue
            _SEEN_MALFORMED.add(sig)
            events.append(
                cfg.events_file,
                "board_malformed_line",
                section=section,
                line=line[:240],
            )
        task = board.next_ready()
        if task is None:
            # next_dispatchable skips any Backlog task with unmet `blocked on:`
            # references — backward-compatible: tasks with no declared blockers
            # are always dispatchable.
            backlog = board.next_dispatchable("Backlog")
            if backlog is not None:
                do_board_edit(cfg, {"action": "move_to_ready", "task_id": backlog.id})
                events.append(
                    cfg.events_file,
                    "backlog_auto_promoted",
                    task=backlog.id,
                    title=backlog.title,
                )
                board = Board.load(cfg.tasks_file)
                task = board.next_ready()
        if task:
            await run_task(cfg, sdk, task)
    except Exception as e:  # noqa: BLE001
        events.append(cfg.events_file, "task_error", error=f"{type(e).__name__}: {e}")

    # 4. Auto-ideation when the working board (Active+Ready+Backlog) is empty.
    # Throttled by AP2_EMPTY_BOARD_IDEATION_COOLDOWN_S (default 3600) to avoid
    # running the ideation agent on every 30s tick. Reuses the `ideation` cron
    # job's prompt/max_turns/allowed_tools; shares cron_state.json so a normal
    # scheduled ideation run ALSO satisfies the cooldown.
    try:
        await _maybe_auto_ideate(cfg, sdk, mcp_server)
    except Exception as e:  # noqa: BLE001
        events.append(cfg.events_file, "ideation_error", error=f"{type(e).__name__}: {e}")

    # 5. Idle watchdog (TB-71) — when nothing meaningful has happened for
    # AP2_AUTO_DIAGNOSE_IDLE_THRESHOLD_S (default 3h), build a self-diagnose
    # report and post it to AP2_MM_CHANNELS[0]. Throttled by
    # AP2_AUTO_DIAGNOSE_COOLDOWN_S (default 6h) to avoid spamming when idle
    # persists. Skips silently when MM env is unset (after one warning).
    try:
        _maybe_auto_diagnose(cfg)
    except Exception as e:  # noqa: BLE001
        events.append(cfg.events_file, "auto_diagnose_error",
                      error=f"{type(e).__name__}: {e}")


async def _maybe_auto_ideate(cfg: Config, sdk, mcp_server) -> None:
    board = Board.load(cfg.tasks_file)
    has_work = any(
        next(board.iter_tasks(section=s), None) is not None
        for s in ("Active", "Ready", "Backlog")
    )
    if has_work:
        return
    jobs = load_jobs(cfg.cron_file)
    ideation = next((j for j in jobs if j.name == "ideation"), None)
    if ideation is None:
        return
    cooldown_s = int(os.environ.get("AP2_EMPTY_BOARD_IDEATION_COOLDOWN_S", 3600))
    state = load_state(cfg.cron_state_file)
    last = state.get("ideation", 0.0)
    if time.time() - last < cooldown_s:
        return
    events.append(
        cfg.events_file,
        "ideation_empty_board",
        cooldown_s=cooldown_s,
        seconds_since_last=int(time.time() - last) if last else None,
    )
    await run_cron(cfg, sdk, mcp_server, ideation)


def _maybe_auto_diagnose(cfg: Config, *, now: float | None = None) -> None:
    """Idle-watchdog hook (TB-71). See `_tick` step 5 for context.

    Inspects events.jsonl for the most recent meaningful event. If the gap
    exceeds `cfg.auto_diagnose_idle_threshold_s` AND we haven't fired within
    `cfg.auto_diagnose_cooldown_s`, post `diagnose.render_markdown` to
    `AP2_MM_CHANNELS[0]` via `tools._mm_post`. Updates persistent state in
    `cfg.auto_diagnose_state_file`.

    `now` parameter exists so tests can drive a fake clock; production uses
    `time.time()`.
    """
    if now is None:
        now = time.time()

    state = _load_diagnose_state(cfg)
    report = diagnose.build_report(cfg, now=now)

    # No meaningful events yet (fresh daemon) → can't be idle. Skip.
    if report.since_last_activity_s is None:
        return

    if report.since_last_activity_s < cfg.auto_diagnose_idle_threshold_s:
        return

    if now - state.get("last_fired", 0.0) < cfg.auto_diagnose_cooldown_s:
        return

    channel = _first_mm_channel()
    if not channel:
        # Without a destination there's nowhere to post. Warn ONCE per run
        # of "AP2_MM_CHANNELS is unset"; the flag is sticky in state so we
        # don't fill events.jsonl with the same line every tick.
        if not state.get("warned_no_destination"):
            events.append(
                cfg.events_file,
                "auto_diagnose_no_destination",
                reason="AP2_MM_CHANNELS unset",
                idle_s=report.since_last_activity_s,
            )
            state["warned_no_destination"] = True
            _save_diagnose_state(cfg, state)
        return

    text = diagnose.render_markdown(report)
    try:
        post_id = tools._mm_post(channel, text)
    except Exception as e:  # noqa: BLE001
        events.append(
            cfg.events_file,
            "auto_diagnose_post_error",
            channel=channel,
            error=f"{type(e).__name__}: {e}",
        )
        return

    events.append(
        cfg.events_file,
        "auto_diagnose_fired",
        channel=channel,
        post_id=post_id,
        idle_s=report.since_last_activity_s,
        report_summary=text[:500],
    )
    state["last_fired"] = now
    state["warned_no_destination"] = False  # reset — destination is back
    _save_diagnose_state(cfg, state)


def _first_mm_channel() -> str:
    """Return the first channel id from `AP2_MM_CHANNELS`, or empty string.

    Mirrors `mattermost._channels_to_watch` parsing so the watchdog and the
    inbound poller agree on which env var defines "the project's channel(s)".
    """
    raw = os.environ.get("AP2_MM_CHANNELS", "").strip()
    for c in raw.split(","):
        c = c.strip()
        if c:
            return c
    return ""


def _load_diagnose_state(cfg: Config) -> dict:
    if not cfg.auto_diagnose_state_file.exists():
        return {}
    try:
        data = json.loads(cfg.auto_diagnose_state_file.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_diagnose_state(cfg: Config, state: dict) -> None:
    cfg.auto_diagnose_state_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.auto_diagnose_state_file.write_text(json.dumps(state, indent=2, sort_keys=True))


def _import_sdk_or_die() -> None:
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        print(
            "Error: claude-agent-sdk not installed. "
            "Install with: uv pip install claude-agent-sdk",
            file=sys.stderr,
        )
        sys.exit(1)


def run(project_root: str | None = None) -> None:
    cfg = Config.load(project_root)
    asyncio.run(main_loop(cfg))


if __name__ == "__main__":
    run()

"""Linear git-history rollback — shared between TB-110 (post-hoc state-file
violation detection) and TB-111 (operator-facing `ap2 rollback`).

The cohesion guarantee comes from TB-112: every daemon mutation is a commit,
and `_STATE_FILE_NAMES` covers TASKS.md + progress.md + ideation_state.md +
cron.yaml + retry_state.json + operator_log.md plus the briefings/insights
dirs. Linear `git reset --hard <boundary>` therefore restores the entire
system state in one atomic op — no manual board surgery, no retry-counter
reset, no merge conflicts.

Public surface:
  - linear_rollback_to(cfg, boundary_sha)
  - snapshot_fenced_files(cfg) / detect_fenced_violations(cfg, pre_snapshot)
  - resolve_boundary_by_n(cfg, n)
  - resolve_boundary_by_task(cfg, task_id)
  - list_affected_commits(cfg, boundary_sha)
  - list_alive_pipelines_in_range(cfg, boundary_sha)

Callers are responsible for holding `locked_board()` and emitting the
appropriate event (task_state_violation for TB-110, task_rollback for TB-111).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

from .config import Config
from .tools import TASK_AGENT_FENCED_PATHS


def _parse_iso(s: str) -> dt.datetime | None:
    """Parse ISO-8601 'Z'-suffixed or numeric-offset strings to UTC datetimes."""
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        d = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(dt.timezone.utc)


# Fenced paths checked by TB-110 — TASK_AGENT_FENCED_PATHS minus events.jsonl
# (gitignored, append-only audit trail; different threat model).
FENCED_PATHS_FOR_VIOLATION_CHECK: tuple[str, ...] = tuple(
    p for p in TASK_AGENT_FENCED_PATHS if p != ".cc-autopilot/events.jsonl"
)


def _is_git_repo(cfg: Config) -> bool:
    return (cfg.project_root / ".git").exists()


def _git(cfg: Config, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cfg.project_root), *args],
        capture_output=True, text=True,
    )


def git_head(cfg: Config) -> str | None:
    """Resolve HEAD to a SHA, or None if not a git repo / git fails."""
    if not _is_git_repo(cfg):
        return None
    p = _git(cfg, "rev-parse", "HEAD")
    if p.returncode != 0:
        return None
    return p.stdout.strip() or None


def is_ancestor(cfg: Config, sha: str) -> bool:
    """True if `sha` is a strict-or-equal ancestor of HEAD."""
    if not _is_git_repo(cfg):
        return False
    p = _git(cfg, "merge-base", "--is-ancestor", sha, "HEAD")
    return p.returncode == 0


# ---------------------------------------------------------------------------
# TB-110: hash-based fenced-file violation detection
#
# Rationale: snapshot AFTER the daemon's own `move_to_active` write so that
# legitimate daemon writes don't show up as agent violations. Hashing also
# catches uncommitted working-tree dirtying that a `git diff` walk between
# pre_run_head and HEAD would miss.

def snapshot_fenced_files(cfg: Config) -> dict[str, str]:
    """Return {relpath: sha256-hex} for each fenced path that exists.

    Missing files are simply absent from the dict — comparing `pre` and
    `post` snapshots picks up creations, deletions, and modifications.
    """
    out: dict[str, str] = {}
    for rel in FENCED_PATHS_FOR_VIOLATION_CHECK:
        p = cfg.project_root / rel
        if not p.exists():
            continue
        try:
            out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
        except OSError:
            # Unreadable file — treat as a "changed" sentinel so the next
            # snapshot diff catches it without crashing.
            out[rel] = "unreadable"
    return out


def detect_fenced_violations(
    cfg: Config,
    pre_snapshot: dict[str, str],
) -> list[str]:
    """Compare `pre_snapshot` against the current state.

    Returns a sorted list of fenced paths whose hash differs (or which
    appeared / disappeared) since `pre_snapshot` was taken.
    """
    post = snapshot_fenced_files(cfg)
    keys = set(pre_snapshot) | set(post)
    return sorted(k for k in keys if pre_snapshot.get(k) != post.get(k))


# ---------------------------------------------------------------------------
# Linear rollback executor (shared by TB-110 + TB-111)

def linear_rollback_to(cfg: Config, boundary_sha: str) -> None:
    """`git reset --hard <boundary_sha>`. Caller holds the board lock and
    emits the appropriate event. Silent no-op when the project isn't a git
    repo — mirrors `_commit_state_files`'s policy.
    """
    if not _is_git_repo(cfg):
        return
    if not is_ancestor(cfg, boundary_sha):
        raise ValueError(
            f"linear_rollback_to: {boundary_sha!r} is not an ancestor of HEAD"
        )
    p = _git(cfg, "reset", "--hard", boundary_sha)
    if p.returncode != 0:
        raise RuntimeError(
            f"git reset --hard {boundary_sha} failed: {p.stderr.strip()}"
        )


# ---------------------------------------------------------------------------
# Boundary resolvers (TB-111)
#
# We walk `git log --first-parent --format=%H%x00%s` so a future merge in the
# project's history doesn't make the rollback walk through the side branch.

_TB_PREFIX_RE = re.compile(r"^(TB-\d+)\b")
_STATE_TB_RE = re.compile(r"^state:\s*(TB-\d+)\b")


def _walk_history(cfg: Config) -> list[tuple[str, str]]:
    """Return [(sha, subject), ...] from HEAD walking first-parent."""
    if not _is_git_repo(cfg):
        return []
    p = _git(cfg, "log", "--first-parent", "--format=%H%x00%s")
    if p.returncode != 0:
        return []
    out: list[tuple[str, str]] = []
    for line in p.stdout.splitlines():
        if "\x00" not in line:
            continue
        sha, subject = line.split("\x00", 1)
        out.append((sha, subject))
    return out


def _commit_task_id(subject: str) -> str | None:
    """Pull a TB-N out of either an agent commit (`TB-N: ...`) or a daemon
    state commit (`state: TB-N → ...`)."""
    m = _TB_PREFIX_RE.match(subject)
    if m:
        return m.group(1)
    m = _STATE_TB_RE.match(subject)
    if m:
        return m.group(1)
    return None


def resolve_boundary_by_n(cfg: Config, n: int) -> str | None:
    """Walk back from HEAD, find the boundary N task-completions back.

    Each "task-completion" is a group of consecutive commits sharing a TB-N
    id (typically agent commit + daemon's `state: TB-N → <section>` state
    commit). Walks newest-first; the boundary is the first commit that
    belongs to a different (older) task or to no task at all.

    Returns None if history has fewer than N task groups.
    """
    if n <= 0:
        return None
    history = _walk_history(cfg)
    if not history:
        return None

    groups = 0
    seen_last: str | None = None
    for sha, subject in history:
        m_state = _STATE_TB_RE.match(subject)
        m_task = _TB_PREFIX_RE.match(subject)
        if m_state:
            tid = m_state.group(1)
        elif m_task:
            tid = m_task.group(1)
        else:
            tid = None
        if tid is None:
            # Non-task commit (baseline, init, manual sync). If we already
            # consumed n groups, this is the boundary; else there isn't
            # enough history to honor the request.
            return sha if groups >= n else None
        if tid != seen_last:
            groups += 1
            seen_last = tid
            if groups == n + 1:
                return sha
    # End of history: no commit qualified as the boundary.
    return None


def resolve_boundary_by_task(cfg: Config, task_id: str) -> str | None:
    """Find the boundary just before the oldest commit (agent or state)
    in HEAD's recent history that mentions `task_id`. Linear rule: every
    commit between that point and HEAD is rolled back.

    Returns None if `task_id` isn't found in the first-parent walk.
    """
    history = _walk_history(cfg)
    if not history:
        return None
    oldest_index: int | None = None
    for i, (_sha, subject) in enumerate(history):
        if _commit_task_id(subject) == task_id:
            oldest_index = i  # keep walking; we want the last (oldest) match
    if oldest_index is None:
        return None
    if oldest_index + 1 >= len(history):
        return None
    return history[oldest_index + 1][0]


def list_affected_commits(
    cfg: Config,
    boundary_sha: str,
) -> list[tuple[str, str]]:
    """Return [(sha, subject), ...] for commits in the range
    `boundary_sha..HEAD` (newest first). Caller renders these in the
    confirmation print and embeds them in the `task_rollback` event.
    """
    if not _is_git_repo(cfg):
        return []
    p = _git(
        cfg, "log", "--first-parent", "--format=%H%x00%s",
        f"{boundary_sha}..HEAD",
    )
    if p.returncode != 0:
        return []
    out: list[tuple[str, str]] = []
    for line in p.stdout.splitlines():
        if "\x00" not in line:
            continue
        sha, subject = line.split("\x00", 1)
        out.append((sha, subject))
    return out


def affected_task_ids(commits: list[tuple[str, str]]) -> list[str]:
    """Distinct TB-N ids referenced by the given commits, preserving the
    order they first appear (newest-first since `commits` is from log)."""
    seen: list[str] = []
    for _sha, subject in commits:
        tid = _commit_task_id(subject)
        if tid and tid not in seen:
            seen.append(tid)
    return seen


# ---------------------------------------------------------------------------
# Pipeline-running warning (TB-111)
#
# We scan events.jsonl from the boundary commit's timestamp forward for
# `pipeline_start` events and check whether each pid is still alive. We
# don't auto-kill — the briefing is explicit that operator decides.

def list_alive_pipelines_in_range(
    cfg: Config,
    boundary_sha: str,
) -> list[dict]:
    """Return [{pid, name, started_ts, log}, ...] for `pipeline_start`
    events whose pid is still alive (`os.kill(pid, 0)` works) and whose
    `ts` is at or after `boundary_sha`'s commit timestamp.
    """
    if not _is_git_repo(cfg) or not cfg.events_file.exists():
        return []
    p = _git(cfg, "log", "-1", "--format=%cI", boundary_sha)
    if p.returncode != 0:
        return []
    boundary_dt = _parse_iso(p.stdout.strip())
    if boundary_dt is None:
        return []

    out: list[dict] = []
    seen_pids: set[int] = set()
    try:
        lines = cfg.events_file.read_text().splitlines()
    except OSError:
        return []
    for line in lines:
        line = line.strip()
        if not line or "pipeline_start" not in line:
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        if evt.get("type") != "pipeline_start":
            continue
        ts = evt.get("ts", "")
        # ap2's events.append emits a Zulu / Z-suffixed timestamp; git's
        # `%cI` emits a strict-ISO8601 with a numeric offset. Lexical
        # compare misorders them ('Z' > '+00:00'), so parse both.
        evt_dt = _parse_iso(ts) if ts else None
        if evt_dt is not None and evt_dt < boundary_dt:
            continue
        pid = evt.get("pid")
        if not isinstance(pid, int) or pid in seen_pids:
            continue
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            continue
        seen_pids.add(pid)
        out.append({
            "pid": pid,
            "name": evt.get("name", ""),
            "started_at": evt.get("started_at") or ts,
            "log": evt.get("log", ""),
        })
    return out

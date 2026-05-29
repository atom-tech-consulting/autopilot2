"""Daemon-owned state-file commit machinery (TB-126).

Lifted from `ap2/daemon.py` as part of TB-263's responsibility split. The
orchestrator still drives WHEN to commit (after each task run, after each
cron tick, on orphan recovery) — this module owns the WHAT and HOW:

  - `_STATE_FILE_NAMES` / `_STATE_DIRS`: the canonical daemon-owned state
    surface. Membership here means "rollback restores it, git commit
    bundles it." Adding a file means thinking about both axes — see the
    in-line comments at each tuple entry for the rationale.
  - `_commit_state_files`: stage + commit a narrow allowlist of paths
    that the current operation actually touched. Silently no-ops when
    the staged subset is clean; failures emit `state_commit_error` but
    never raise so a broken commit can't wedge the daemon.
  - `_filter_state_paths`: defense-in-depth filter so a caller threading
    the wrong path through doesn't silently drag unrelated code into a
    state commit.
  - `_snapshot_state_paths` / `_changed_state_paths`: pre/post-hash diff
    used by control-agent paths (cron, ideation) where the daemon
    doesn't know statically which subset the agent will touch.
  - `_task_state_paths`: the fixed superset a task-completion path can
    dirty; downstream `_filter_state_paths` + `git diff --cached
    --quiet` drop entries that don't apply.

Tests reach in via `daemon._STATE_FILE_NAMES` / `daemon._commit_state_files`;
those names are re-exported from `ap2/daemon.py` so the import contract
is unchanged.
"""
from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Iterable
from pathlib import Path

from . import events
from .config import Config


# Files the daemon is authoritative for. Committed together per semantic unit
# (a completed task, a cron ideation run, an orphan recovery) so the git log
# tracks board evolution alongside the task agents' source-code commits.
# `ideation_state.md` is the per-cycle progress assessment ideation
# overwrites at the start of every cron run (TB-87) — committing it with
# the rest of the state files keeps the assessment recoverable from git
# history for retrospectives.
_STATE_FILE_NAMES = (
    "TASKS.md",
    ".cc-autopilot/progress.md",
    "CLAUDE.md",
    ".cc-autopilot/ideation_state.md",
    # TB-112: bring three more under daemon auto-commit so the linear
    # rollback design (TB-111) gets cohesion for free.
    #   - cron.yaml: schedule config; mutated via the cron_edit MCP
    #     tool. Previously deferred (TB-83) as YAGNI; relevant now.
    #   - retry_state.json: per-task retry counter. Un-gitignored at
    #     the same time so commits succeed.
    #   - operator_log.md: operator decisions (TB-106). Was committed
    #     ad-hoc; now part of the canonical state-file set.
    # Files that stay gitignored — cron_state.json, mm_state.json,
    # auto_diagnose_state.json, events.jsonl — are ephemeral runtime
    # state. Rollback should NOT re-fire crons / replay MM / re-fire
    # watchdog / replay events; leaving them uncommitted gives that
    # property for free.
    ".cc-autopilot/cron.yaml",
    ".cc-autopilot/retry_state.json",
    ".cc-autopilot/operator_log.md",
    # TB-193: `goal.md` becomes daemon-mutable via the `update_goal`
    # operator-queue op so refreshing the project mission while the
    # daemon runs no longer requires `ap2 daemon-control --pause`. Once
    # mutable, rollback cohesion demands it be in the snapshot baseline
    # — otherwise an `ap2 rollback` past an `update_goal` commit would
    # leave goal.md at the new content while every other state file
    # reverts (the same failure mode TB-192 catches for `_index.md`).
    # Adding it here also means out-of-band edits during a pause get
    # auto-picked up by the next snapshot/diff cron commit (acceptable:
    # pause-edits are still rare post-TB-193 and the auto-commit
    # eliminates an entire class of "operator forgot to commit goal.md"
    # footgun).
    "goal.md",
    # TB-324 (axis 4): `.cc-autopilot/config.toml` becomes daemon-
    # mutable via the `config_set` operator-queue op. Same rollback-
    # cohesion rationale as `goal.md` above — once mutable, the file
    # must be in the snapshot baseline so an `ap2 rollback` past a
    # `config_set` commit reverts the structured-config state along
    # with everything else. Out-of-band edits during a pause also get
    # auto-picked up by the next snapshot/diff cron commit (same
    # acceptable trade-off as goal.md).
    ".cc-autopilot/config.toml",
)
# Directories whose contents are also daemon-owned audit trail. Staged with
# `git add <dir>` so new briefings (from `add_backlog` auto-fill, ideation
# proposals, or `/tb prep`) and accumulated `## Attempts` edits ride along
# with the state-file commit (TB-73). Briefings get linked from TASKS.md, so
# bundling them keeps reverts/bisects semantically intact.
#
# `.cc-autopilot/insights/` (TB-89) is daemon-owned audit trail too — the
# index file is auto-regenerated by ap2, individual insight files are
# written by tasks/operators. Including the dir keeps git history lined up
# with what ideation actually saw on each cycle.
_STATE_DIRS = (
    ".cc-autopilot/tasks",
    ".cc-autopilot/insights",
    # TB-188: per-proposal records (one JSON per ideation-authored
    # proposal, keyed on TB-N). Daemon-owned audit trail — written at
    # `add_backlog` time and reconciled with an `outcome` block on the
    # first terminal event (task_complete / operator approve / reject /
    # delete). Bundled into the state-dirs set so signal-collection
    # follow-ups (TB-189 delete-test verdict, acceptance-rate
    # aggregation, retrospective classifier) can query history across
    # cycles, and so an `ap2 rollback` past a state commit reverts the
    # records alongside the board / progress / cron state they were
    # paired with.
    ".cc-autopilot/ideation_proposals",
)


def _commit_state_files(
    cfg: Config,
    message: str,
    *,
    paths: Iterable[str],
) -> None:
    """Stage + commit a narrow allowlist of daemon-owned state files.

    `paths` is a caller-supplied list of repo-relative paths the current
    operation actually touched (TB-126). Only paths inside the daemon-owned
    state set (`_STATE_FILE_NAMES` ∪ `_STATE_DIRS`) AND that exist on disk
    are staged — anything else is dropped defensively. This keeps state
    commits semantically narrow: a `state: TB-N → Backlog` commit no longer
    rides along with an unrelated briefing that happened to be dirty in the
    working tree from a prior operation.

    Silently no-ops when the working tree is clean for the supplied paths
    (e.g. a status-report cron that didn't touch any state file). Failures
    emit `state_commit_error` events but don't raise — a broken commit
    shouldn't wedge the daemon.
    """
    # Silent no-op when the project isn't a git repo — lets tests and non-git
    # experimentation use ap2 without every tick emitting a commit error.
    if not (cfg.project_root / ".git").exists():
        return
    rel_paths = _filter_state_paths(cfg, paths)
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


def _filter_state_paths(cfg: Config, paths: Iterable[str]) -> list[str]:
    """Filter caller-supplied paths to existing files inside the state set.

    Defensive: a caller threading the wrong path through (e.g. a source file)
    silently no-ops rather than letting a state commit pull in unrelated
    code. Dedupes while preserving caller order so commit log reads
    naturally. Callers may pass POSIX-style or `Path`-style relative strings.
    """
    allowed_files = set(_STATE_FILE_NAMES)
    allowed_dirs = tuple(_STATE_DIRS)
    out: list[str] = []
    seen: set[str] = set()
    for p in paths:
        # Normalize: posix slashes, drop a `./` prefix if present. NOT a
        # `lstrip("./")` — that's a charset strip and would eat the leading
        # dot in `.cc-autopilot/...`.
        rel = str(p).replace("\\", "/")
        if rel.startswith("./"):
            rel = rel[2:]
        if rel in seen:
            continue
        # Path must be either an explicit state file or live inside a state dir.
        if rel not in allowed_files and not any(
            rel == d or rel.startswith(d.rstrip("/") + "/")
            for d in allowed_dirs
        ):
            continue
        full = cfg.project_root / rel
        if not full.exists():
            continue
        seen.add(rel)
        out.append(rel)
    return out


def _snapshot_state_paths(cfg: Config) -> dict[str, str]:
    """Hash every state-relevant path's working-tree content.

    Used by control-agent paths (cron tick, ideation) where we don't know
    statically which subset of the state surface the agent will touch.
    Caller compares pre/post snapshots via `_changed_state_paths` and
    threads the delta into `_commit_state_files`.

    Missing files are absent from the dict (so `_changed_state_paths` sees
    "appeared" / "disappeared" as a hash mismatch). Unreadable files map to
    a sentinel so a transient I/O error doesn't crash the snapshot.
    """
    out: dict[str, str] = {}

    def _hash(p: Path) -> str | None:
        if not p.is_file():
            return None
        try:
            return hashlib.sha1(p.read_bytes()).hexdigest()
        except OSError:
            return "unreadable"

    for name in _STATE_FILE_NAMES:
        h = _hash(cfg.project_root / name)
        if h is not None:
            out[name] = h
    for d in _STATE_DIRS:
        dpath = cfg.project_root / d
        if not dpath.is_dir():
            continue
        for f in dpath.rglob("*"):
            if not f.is_file():
                continue
            try:
                rel = str(f.relative_to(cfg.project_root))
            except ValueError:
                continue
            h = _hash(f)
            if h is not None:
                out[rel.replace("\\", "/")] = h
    return out


def _changed_state_paths(
    before: dict[str, str], after: dict[str, str]
) -> list[str]:
    """Return state-relevant paths whose hash differs between snapshots.

    Includes paths that appeared (new file) or disappeared (deletion). Sort
    keeps commit-log diffs deterministic.
    """
    keys = set(before) | set(after)
    return sorted(k for k in keys if before.get(k) != after.get(k))


def _task_state_paths(task) -> list[str]:
    """Repo-relative state paths a task-completion (or failure) operation
    can dirty. Used by `run_task` and the pipeline-pending sweep.

    - `TASKS.md`: every board move.
    - `progress.md`: `_append_progress` on Complete.
    - `retry_state.json`: `bump_attempt` / `reset_attempt`.
    - The task's briefing: `_append_attempts` on every failure mode.
    - The TB-188 proposal record (`.cc-autopilot/ideation_proposals/<TB-N>.json`):
      `reconcile_proposal_outcome` appends an `outcome` block on
      `task_complete` (status=complete or status=verification_failed).
      The path is included unconditionally — `_filter_state_paths`
      drops it for tasks without a record (legacy / non-ideation
      proposals), and `git diff --cached --quiet` drops it for
      no-op reconciliations.

    Files that exist but weren't actually modified are filtered downstream
    by `git diff --cached --quiet`, so passing a fixed superset is safe.
    """
    paths = [
        "TASKS.md",
        ".cc-autopilot/progress.md",
        ".cc-autopilot/retry_state.json",
        f".cc-autopilot/ideation_proposals/{task.id}.json",
    ]
    if task.briefing:
        paths.append(str(task.briefing).replace("\\", "/"))
    return paths

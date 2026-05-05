"""Janitor — deterministic detector for stranded git state in a project (TB-177).

Why this module exists
----------------------
The walk-away promise in `goal.md` ("an operator can point ap2 at a fresh
project, paste a goal.md, and walk away for a week without intervention")
breaks silently when working-tree detritus accumulates in the target repo.
Concrete shape: a pipeline script (e.g. `reeval_tb22.py`'s `_git_commit`)
swallows `git add`'s exit-1 from a tracked-in-gitignored-dir quirk; a
staged but uncommitted file then sits in the index for ~32 minutes until
the operator manually runs `git status`. The bug-class (any pipeline
script that mishandles its own commit, plus operator scratch left over a
context switch) repeats across projects.

The janitor runs deterministically (no LLM, no SDK call) on the cron
cadence the operator chose in `cron.yaml` and emits a `janitor_finding`
event per detection plus a single summary line in `operator_log.md` when
at least one finding fires. v1 is **report-first**: detection without
auto-remediation. Operators see findings on `ap2 status` and the next
cron status-report; they decide whether to commit, discard, or ignore.
A future `ap2 janitor apply` (separate TB) can offer guided remediation
once the safe-cases-vs-risky-cases distinction has had operator-eyes-on
for at least one shipping cycle.

What's NOT in v1 (explicit, so a future contributor doesn't get clever)
- Auto-commit / auto-stash / auto-discard. All destructive or surprising.
- Other detection kinds beyond `git_stranded_state` (dead-blocker,
  pipeline-pending-with-dead-pid, stale debug dumps, TASKS.md drift) —
  each gets its own TB once the framework's prove-out lands.
- LLM-driven interpretation. Deterministic Python is sufficient.
- Multi-project scans. One daemon per project; one janitor per daemon.
- Configurable per-finding age thresholds via env. Defaults baked in.

Detection scope (v1)
--------------------
One kind: `git_stranded_state`. Three subkinds:

  staged_uncommitted     → `git diff --cached --name-only` returns paths.
                           The TB-22 case. Fires regardless of age — staged
                           files are NEVER an intended steady state.
  modified_not_staged    → tracked files modified ≥ MIN_AGE_S ago and not
                           staged. Excludes daemon-managed paths (events.jsonl,
                           cron_state.json, etc.) that legitimately churn
                           between commits. Age guard prevents false
                           positives during an in-flight task agent run.
  untracked_non_ignored  → files in the working tree that are neither
                           tracked nor matched by .gitignore. Operator
                           scratch work or pipeline detritus.

Each finding is a `JanitorFinding(subkind, paths, age_s, hint)` record. The
`hint` is a one-line operator suggestion (e.g. "commit with the operator's
intent, or `git restore --staged` to unstage"). Findings are aggregated
into a `JanitorReport`; one event is emitted per finding (NOT per file —
keeps the events.jsonl tail readable). `operator_log.md` gains exactly
one summary line per run that found anything.

Excluded paths (working-tree-modified check)
--------------------------------------------
Daemon-managed state under `.cc-autopilot/` legitimately churns between
commits. Surfacing it as stranded would be noise. We exclude:

  events.jsonl, cron_state.json, mm_state.json, daemon.pid, paused,
  auto_diagnose_state.json, retry_state.json, operator_queue.jsonl,
  operator_queue_state.json, pipelines/*.log, debug/<run>.{prompt.md,
  stream.jsonl,messages.jsonl}, ideation_state.md (for the working-tree
  check — `_index.md` IS scanned because it's content the operator may
  want to commit).

Note: this exclusion is for the `modified_not_staged` and
`untracked_non_ignored` subkinds. The `staged_uncommitted` subkind
intentionally surfaces ANY staged file — if the daemon's own commit path
left something staged, that's a daemon bug worth surfacing.

Public surface
--------------
`run_janitor(cfg) -> JanitorReport` — the entry the daemon dispatches via
the cron-job table; also callable directly from tests. Pure function modulo
the `events.append` and `operator_log.md` append it does at the end.
"""
from __future__ import annotations

import datetime as dt
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import events
from .config import Config


# --------------------------------------------------------------------------
# Tunables
# --------------------------------------------------------------------------

# Age threshold (seconds) for the `modified_not_staged` subkind. Files
# modified within this window are skipped — they're plausibly an in-flight
# task agent's working tree mid-edit. 5 minutes ≈ 10 ticks at the default
# 30s tick interval, matching the briefing's "tick interval × 10" rule.
# Baked-in for v1; operator-tunable thresholds get a separate TB if they
# become a real ask.
MIN_MODIFIED_AGE_S = 5 * 60

# Maximum recency window for a `janitor_finding` to count as "current"
# when surfaced by `ap2 status` / status-report cron. Findings older than
# this are stale (the next janitor run will re-emit them if still
# relevant); freshness keeps the CLI line from growing day-old noise.
# Sized at 7h so a default 6h cadence (`0 */6 * * *`) always shows the
# most recent run's findings without overlap risk.
RECENT_FINDING_WINDOW_S = 7 * 3600


# --------------------------------------------------------------------------
# Excluded paths (working-tree modified / untracked checks)
# --------------------------------------------------------------------------

# Files under `.cc-autopilot/` that the daemon legitimately mutates between
# commits. Surfacing these as stranded would be noise. The list mirrors the
# briefing's enumeration verbatim so the contract is auditable from one
# place. Paths are repo-relative POSIX strings (matches `git status
# --porcelain` output).
_EXCLUDED_FILES = frozenset({
    ".cc-autopilot/events.jsonl",
    ".cc-autopilot/cron_state.json",
    ".cc-autopilot/mm_state.json",
    ".cc-autopilot/daemon.pid",
    ".cc-autopilot/paused",
    ".cc-autopilot/auto_diagnose_state.json",
    ".cc-autopilot/retry_state.json",
    ".cc-autopilot/operator_queue.jsonl",
    ".cc-autopilot/operator_queue_state.json",
    ".cc-autopilot/ideation_state.md",
})

# Directory prefixes whose contents are excluded from the working-tree
# checks. `.cc-autopilot/pipelines/*.log` is per-pipeline subprocess output
# and `.cc-autopilot/debug/*` are per-task debug dumps; both grow during
# normal operation and are not commit-worthy.
_EXCLUDED_PREFIXES = (
    ".cc-autopilot/pipelines/",
    ".cc-autopilot/debug/",
)


def _is_excluded(path: str) -> bool:
    """True iff `path` (repo-relative POSIX) is daemon-managed churn that
    should NOT surface as stranded.

    Used by the `modified_not_staged` and `untracked_non_ignored` subkinds.
    The `staged_uncommitted` subkind intentionally bypasses this — anything
    staged deserves surfacing regardless of who staged it.
    """
    if path in _EXCLUDED_FILES:
        return True
    for prefix in _EXCLUDED_PREFIXES:
        if path.startswith(prefix):
            return True
    return False


# --------------------------------------------------------------------------
# Data shapes
# --------------------------------------------------------------------------


@dataclass
class JanitorFinding:
    """A single deterministic finding from one janitor check.

    `subkind` distinguishes the shape (`staged_uncommitted`,
    `modified_not_staged`, `untracked_non_ignored`); `paths` carries the
    POSIX-relative file list; `age_s` is the max age across the paths
    (0 for staged_uncommitted — git doesn't surface stage-time); `hint` is
    the one-line operator suggestion the event + log line carry verbatim.
    """

    subkind: str
    paths: list[str]
    age_s: int
    hint: str


@dataclass
class JanitorReport:
    """Aggregate result of one `run_janitor` invocation.

    Empty `findings` ⇒ healthy; the routine emits NO events and NO
    operator_log line in that case. Returned so callers (tests, future
    `ap2 janitor` CLI) can introspect without re-reading events.jsonl.
    """

    findings: list[JanitorFinding] = field(default_factory=list)

    @property
    def total_paths(self) -> int:
        return sum(len(f.paths) for f in self.findings)


# --------------------------------------------------------------------------
# Detection helpers
# --------------------------------------------------------------------------


def _run_git(cfg: Config, *args: str) -> tuple[int, str]:
    """Invoke `git` in the project root; return (returncode, stdout)."""
    try:
        r = subprocess.run(
            ["git", *args],
            cwd=cfg.project_root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return r.returncode, r.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 1, ""


def _staged_paths(cfg: Config) -> list[str]:
    """Paths in the index that differ from HEAD (staged-but-uncommitted)."""
    rc, out = _run_git(cfg, "diff", "--cached", "--name-only")
    if rc != 0:
        return []
    return sorted(p for p in out.splitlines() if p.strip())


def _porcelain_lines(cfg: Config) -> list[tuple[str, str]]:
    """Return (status, path) pairs from `git status --porcelain`.

    Status is the two-char code (e.g. ` M`, `??`, `A `). Path is the
    POSIX-relative string. Quoted paths (git's default for paths with
    special characters) are returned with surrounding quotes stripped —
    callers compare against simple repo-relative names.
    """
    rc, out = _run_git(cfg, "status", "--porcelain")
    if rc != 0:
        return []
    pairs: list[tuple[str, str]] = []
    for line in out.splitlines():
        if len(line) < 4:
            continue
        status = line[:2]
        rest = line[3:]
        # Renames/copies have the form "OLD -> NEW"; we want the new path.
        if " -> " in rest:
            rest = rest.split(" -> ", 1)[1]
        if rest.startswith('"') and rest.endswith('"'):
            rest = rest[1:-1]
        pairs.append((status, rest))
    return pairs


def _path_mtime(cfg: Config, path: str) -> float:
    """Mtime of a working-tree file, or 0.0 if missing."""
    p = cfg.project_root / path
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


# --------------------------------------------------------------------------
# Subkind detectors
# --------------------------------------------------------------------------


def _check_staged_uncommitted(cfg: Config) -> JanitorFinding | None:
    """Surface staged-but-uncommitted paths regardless of age (TB-22 case).

    Skips the daemon-managed exclusion list because anything staged was
    actively put there by SOMETHING — if that something was the daemon's
    own commit path leaving residue, surfacing it is the bug we want to
    see. Staged files have no git-visible "stage time"; `age_s` is 0.
    """
    paths = _staged_paths(cfg)
    if not paths:
        return None
    return JanitorFinding(
        subkind="staged_uncommitted",
        paths=paths,
        age_s=0,
        hint=(
            "git status to inspect; commit with operator's intent or "
            "`git restore --staged <paths>` to unstage."
        ),
    )


def _check_modified_not_staged(
    cfg: Config, *, now: float | None = None
) -> JanitorFinding | None:
    """Tracked files modified ≥ MIN_MODIFIED_AGE_S ago and NOT staged.

    Excludes daemon-managed churn under `.cc-autopilot/`. The age guard
    keeps an in-flight task agent's mid-edit working tree from firing
    spurious findings — only stale modifications surface.
    """
    t = now if now is not None else time.time()
    paths: list[str] = []
    max_age = 0
    for status, path in _porcelain_lines(cfg):
        # `git status --porcelain` codes: index in col 1, worktree in col 2.
        # We want files where worktree is modified ('M' or 'D' or 'T') but
        # index column is space (i.e. NOT staged). Renames / additions are
        # caught by the staged check.
        if status[0] != " ":
            continue
        if status[1] not in ("M", "D", "T"):
            continue
        if _is_excluded(path):
            continue
        mt = _path_mtime(cfg, path)
        if mt <= 0:
            continue
        age = int(t - mt)
        if age < MIN_MODIFIED_AGE_S:
            continue
        paths.append(path)
        if age > max_age:
            max_age = age
    if not paths:
        return None
    return JanitorFinding(
        subkind="modified_not_staged",
        paths=sorted(paths),
        age_s=max_age,
        hint=(
            "tracked files modified but not staged for ≥"
            f" {MIN_MODIFIED_AGE_S // 60}min — `git diff` to inspect, "
            "then commit, stash, or `git checkout -- <paths>` to discard."
        ),
    )


def _check_untracked_non_ignored(cfg: Config) -> JanitorFinding | None:
    """Untracked files that .gitignore did NOT match.

    `git status --porcelain` already honors .gitignore, so '??' entries are
    by definition non-ignored. We still apply the daemon-managed exclusion
    list (defense in depth — if a daemon-managed path slips through the
    gitignore for any reason, we still don't want to fire on it).
    """
    paths: list[str] = []
    for status, path in _porcelain_lines(cfg):
        if status != "??":
            continue
        if _is_excluded(path):
            continue
        paths.append(path)
    if not paths:
        return None
    return JanitorFinding(
        subkind="untracked_non_ignored",
        paths=sorted(paths),
        age_s=0,
        hint=(
            "untracked files outside .gitignore — `git add` if intentional, "
            "delete if scratch, or extend .gitignore."
        ),
    )


# --------------------------------------------------------------------------
# Public entry
# --------------------------------------------------------------------------


def run_janitor(cfg: Config) -> JanitorReport:
    """Run all v1 janitor checks; emit events + operator_log line on findings.

    Side effects:
      - one `janitor_finding` event per (subkind, paths-set) — never per
        file. The events.jsonl tail stays readable even when a check
        returns 50 paths.
      - one summary line appended to `.cc-autopilot/operator_log.md` when
        at least one finding fires. Clean runs are silent (no noise on
        healthy projects — operator inbox stays calm).

    Returns the structured `JanitorReport` so callers can introspect
    without re-reading events.jsonl.

    Pure deterministic Python — no SDK call, no LLM. Cheap enough to run
    every cron tick without operator concern.
    """
    report = JanitorReport()
    for check in (
        _check_staged_uncommitted,
        _check_modified_not_staged,
        _check_untracked_non_ignored,
    ):
        finding = check(cfg)
        if finding is not None:
            report.findings.append(finding)

    if not report.findings:
        return report

    for f in report.findings:
        events.append(
            cfg.events_file,
            "janitor_finding",
            kind="git_stranded_state",
            subkind=f.subkind,
            paths=list(f.paths),
            age_s=f.age_s,
            hint=f.hint,
        )

    _append_operator_log_summary(cfg, report)
    return report


def _append_operator_log_summary(cfg: Config, report: JanitorReport) -> None:
    """Append one summary bullet to `.cc-autopilot/operator_log.md`.

    Format mirrors the rejection-line shape (`tools._append_operator_audit_line`):
    `- <ts> — janitor: N stranded-state finding(s) (M paths). See events.jsonl.`

    Single line per run keeps the log scannable. Per-finding detail lives
    in events.jsonl (one event per subkind, with paths inline).
    """
    log_path = cfg.project_root / ".cc-autopilot" / "operator_log.md"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    n_findings = len(report.findings)
    n_paths = report.total_paths
    line = (
        f"- {ts} — janitor: {n_findings} stranded-state finding"
        f"{'s' if n_findings != 1 else ''} ({n_paths} path"
        f"{'s' if n_paths != 1 else ''}). See events.jsonl.\n"
    )
    # Ensure the file ends with a newline before we append (matches the
    # convention `tools._append_operator_audit_line` leans on).
    existing = log_path.read_text() if log_path.exists() else ""
    if existing and not existing.endswith("\n"):
        existing += "\n"
    log_path.write_text(existing + line)


# --------------------------------------------------------------------------
# Read-side helper for `ap2 status` / status-report cron
# --------------------------------------------------------------------------


def recent_finding_count(cfg: Config, *, window_s: int | None = None) -> int:
    """Count `janitor_finding` events within the freshness window.

    Used by `cli.cmd_status` and `status_report` to decide whether to
    surface a "janitor: N findings" line. Findings older than `window_s`
    are stale (the next scheduled janitor run will re-emit them if still
    relevant) and intentionally not counted — the line should reflect the
    most recent janitor cycle, not a day-old run.

    `window_s` defaults to `RECENT_FINDING_WINDOW_S`. Returns 0 on a
    missing or unparseable events file (status surfaces should never
    crash on a transient I/O hiccup).
    """
    if not cfg.events_file.exists():
        return 0
    win = window_s if window_s is not None else RECENT_FINDING_WINDOW_S
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=win)
    cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    count = 0
    # 500-event window is enough headroom: the janitor emits at most 3
    # events per run (one per subkind), and operators rarely set the cadence
    # under 1h. So even the fast end of the operator-tuning range stays
    # well inside this window.
    for evt in events.tail(cfg.events_file, n=500):
        if evt.get("type") != "janitor_finding":
            continue
        ts = evt.get("ts") or ""
        if ts < cutoff_str:
            continue
        count += 1
    return count

"""Janitor — git-stranded-state detector + LLM-judge classifier (TB-177, TB-178).

This module is the **canary component** for the registry + manifest cleavage
landed by TB-309. Pre-TB-309 lived at `ap2/janitor.py`; the move into
`ap2/components/janitor/` is a structural refactor only (no behavior delta)
that pins the manifest contract every later axis-(5) migration follows.
The manifest lives in `manifest.py`; consumers (daemon, cli_daemon,
status_report) reach `run_janitor` / `recent_finding_counts_by_verdict`
through `ap2.registry.default_registry().hook(...)` rather than via a
direct `from ap2.components.janitor import …` (axis (6) gates the
import-direction rule, separate TB).

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

But "stranded" vs. "operator's deliberate draft" cannot be distinguished
by file-system inspection alone — both look identical to `git status`.
A staged file could be the post-TB-22 detritus (real strand) OR the
operator running `git add` to review staging before committing
(deliberate WIP). TB-178 layers an LLM judge on top of TB-177's
deterministic detector that classifies each finding as one of:

  real_strand       — high confidence the file is unintended detritus.
  operator_draft    — high confidence it's deliberate operator work.
  ambiguous         — judge couldn't make a confident call.

Per the operator's directive (TB-178), classified findings emit ONLY to
events.jsonl — NO summary line in the operator decision log. The
operator decision log stays curated for genuine operator decisions
(`ap2 ack`, queue ops, rejections); janitor findings — even classified —
are observability, not operator decisions. (Ideation Step 0 reads the
operator decision log as authoritative ground truth on operator
decisions; flooding it with auto-emitted janitor noise would dilute the
signal ideation calibrates against — TB-152, TB-163.)

What's NOT in v1 (explicit, so a future contributor doesn't get clever)
- Auto-commit / auto-stash / auto-discard. All destructive or surprising.
- Other detection kinds beyond `git_stranded_state` (dead-blocker,
  pipeline-pending-with-dead-pid, stale debug dumps, TASKS.md drift) —
  each gets its own TB once the framework's prove-out lands.
- Multi-finding LLM aggregation. v1 judges per-finding.
- Confidence-score field. Three discrete labels suffice.
- Memoization across cron runs. Each scan judges fresh.
- Multi-project scans. One daemon per project; one janitor per daemon.

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

Each finding is a `JanitorFinding(subkind, paths, age_s, hint, verdict,
reasoning)` record. The `hint` is a one-line operator suggestion (e.g.
"commit with the operator's intent, or `git restore --staged` to
unstage"); the `verdict` and `reasoning` fields are populated by the
TB-178 LLM judge after the deterministic detector runs.

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

Cost shape (TB-178)
-------------------
One janitor cron run with N findings issues N SDK calls (one judge call
per finding); the per-call shared-context block (events tail + operator
log tail + recent commits + active TB list) is built once and reused.
At default cadence (every 6h) and a healthy project (typically 0-2
findings per scan), expected cost is ~$0.05-0.20 per scan, captured in
`control_run_usage`-style `judge_call` events.

For projects with chronically-many findings, `AP2_JANITOR_MAX_FINDINGS_LLM`
(default 10) caps the per-run LLM budget: findings beyond the cap emit
with `verdict="ambiguous"` and skip the SDK call. Setting the env var
to 0 disables the judge entirely (deterministic-only fallback) — useful
for cost-constrained projects or repro-style tests.

Public surface
--------------
`run_janitor(cfg, sdk=None) -> JanitorReport` — the entry the daemon
dispatches via the cron-job table; also callable directly from tests.
Async because the per-finding judge step makes async SDK calls. When
`sdk is None` (or the env-cap is 0), the function falls back to
TB-177's deterministic-only behavior with `verdict="ambiguous"` on
every finding.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from ap2 import events
from ap2.config import Config
from ap2.json_extract import extract_rightmost_json_object


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

# Per-run cap on LLM judge calls (TB-178). A scan with N candidate findings
# issues at most `min(N, _max_findings_llm(cfg))` SDK calls; findings beyond
# the cap emit with `verdict="ambiguous"` (operator decides). Set to 0
# via env to disable the judge entirely and fall back to TB-177's
# deterministic behavior. The default of 10 matches the briefing's
# "expected cost ~$0.05-0.20 per scan" target — operators with chronic
# >10-finding scans should fix the underlying churn, not raise the cap.
_AP2_JANITOR_MAX_FINDINGS_LLM_DEFAULT = 10

# Default per-judge max-turns budget (TB-178). Cap on the SDK
# `ClaudeAgentOptions.max_turns` for the per-finding judge call. 12
# matches the pre-TB-330 inline `int(...)` fallback in `_judge_finding`'s
# `AP2_JANITOR_JUDGE_MAX_TURNS` env-read expression (TB-330 routed it
# through `cfg.get_component_value("janitor", "judge_max_turns")`).
_AP2_JANITOR_JUDGE_MAX_TURNS_DEFAULT = 12


def _max_findings_llm(cfg: Config) -> int:
    """Resolve the per-run LLM judge cap (TB-178; TB-330 cfg-routed).

    Resolution shape (TB-330 axis-5): routes through
    `cfg.get_component_value("janitor", "max_findings_llm")` so the
    legacy flat env name `AP2_JANITOR_MAX_FINDINGS_LLM` still wins via
    the `FLAT_TO_SECTIONED` reverse-lookup back-compat path while a
    `[components.janitor] max_findings_llm = N` TOML value flows through
    the same call when no env override is live. Call-time env-first
    precedence inside `get_component_value` preserves the
    pre-migration lazy-read pattern so a monkeypatched env value
    (the test idiom) takes effect on the next call without rebuilding
    cfg. Non-numeric values fall back to the default rather than
    crashing the cron run (operator typos shouldn't break janitor);
    negative values clamp to 0 (disabled).
    """
    raw = cfg.get_component_value("janitor", "max_findings_llm")
    if raw is None or (isinstance(raw, str) and raw == ""):
        return _AP2_JANITOR_MAX_FINDINGS_LLM_DEFAULT
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return _AP2_JANITOR_MAX_FINDINGS_LLM_DEFAULT
    return max(0, v)


def _judge_effort(cfg: Config) -> str:
    """Resolve the per-judge effort knob (TB-178; TB-330 cfg-routed).

    Same cfg-routed shape as `_max_findings_llm` for the janitor-
    specific value (`AP2_JANITOR_JUDGE_EFFORT` → sectioned
    `components.janitor.judge_effort`). Falls back to `AP2_AGENT_EFFORT`
    (a core-namespace knob — TB-334 routed it through
    `cfg.get_core_value("agent_effort", default="high")` so the
    sectioned-env > flat-env > TOML > default precedence applies
    uniformly across the agent-runtime cluster) and finally to
    `"high"` — same precedence chain the pre-TB-330 nested env-get
    fallback expression evaluated.
    """
    raw = cfg.get_component_value("janitor", "judge_effort")
    if isinstance(raw, str) and raw.strip():
        return raw
    if raw is not None and not isinstance(raw, str):
        # TOML-typed non-str (unlikely for a free-form effort label but
        # defensive): coerce to its string form.
        return str(raw)
    return cfg.get_core_value("agent_effort", default="high")


def _judge_max_turns(cfg: Config) -> int:
    """Resolve the per-judge max-turns knob (TB-178; TB-330 cfg-routed).

    Same cfg-routed shape as `_max_findings_llm` for the janitor-
    specific value (`AP2_JANITOR_JUDGE_MAX_TURNS` → sectioned
    `components.janitor.judge_max_turns`). Default 12 mirrors the
    pre-TB-330 `int(...)`-with-default fallback on the flat env name;
    non-numeric values fall back to the default rather than crashing
    the cron run (operator typos shouldn't break janitor).
    """
    raw = cfg.get_component_value("janitor", "judge_max_turns")
    if raw is None or (isinstance(raw, str) and raw == ""):
        return _AP2_JANITOR_JUDGE_MAX_TURNS_DEFAULT
    try:
        return int(raw)
    except (TypeError, ValueError):
        return _AP2_JANITOR_JUDGE_MAX_TURNS_DEFAULT


# Verdict vocabulary (TB-178). Codebase-fixed; per-project custom labels
# are explicit out-of-scope. Unknown verdicts coming back from the SDK
# fall back to "ambiguous" (defensive — a hallucinated label shouldn't
# silently render as a real_strand).
VERDICT_REAL_STRAND = "real_strand"
VERDICT_OPERATOR_DRAFT = "operator_draft"
VERDICT_AMBIGUOUS = "ambiguous"
KNOWN_VERDICTS = frozenset({
    VERDICT_REAL_STRAND,
    VERDICT_OPERATOR_DRAFT,
    VERDICT_AMBIGUOUS,
})

# Read-only tools the per-finding judge may use (mirrors `JUDGE_REPO_READ_TOOLS`
# in `verify.py`). The judge is read-only by construction — Read/Glob/Grep
# scoped to `cfg.project_root`, no Bash, no MCP-write. Same shape TB-136
# pinned for the prose-bullet judge.
JUDGE_REPO_READ_TOOLS = ["Read", "Glob", "Grep"]

# Lifecycle event types fed to the judge as recent-history context. Filtered
# tightly so the judge sees task arcs (start, complete, pipeline pending,
# verification fail, ideation approval) rather than the full firehose.
_JUDGE_LIFECYCLE_EVENT_TYPES = frozenset({
    "task_start",
    "task_complete",
    "task_pipeline_pending",
    "verification_failed",
    "ideation_approved",
    "cron_complete",
    "cron_error",
    "pipeline_task_start",
})

# Truncation caps for the per-finding judge prompt. The static bounds
# keep the prompt size predictable per call (a few KB total); the judge
# can pull more detail via Read/Glob/Grep if needed.
_JUDGE_EVENTS_TAIL_N = 50
_JUDGE_RECENT_COMMITS_N = 10
_JUDGE_REASONING_MAX_CHARS = 200


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

    TB-178: `verdict` and `reasoning` are populated by the LLM judge
    after the deterministic detector runs. `verdict` is always one of
    `KNOWN_VERDICTS`; defaults to `ambiguous` until classified (so
    callers reading a pre-judge `JanitorFinding` see a safe default).
    `reasoning` is a one-sentence rationale capped at
    `_JUDGE_REASONING_MAX_CHARS`, empty when the judge was disabled or
    skipped (cap overflow).
    """

    subkind: str
    paths: list[str]
    age_s: int
    hint: str
    verdict: str = VERDICT_AMBIGUOUS
    reasoning: str = ""


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


async def run_janitor(cfg: Config, sdk=None) -> JanitorReport:
    """Run all v1 janitor checks; emit one classified event per finding.

    Side effects (TB-178 contract):
      - one `janitor_finding` event per (subkind, paths-set) carrying
        `verdict` ∈ {real_strand, operator_draft, ambiguous} and a
        one-sentence `reasoning` (≤200 chars). Never per-file — the
        events.jsonl tail stays readable even when a check returns 50
        paths.
      - one `judge_call` event per LLM judge invocation (token usage,
        cost, model, duration), so cost-tradeoff experiments can
        aggregate per-judge spend without routing through the daemon's
        `_log_message` capture path.
      - **NO write to the operator decision log.** Per the operator's
        directive (TB-178), classified findings emit ONLY to
        events.jsonl; the operator decision log is reserved for
        genuine operator decisions (`ap2 ack`, queue ops, rejections).
        Janitor findings — even classified — are observability, not
        operator decisions.

    Returns the structured `JanitorReport` so callers can introspect
    without re-reading events.jsonl.

    Async because the per-finding judge step makes async SDK calls. When
    `sdk is None` (or `AP2_JANITOR_MAX_FINDINGS_LLM=0`), the function
    falls back to TB-177's deterministic behavior: every finding emits
    with `verdict="ambiguous"` and no SDK calls fire.
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

    # TB-178: classify each finding via the LLM judge before emitting
    # events. Cap-aware (overflow → "ambiguous"); SDK-optional (None
    # falls back to "ambiguous" without judging). The shared-context
    # block is built once per run and reused across per-finding calls.
    cap = _max_findings_llm(cfg)
    if sdk is not None and cap > 0:
        shared_ctx = _build_judge_shared_context(cfg)
        for i, f in enumerate(report.findings):
            if i >= cap:
                f.verdict = VERDICT_AMBIGUOUS
                f.reasoning = (
                    f"skipped: exceeded AP2_JANITOR_MAX_FINDINGS_LLM={cap}"
                )
                continue
            verdict, reasoning = await _judge_finding(
                cfg, sdk, f, shared_ctx,
            )
            f.verdict = verdict
            f.reasoning = reasoning
    # else: judge disabled. Findings keep their dataclass-default
    # `verdict="ambiguous"` and empty `reasoning` — emitted as-is below.

    for f in report.findings:
        events.append(
            cfg.events_file,
            "janitor_finding",
            kind="git_stranded_state",
            subkind=f.subkind,
            paths=list(f.paths),
            age_s=f.age_s,
            hint=f.hint,
            verdict=f.verdict,
            reasoning=f.reasoning,
        )

    return report


# --------------------------------------------------------------------------
# LLM judge — classifier for `JanitorFinding` (TB-178)
# --------------------------------------------------------------------------


def _build_judge_shared_context(cfg: Config) -> str:
    """Compose the per-run static-context block fed to every per-finding judge.

    The block is built once per `run_janitor` call (NOT per-finding), so a
    scan with N findings doesn't re-walk events.jsonl / git log N times.
    Composes:

      - Last `_JUDGE_EVENTS_TAIL_N` events from events.jsonl, filtered to
        the lifecycle subset (`_JUDGE_LIFECYCLE_EVENT_TYPES`) — task arcs
        rather than the full firehose.
      - Last `_JUDGE_RECENT_COMMITS_N` commit SHAs + subjects + changed
        paths (so the judge can correlate finding paths with recently-
        committed work).
      - The list of TB-Ns currently in Active / Backlog / Pipeline
        Pending with their briefing paths (so the judge can `Read` a
        briefing if a finding's file paths look scope-relevant).

    Each section is small + bounded; the total is a few KB. The judge can
    pull more detail via Read/Glob/Grep if it needs to.
    """
    parts: list[str] = []

    # Lifecycle events tail.
    lifecycle: list[str] = []
    try:
        for evt in events.tail(cfg.events_file, n=300):
            if evt.get("type") not in _JUDGE_LIFECYCLE_EVENT_TYPES:
                continue
            ts = evt.get("ts", "")
            typ = evt.get("type", "")
            extras = {
                k: v for k, v in evt.items()
                if k not in ("ts", "type")
                and k in ("task", "title", "job", "kind", "name", "pid")
            }
            extra_str = " ".join(f"{k}={v}" for k, v in extras.items())
            lifecycle.append(f"  {ts} {typ} {extra_str}".rstrip())
    except (OSError, ValueError):
        pass
    lifecycle = lifecycle[-_JUDGE_EVENTS_TAIL_N:]
    parts.append("Recent lifecycle events (most recent last):")
    parts.append("\n".join(lifecycle) if lifecycle else "  (none)")

    # Recent commits with changed paths.
    commits_block: list[str] = []
    rc, log_out = _run_git(
        cfg,
        "log",
        f"-n{_JUDGE_RECENT_COMMITS_N}",
        "--pretty=format:%h %s",
        "--name-only",
    )
    if rc == 0:
        # `git log --name-only` separates entries with blank lines; group them.
        current: list[str] = []
        groups: list[list[str]] = []
        for ln in log_out.splitlines():
            if ln.strip() == "":
                if current:
                    groups.append(current)
                    current = []
            else:
                current.append(ln)
        if current:
            groups.append(current)
        for grp in groups:
            header = grp[0]
            paths = grp[1:]
            paths_str = ", ".join(paths[:8]) + (
                f" (+{len(paths) - 8} more)" if len(paths) > 8 else ""
            )
            commits_block.append(f"  {header}    paths: {paths_str}")
    parts.append("\nRecent commits (most recent first):")
    parts.append("\n".join(commits_block) if commits_block else "  (none)")

    # Active / Backlog / Pipeline-Pending TB list.
    tb_block: list[str] = []
    try:
        from ap2.board import Board

        board = Board.load(cfg.tasks_file)
        for section in ("Active", "Backlog", "Pipeline Pending"):
            for line in board.sections.get(section, []):
                tb_block.append(f"  [{section}] {line.strip()}")
    except Exception:  # noqa: BLE001
        pass
    parts.append(
        "\nIn-flight tasks (Active / Backlog / Pipeline Pending). "
        "Briefings live under .cc-autopilot/tasks/ — Read them if a "
        "finding path looks scope-relevant:"
    )
    parts.append("\n".join(tb_block) if tb_block else "  (none)")

    return "\n".join(parts)


async def _judge_finding(
    cfg: Config,
    sdk,
    finding: JanitorFinding,
    shared_context: str,
) -> tuple[str, str]:
    """Ask the SDK to classify ONE finding as real_strand / operator_draft / ambiguous.

    Returns ``(verdict, reasoning)``. On any error (SDK exception, parse
    failure, unknown verdict), returns ``("ambiguous", "<error>")`` rather
    than raising — a janitor scan must never crash the daemon's cron loop.

    Emits a `judge_call` event (TB-157 shape) carrying usage / model /
    cost / verdict / duration so per-finding judge spend can be aggregated
    out of band of the daemon's `_log_message` capture path.
    """
    paths_inline = ", ".join(finding.paths[:10])
    if len(finding.paths) > 10:
        paths_inline += f" (+{len(finding.paths) - 10} more)"

    prompt = (
        "You are classifying ONE janitor finding (a candidate stranded git-"
        "state observation) as either unintended detritus or deliberate "
        "operator work. Answer with ONE LINE of JSON: "
        '{"verdict": "real_strand" | "operator_draft" | "ambiguous", '
        '"reasoning": "<one sentence, ≤200 chars>"}. '
        "Do not include any other text outside that JSON line.\n\n"
        "Verdict semantics:\n"
        "  real_strand    — high confidence the file is unintended detritus. "
        "Examples: a staged file matches a recently-completed pipeline "
        "task's expected output paths AND the pipeline log shows a commit "
        "failure; OR an untracked file in a directory whose siblings are "
        "gitignored and no recent operator activity touches the path.\n"
        "  operator_draft — high confidence the file is deliberate operator "
        "work. Examples: an untracked file in repo root with operator-style "
        "naming (`draft_*.md`, `notes-*.md`, `scratch.*`, `goal-draft.md`) "
        "AND no TB-N references it; OR a working-tree-modified file the "
        "operator has been actively touching (mtime within last hour).\n"
        "  ambiguous      — judge cannot make a confident call.\n\n"
        "You have Read, Glob, and Grep tools scoped to the project root. "
        "Use them sparingly: read a briefing under .cc-autopilot/tasks/ "
        "only if a finding path looks scope-relevant to a TB-N in the "
        "in-flight list; grep recent files to confirm a hypothesis. "
        "Default to `ambiguous` rather than guessing.\n\n"
        f"Finding:\n"
        f"  subkind: {finding.subkind}\n"
        f"  paths: {paths_inline}\n"
        f"  age_s: {finding.age_s}\n"
        f"  hint: {finding.hint}\n\n"
        f"Static context (built once per janitor run):\n"
        f"{shared_context}\n"
    )

    verdict = VERDICT_AMBIGUOUS
    reasoning = ""
    text = ""
    result_meta: dict = {}
    error_note = ""
    t0 = time.monotonic()
    try:
        effort = _judge_effort(cfg)
        options = sdk.ClaudeAgentOptions(
            cwd=str(cfg.project_root),
            allowed_tools=list(JUDGE_REPO_READ_TOOLS),
            permission_mode="bypassPermissions",
            max_turns=_judge_max_turns(cfg),
            setting_sources=["project"],
            # TB-344: schema is the single source of truth for the
            # agent_model default (see CORE_CONFIG_SCHEMA).
            model=cfg.get_core_value("agent_model"),
            extra_args={"effort": effort},
        )
        async for msg in sdk.query(prompt=prompt, options=options):
            content = getattr(msg, "content", None)
            if isinstance(content, list):
                for part in content:
                    t = getattr(part, "text", None)
                    if isinstance(t, str) and t.strip():
                        text = t.strip()
            else:
                t = getattr(msg, "result", None)
                if isinstance(t, str) and t.strip():
                    text = t.strip()
            for k in ("model", "num_turns", "total_cost_usd", "stop_reason"):
                v = getattr(msg, k, None)
                if v is not None:
                    result_meta[k] = v
            for k in ("usage", "model_usage"):
                v = getattr(msg, k, None)
                if isinstance(v, dict) and v:
                    result_meta[k] = v
    except Exception as e:  # noqa: BLE001
        error_note = f"judge error: {type(e).__name__}: {e}"
    duration_s = time.monotonic() - t0

    if error_note:
        verdict = VERDICT_AMBIGUOUS
        reasoning = error_note
    else:
        verdict, reasoning = _parse_judge_response(text)

    # Cap reasoning to the documented bound.
    if len(reasoning) > _JUDGE_REASONING_MAX_CHARS:
        reasoning = (
            reasoning[: _JUDGE_REASONING_MAX_CHARS - 1].rstrip() + "…"
        )

    # Emit `judge_call` (TB-157 shape) — best-effort; a write failure
    # here must not flip the judge's verdict.
    try:
        payload: dict = {
            "task": "",
            "bullet_idx": -1,
            "bullet_kind": f"janitor:{finding.subkind}",
            "verdict": verdict,
            "duration_s": round(duration_s, 3),
        }
        for k in ("model", "num_turns", "total_cost_usd",
                  "stop_reason", "usage", "model_usage"):
            if k in result_meta:
                payload[k] = result_meta[k]
        events.append(cfg.events_file, "judge_call", **payload)
    except Exception:  # noqa: BLE001
        pass

    return verdict, reasoning


def _parse_judge_response(response: str) -> tuple[str, str]:
    """Extract `(verdict, reasoning)` from the judge's reply.

    TB-261: extracts the **rightmost top-level** balanced ``{...}``
    substring via ``ap2.json_extract.extract_rightmost_json_object``;
    any parse failure or unknown verdict falls back to
    ``("ambiguous", "<note>")``. Pre-TB-261 the boundary was first-
    ``{`` to last-``}``, which an LLM preamble containing literal
    braces (e.g. set notation, code blocks) could shadow.
    """
    if not response:
        return VERDICT_AMBIGUOUS, "empty judge response"
    extracted = extract_rightmost_json_object(response)
    if extracted is None:
        # No parseable JSON object at all — could be no braces, could
        # be malformed JSON. Surface the response prefix so an
        # operator scanning the events file can eyeball the shape.
        return VERDICT_AMBIGUOUS, f"no JSON in response: {response[:120]!r}"
    data, _, _ = extracted
    verdict = str(data.get("verdict", "")).strip().lower()
    reasoning = str(data.get("reasoning", "")).strip()
    if verdict not in KNOWN_VERDICTS:
        return VERDICT_AMBIGUOUS, (
            f"unknown verdict {verdict!r}; reasoning: {reasoning[:120]}"
        )
    return verdict, reasoning


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

    TB-178: this is the legacy total-count helper — kept for backward
    compat with the JSON `janitor_findings` field. New surfacing logic
    should use `recent_finding_counts_by_verdict` so strands count for
    operator urgency while drafts get a softer summary.
    """
    counts = recent_finding_counts_by_verdict(cfg, window_s=window_s)
    return sum(counts.values())


def recent_finding_counts_by_verdict(
    cfg: Config, *, window_s: int | None = None,
) -> dict[str, int]:
    """Per-verdict count of recent `janitor_finding` events (TB-178).

    Returns a dict ``{"real_strand": N, "operator_draft": M,
    "ambiguous": K}`` (always all three keys, defaulting to 0). Findings
    older than `window_s` (default `RECENT_FINDING_WINDOW_S`) are
    excluded — same freshness contract as `recent_finding_count`.

    Findings missing a `verdict` field (legacy events from pre-TB-178
    runs) bucket as `ambiguous` so the surfacing logic stays consistent
    when the events.jsonl tail mixes old and new formats.
    """
    out = {
        VERDICT_REAL_STRAND: 0,
        VERDICT_OPERATOR_DRAFT: 0,
        VERDICT_AMBIGUOUS: 0,
    }
    if not cfg.events_file.exists():
        return out
    win = window_s if window_s is not None else RECENT_FINDING_WINDOW_S
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=win)
    cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    for evt in events.tail(cfg.events_file, n=500):
        if evt.get("type") != "janitor_finding":
            continue
        ts = evt.get("ts") or ""
        if ts < cutoff_str:
            continue
        verdict = str(evt.get("verdict") or VERDICT_AMBIGUOUS).strip().lower()
        if verdict not in out:
            verdict = VERDICT_AMBIGUOUS
        out[verdict] += 1
    return out

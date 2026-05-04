"""Project state-file integrity check (TB-108).

Pure inspection: walks the project's `.cc-autopilot/` + root state files,
returns a structured `CheckReport` with `Issue` entries. Used by
`ap2 check` (a sibling of `ap2 doctor` — doctor checks the environment;
check checks the data on disk).

Distinct from the daemon's runtime detection (`Board.malformed_lines` →
`board_malformed_line` events): a one-shot operator-runnable view that
covers TASKS.md shape, briefing-link integrity, cron.yaml schema, JSON
state-file parseability, and insights front-matter completeness.

Severity:
  - `error`   — daemon will likely choke or silently misbehave; fix.
  - `warning` — non-load-bearing; flag for cleanup.

Exit code from `ap2 check` reflects errors only (warnings don't fail
CI). `--json` for machine-readable output.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .board import Board, SECTIONS
from .config import Config
from .cron import load_jobs
from .init import (
    BRIEFING_REQUIRED_SECTIONS,
    GOAL_ANCHOR_HEADINGS,
    WHY_NOW_MIN_CHARS,
)
from .insights import _parse_front_matter
from .tools import (
    _briefing_section_body,
    _goal_md_anchors,
    _normalize_anchor,
    _why_now_paragraph,
)


@dataclass
class Issue:
    severity: str  # "error" | "warning"
    file: str
    message: str


@dataclass
class CheckReport:
    project_root: Path
    issues: list[Issue] = field(default_factory=list)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors


def check_project(cfg: Config) -> CheckReport:
    issues: list[Issue] = []
    issues.extend(_check_tasks_md(cfg))
    issues.extend(_check_briefing_links(cfg))
    issues.extend(_check_briefings_manual_bullets(cfg))
    issues.extend(_check_briefing_structure(cfg))
    issues.extend(_check_cron_yaml(cfg))
    issues.extend(_check_json_state(cfg))
    issues.extend(_check_insights(cfg))
    issues.extend(_check_optional_files(cfg))
    return CheckReport(project_root=cfg.project_root, issues=issues)


# Each of the 5 section headers must appear in TASKS.md in this exact
# order. Order matters: `Board._parse` reads the file linearly, and
# `next_ready` / `next_dispatchable` walk Active → Ready → Backlog
# expecting the canonical order. Out-of-order sections are not just
# cosmetic — they can route tasks through the wrong precedence.
_SECTION_ORDER_RE = re.compile(r"^##\s+([A-Za-z][A-Za-z ]*?)\s*$", re.M)


def _check_tasks_md(cfg: Config) -> list[Issue]:
    issues: list[Issue] = []
    if not cfg.tasks_file.exists():
        return [Issue("error", "TASKS.md", "missing")]
    text = cfg.tasks_file.read_text()
    found = [
        m.group(1)
        for m in _SECTION_ORDER_RE.finditer(text)
        if m.group(1) in SECTIONS
    ]
    if found != SECTIONS:
        issues.append(Issue(
            "error", "TASKS.md",
            f"section order/presence wrong: expected {SECTIONS}, got {found}",
        ))
    # Board._parse already collects malformed lines — surface them per-line.
    board = Board.load(cfg.tasks_file)
    for section, line in board.malformed_lines:
        snippet = line.strip()
        if len(snippet) > 80:
            snippet = snippet[:77] + "..."
        issues.append(Issue(
            "error", "TASKS.md",
            f"malformed line in {section}: {snippet!r}",
        ))
    return issues


_BRIEF_LINK_RE = re.compile(r"\[→ brief\]\(([^)]+)\)")


def _check_briefing_links(cfg: Config) -> list[Issue]:
    """Every `[→ brief](path)` in TASKS.md must resolve to a real file.
    Stale links accumulate when briefings are moved/deleted out of band
    and silently break the task agent that tries to `Read` them.
    """
    issues: list[Issue] = []
    if not cfg.tasks_file.exists():
        return issues
    text = cfg.tasks_file.read_text()
    for m in _BRIEF_LINK_RE.finditer(text):
        path = m.group(1).strip()
        full = (cfg.project_root / path).resolve()
        if not full.is_file():
            issues.append(Issue(
                "warning", "TASKS.md",
                f"briefing link points to missing file: {path}",
            ))
    return issues


# Match a list bullet (`-` or `*`) whose first non-whitespace token is
# `Manual:` or `[manual]` (any case). Anchors on the bullet marker so we
# don't false-positive on prose that mentions the word inline.
_MANUAL_BULLET_RE = re.compile(
    r"^\s*[-*]\s*(?:Manual\s*:|\[manual\])",
    re.IGNORECASE,
)
_VERIFICATION_HEADER_RE = re.compile(r"^##\s+Verification\s*$", re.M)
_NEXT_SECTION_RE = re.compile(r"^##\s+", re.M)


def _check_briefings_manual_bullets(cfg: Config) -> list[Issue]:
    """TB-138: warn when a briefing's `## Verification` section contains a
    `Manual:` (or `[manual]`) bullet. Such bullets cannot be evaluated by
    the unattended per-task verifier — TB-122 hit `retry_exhausted` on a
    single manual bullet despite the implementation being complete. The
    rule is enforced at the briefing-author layer (ideation prompt + ap2-task
    skill + briefing template); this lint is the operator-facing safety net.
    Non-fatal — surfaced as a warning so the operator can fix it before
    dispatch without blocking other ap2 check usage.
    """
    issues: list[Issue] = []
    tasks_dir = cfg.project_root / ".cc-autopilot" / "tasks"
    if not tasks_dir.exists():
        return issues
    for f in sorted(tasks_dir.iterdir()):
        if not f.is_file() or f.suffix != ".md":
            continue
        try:
            text = f.read_text()
        except OSError:
            continue
        m = _VERIFICATION_HEADER_RE.search(text)
        if not m:
            continue
        # Slice from the header to the next `## ` (or end of file).
        start = m.end()
        next_m = _NEXT_SECTION_RE.search(text, start)
        body = text[start: next_m.start() if next_m else len(text)]
        for line in body.splitlines():
            if _MANUAL_BULLET_RE.match(line):
                issues.append(Issue(
                    "warning", f.name,
                    "Verification section contains a `Manual:` bullet "
                    "(TB-138) — convert to an auto-verifiable shell "
                    "command, test name, or judge-checkable prose, or "
                    "move to `## Out of scope`",
                ))
                break  # one warning per file is enough
    return issues


# TB-154: top-level (`##`) section header pattern. Same shape as
# `ap2/tools.py::_BRIEFING_SECTION_RE` — kept in sync deliberately.
# Tolerates trailing content after the section name (e.g.
# `## Verification (launch-task — ...)`) so the lint mirrors the
# queue-append-time validator's acceptance set rather than its own.
_BRIEFING_STRUCTURE_HEADER_RE = re.compile(
    r"^##\s+([A-Za-z][A-Za-z ]*?)(?:\s*[(\-—:].*)?\s*$", re.M,
)


def _check_briefing_structure(cfg: Config) -> list[Issue]:
    """TB-154 lint: warn on on-disk briefings whose `##`-level section
    structure isn't canonical.

    The hard gate at queue-append time (`do_operator_queue_append` /
    `do_board_edit`) refuses non-canonical briefings before they land
    on disk; this lint is the operator-facing safety net for legacy or
    operator-edited briefings already in `.cc-autopilot/tasks/`.
    Warning-level (not error) so the operator can opportunistically
    fix the legacy entry without `ap2 check` going red — bulk
    migration is explicitly out of scope (per the briefing's own
    Out-of-scope list).

    Mirrors `_check_briefing_links`'s warning shape and per-file
    iteration so the report's UX stays consistent across the
    briefing-quality lints.
    """
    issues: list[Issue] = []
    tasks_dir = cfg.project_root / ".cc-autopilot" / "tasks"
    if not tasks_dir.exists():
        return issues
    required = set(BRIEFING_REQUIRED_SECTIONS)
    # TB-161: derive goal anchors once per `ap2 check` invocation. When
    # goal.md is missing or all-placeholder, `_goal_md_anchors` returns
    # an empty set and the goal-anchor lint short-circuits per file.
    # Same heading source as the queue-append validator
    # (`_validate_briefing_structure`) — single source of truth via
    # `GOAL_ANCHOR_HEADINGS`.
    goal_anchors = _goal_md_anchors(cfg.project_root / "goal.md")
    for f in sorted(tasks_dir.iterdir()):
        if not f.is_file() or f.suffix != ".md":
            continue
        try:
            text = f.read_text()
        except OSError:
            continue
        if not text.strip():
            # Empty briefing on disk is its own anomaly but `_check_briefing_links`
            # already surfaces missing/broken target paths, and an empty file
            # would never have been queue-appended post-TB-135. Skip silently
            # so we don't double-report the same operator-attention candidate.
            continue
        found = {
            m.group(1).strip()
            for m in _BRIEFING_STRUCTURE_HEADER_RE.finditer(text)
        }
        missing = sorted(required - found)
        if missing:
            missing_str = ", ".join(f"`## {s}`" for s in missing)
            issues.append(Issue(
                "warning", f.name,
                f"briefing structure non-canonical (TB-154): missing "
                f"{missing_str}. Canonical sections: "
                f"{', '.join('## ' + s for s in BRIEFING_REQUIRED_SECTIONS)}. "
                "The queue-append validator rejects new briefings with "
                "this shape; legacy on-disk briefings are flagged "
                "(non-fatal) so they can be opportunistically fixed.",
            ))
            # Don't double-warn: a briefing missing `## Goal` already
            # surfaced its main fix-up; the goal-anchor lint below would
            # just repeat the operator-action signal.
            continue
        # TB-161: goal-anchor lint. Warning-level companion to the
        # queue-append-time hard gate — surfaces legacy briefings whose
        # `## Goal` body cites no goal.md anchor without blocking
        # `ap2 check` (bulk migration is explicitly out of scope; the
        # operator decides whether to rewrite or leave). Only fires
        # when goal.md actually exposes anchors — a fresh project with
        # an all-placeholder goal.md gets no spurious warnings.
        goal_body = _briefing_section_body(text, "Goal")
        if goal_anchors and goal_body.strip():
            norm = _normalize_anchor(goal_body)
            if not any(a in norm for a in goal_anchors):
                anchor_heading_list = ", ".join(
                    f"`## {h}`" for h in GOAL_ANCHOR_HEADINGS
                )
                issues.append(Issue(
                    "warning", f.name,
                    "briefing `## Goal` body cites no goal.md anchor "
                    "(TB-161): no substring matches any "
                    f"{anchor_heading_list} heading title or Done-when "
                    "bullet from goal.md. The queue-append validator "
                    "rejects new briefings with this shape; legacy "
                    "on-disk briefings are flagged (non-fatal) so they "
                    "can be opportunistically rewritten or moved to "
                    "ap2-meta-polish out-of-scope.",
                ))
                # Don't double-warn: a Goal that fails the anchor check
                # already names the operator-action signal; the why-now
                # lint below is a separate failure mode but stacking
                # both warnings on one briefing just adds noise.
                continue
        # TB-164: "why now" rationale lint. Warning-level companion to
        # the queue-append-time hard gate — surfaces legacy briefings
        # whose `## Goal` body lacks a "Why now" delete-test rationale
        # (or whose marker is present but the paragraph is shorter than
        # `WHY_NOW_MIN_CHARS`). Non-fatal; the operator decides whether
        # to rewrite. Same skip logic as the lint above: empty `## Goal`
        # body short-circuits (the missing-section warning already
        # covers it).
        if not goal_body.strip():
            continue
        rationale = _why_now_paragraph(goal_body)
        if rationale is None:
            issues.append(Issue(
                "warning", f.name,
                "briefing `## Goal` body lacks a 'Why now' rationale "
                "(TB-164 — goal.md's delete-test): no line-anchored "
                "`Why now` marker found. Add a line like `Why now: "
                "<one sentence answering 'if we delete this and the "
                "goal still ships, was it useful?'>` inside the Goal "
                "body so the delete-test is articulated in writing. "
                "The queue-append validator rejects new briefings "
                "with this shape; legacy on-disk briefings are flagged "
                "(non-fatal) so they can be opportunistically fixed.",
            ))
        elif len(rationale) < WHY_NOW_MIN_CHARS:
            issues.append(Issue(
                "warning", f.name,
                "briefing `## Goal` body has a 'Why now' marker but "
                f"the rationale is only {len(rationale)} chars "
                f"(min {WHY_NOW_MIN_CHARS}; TB-164 — goal.md's "
                "delete-test). Trivial rationales like `Why now: yes` "
                "fail the queue-append validator; flesh out the "
                "delete-test answer in writing.",
            ))
    return issues


def _check_cron_yaml(cfg: Config) -> list[Issue]:
    if not cfg.cron_file.exists():
        return []
    try:
        jobs = load_jobs(cfg.cron_file)
    except Exception as e:  # noqa: BLE001
        return [Issue("error", "cron.yaml", f"parse failed: {type(e).__name__}: {e}")]
    issues: list[Issue] = []
    for j in jobs:
        if not j.name:
            issues.append(Issue("error", "cron.yaml", "job missing name"))
            continue
        if not j.prompt or not j.prompt.strip():
            issues.append(Issue(
                "error", "cron.yaml",
                f"job {j.name!r} has empty prompt",
            ))
        if j.interval_s <= 0:
            issues.append(Issue(
                "error", "cron.yaml",
                f"job {j.name!r} has non-positive interval ({j.interval_s}s)",
            ))
    return issues


def _check_json_state(cfg: Config) -> list[Issue]:
    """JSON state files are parsed lazily by the daemon; corruption
    surfaces as a runtime exception. One-shot check parses each file
    and reports decode errors with line+column from `JSONDecodeError`."""
    issues: list[Issue] = []
    files = [
        cfg.cron_state_file,
        cfg.retry_state_file,
        cfg.mm_state_file,
        cfg.auto_diagnose_state_file,
    ]
    for f in files:
        if not f.exists():
            continue
        try:
            json.loads(f.read_text())
        except json.JSONDecodeError as e:
            issues.append(Issue(
                "error", f.name,
                f"corrupt JSON: {e.msg} (line {e.lineno}, col {e.colno})",
            ))
        except OSError as e:
            issues.append(Issue("error", f.name, f"unreadable: {e}"))
    return issues


def _check_insights(cfg: Config) -> list[Issue]:
    """Each `*.md` in `.cc-autopilot/insights/` (excluding `_index.md`)
    should have parseable YAML front matter with `tldr`/`updated`/
    `updated_by`/`cites`. The auto-regenerated index drops files
    silently when front matter is missing — operator-visible warning
    catches the silent disappearance.
    """
    insights_dir = cfg.project_root / ".cc-autopilot" / "insights"
    if not insights_dir.exists():
        return []
    issues: list[Issue] = []
    for f in sorted(insights_dir.iterdir()):
        if not f.is_file() or f.suffix != ".md" or f.name == "_index.md":
            continue
        try:
            text = f.read_text()
        except OSError as e:
            issues.append(Issue("warning", f.name, f"unreadable: {e}"))
            continue
        fm = _parse_front_matter(text)
        if not fm:
            issues.append(Issue(
                "warning", f.name,
                "missing or unparseable YAML front matter (won't appear in _index.md)",
            ))
            continue
        for k in ("tldr", "updated", "updated_by", "cites"):
            if k not in fm:
                issues.append(Issue(
                    "warning", f.name,
                    f"front matter missing key: {k!r}",
                ))
    return issues


def _check_optional_files(cfg: Config) -> list[Issue]:
    """Files the prompt expects to exist for full functionality. Missing
    them isn't fatal — ideation has fallbacks — but each absence reduces
    grounding quality."""
    issues: list[Issue] = []
    if not (cfg.project_root / "goal.md").exists():
        issues.append(Issue(
            "warning", "goal.md",
            "missing — ideation will fall back to inferring goals from "
            "CLAUDE.md + progress.md",
        ))
    return issues


def render_text(report: CheckReport) -> str:
    if report.ok and not report.warnings:
        return f"ap2 check: clean ({report.project_root})"
    n_err = len(report.errors)
    n_warn = len(report.warnings)
    lines = [
        f"ap2 check: {n_err} error{'s' if n_err != 1 else ''}, "
        f"{n_warn} warning{'s' if n_warn != 1 else ''}  "
        f"({report.project_root})"
    ]
    by_file: dict[str, list[Issue]] = {}
    for i in report.issues:
        by_file.setdefault(i.file, []).append(i)
    for f, items in sorted(by_file.items()):
        lines.append(f"\n{f}:")
        for i in items:
            tag = "ERROR" if i.severity == "error" else "warn "
            lines.append(f"  [{tag}] {i.message}")
    return "\n".join(lines)


def render_json(report: CheckReport) -> str:
    return json.dumps(
        {
            "project_root": str(report.project_root),
            "ok": report.ok,
            "errors": [
                {"file": i.file, "message": i.message} for i in report.errors
            ],
            "warnings": [
                {"file": i.file, "message": i.message} for i in report.warnings
            ],
        },
        indent=2,
    )

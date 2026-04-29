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
from .insights import _parse_front_matter


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
_SECTION_ORDER_RE = re.compile(r"^##\s+(\w+)\s*$", re.M)


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

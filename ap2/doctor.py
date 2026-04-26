"""`ap2 doctor` — one-shot readiness check for the ap2 setup.

Combines `user_audit` (does the sandbox user exist + is it cred-clean?) with
`project_audit` (is there a sandbox clone of THIS project at the expected
path?), plus a check that the `ap2` CLI is installed for the sandbox user.

The output is a flat list of OK / FAIL / WARN / INFO lines designed to
replace the manual environment-check ladder previously done in markdown by
the setup-project skill.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .config import DEFAULT_VERIFY_TIMEOUT_S
from .sandbox import (
    AuditResult,
    DEFAULT_USER,
    _user_exists,
    _user_home,
    project_audit,
    user_audit,
)


@dataclass
class DoctorReport:
    sections: list[tuple[str, AuditResult]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(s.ok for _, s in self.sections)

    def print(self) -> None:
        for title, res in self.sections:
            print(f"[{title}]")
            res.print()
            print()
        print("doctor: " + ("OK" if self.ok else "FAIL — see above"))


def _ap2_installed_for_user(user: str) -> AuditResult:
    res = AuditResult()
    if not _user_exists(user):
        res.add("FAIL", f"user {user!r} does not exist (run: ap2 sandbox user-setup)")
        return res
    r = subprocess.run(
        ["sudo", "-u", user, "-i", "bash", "-c", "command -v ap2 || true"],
        capture_output=True, text=True,
    )
    path = r.stdout.strip()
    if path:
        res.add("OK", f"ap2 CLI on $PATH for {user}: {path}")
    else:
        res.add(
            "FAIL",
            f"ap2 not on $PATH for {user}. As that user run: "
            f"uv tool install --from <path-to-claude-tools> 'claude-automation[ap2]'",
        )
    return res


def _project_init_state(project_root: Path) -> AuditResult:
    """Verify the local clone has the bare-minimum on-disk skeleton.

    Doesn't try to run `ap2 init` for the user — just reports what's missing
    so the next-step is obvious.
    """
    res = AuditResult()
    expected = [
        (project_root / "TASKS.md", "TASKS.md"),
        (project_root / ".cc-autopilot", ".cc-autopilot/"),
        (project_root / ".cc-autopilot" / "progress.md", ".cc-autopilot/progress.md"),
        (project_root / ".cc-autopilot" / "tasks", ".cc-autopilot/tasks/"),
        (project_root / "CLAUDE.md", "CLAUDE.md"),
    ]
    missing = [name for path, name in expected if not path.exists()]
    if missing:
        res.add("FAIL", f"missing: {', '.join(missing)} — run: ap2 init")
        return res

    # CLAUDE.md exists but make sure it has the Autopilot section the daemon reads.
    text = (project_root / "CLAUDE.md").read_text()
    if "## Autopilot" not in text:
        res.add("FAIL", "CLAUDE.md has no `## Autopilot` section — run: ap2 init")
    else:
        res.add("OK", "project skeleton in place (TASKS.md, progress.md, autopilot config)")
    return res


def _sandbox_clone_path(project_root: Path, user: str) -> Path | None:
    """Where the sandbox user's clone of this project SHOULD live."""
    home = _user_home(user)
    if home is None:
        return None
    return home / "repos" / project_root.resolve().name


def _verify_gate_state() -> AuditResult:
    """Report whether AP2_VERIFY_CMD is configured (project-wide regression gate).

    The gate is opt-in — unset is the documented default and not a problem;
    an INFO line just tells the operator how to enable it. When set, OK with
    the resolved command + timeout so the human can verify what the daemon
    will actually run.
    """
    res = AuditResult()
    cmd = os.environ.get("AP2_VERIFY_CMD", "").strip()
    timeout = int(os.environ.get("AP2_VERIFY_TIMEOUT_S", DEFAULT_VERIFY_TIMEOUT_S))
    if not cmd:
        res.add(
            "INFO",
            "AP2_VERIFY_CMD unset — project-wide verify gate disabled. "
            "To enable, add e.g. `AP2_VERIFY_CMD=uv run pytest -q` to "
            ".cc-autopilot/env.",
        )
    else:
        res.add("OK", f"AP2_VERIFY_CMD: {cmd!r} (timeout {timeout}s)")
    return res


def diagnose(project_root: Path, user: str = DEFAULT_USER) -> DoctorReport:
    report = DoctorReport()

    report.sections.append(("project skeleton", _project_init_state(project_root)))
    report.sections.append(("verify gate", _verify_gate_state()))
    report.sections.append((f"sandbox user ({user})", user_audit(user)))
    report.sections.append((f"ap2 CLI for {user}", _ap2_installed_for_user(user)))

    sb_path = _sandbox_clone_path(project_root, user)
    if sb_path is None:
        miss = AuditResult()
        miss.add("FAIL", f"cannot resolve home for {user!r}")
        report.sections.append(("sandbox clone", miss))
    else:
        if sb_path.exists():
            report.sections.append((f"sandbox clone ({sb_path})", project_audit(sb_path, user)))
        else:
            miss = AuditResult()
            miss.add(
                "INFO",
                f"sandbox clone not found at {sb_path} — run: "
                f"ap2 sandbox project-setup {project_root}",
            )
            report.sections.append(("sandbox clone", miss))

    return report

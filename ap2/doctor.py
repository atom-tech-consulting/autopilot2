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
    _user_login_shell,
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
    # Probe via the user's actual login shell — `uv tool install` puts
    # `~/.local/bin` on PATH via `~/.zshenv` for zsh users, and a bash
    # probe wouldn't source it. See sandbox._user_login_shell for the
    # full rationale.
    shell = _user_login_shell(user)
    r = subprocess.run(
        ["sudo", "-u", user, "-i", shell, "-c", "command -v ap2 || true"],
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


def _parse_positive_int(raw: str) -> int:
    """Mirror `_per_task_token_cap` / `_window_token_cap` parse semantics
    (ap2/daemon.py:2581-2614): unset / empty / non-integer / non-positive
    → 0 (disabled). Doctor reusing the same shape avoids the failure mode
    where doctor reports OK on a value the daemon will treat as disabled.
    """
    s = (raw or "").strip()
    if not s:
        return 0
    try:
        v = int(s)
    except ValueError:
        return 0
    return v if v > 0 else 0


def _truthy(raw: str) -> bool:
    """Same shape as the daemon's `_truthy` env parse for
    `AP2_AUTO_APPROVE` (`1` / `true` / `yes`, case-insensitive)."""
    return (raw or "").strip().lower() in ("1", "true", "yes")


def auto_approve_audit() -> AuditResult:
    """Pre-flight check on `AP2_AUTO_APPROVE` + token-cap configuration.

    Goal.md L102-113 frames axis-3 cost guards (per-task cap, window cap,
    regression pauses) as the safety floor that lets auto-approve ship
    bounded blast-radius. `_per_task_token_cap` / `_window_token_cap`
    (daemon.py:2581-2614) deliberately return 0 ("disabled") on unset, so
    an operator can enable auto-approve without realizing the floor is
    OFF. This audit fail-loud surfaces that misconfiguration at pre-flight
    time. WARN, not FAIL: operator authority preserved per goal.md
    L184-186 — doctor warns, doesn't refuse to run.
    """
    res = AuditResult()
    enabled_raw = os.environ.get("AP2_AUTO_APPROVE", "")
    if not _truthy(enabled_raw):
        res.add(
            "INFO",
            "auto-approve disabled (AP2_AUTO_APPROVE unset) — "
            "manual approve required per task",
        )
        return res

    per_task = _parse_positive_int(os.environ.get("AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP", ""))
    window = _parse_positive_int(os.environ.get("AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP", ""))

    if per_task > 0:
        res.add("OK", f"AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP={per_task}")
    else:
        res.add(
            "WARN",
            "AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP unset/zero — per-task cost "
            "ceiling DISABLED. Fix: export "
            "AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP=<budget>",
        )

    if window > 0:
        res.add("OK", f"AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP={window}")
    else:
        res.add(
            "WARN",
            "AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP unset/zero — 24h rolling-"
            "window cost ceiling DISABLED. Fix: export "
            "AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP=<budget>",
        )

    if per_task == 0 and window == 0:
        res.add(
            "WARN",
            "auto-approve enabled with no cost ceiling — safety floor OFF; "
            "see goal.md L102-113 for rationale",
        )
    return res


def _parse_nonneg_int_with_default(raw: str, default: int) -> int:
    """Mirror `_auto_unfreeze_max_per_task` / `_auto_unfreeze_max_per_day`
    parse semantics (ap2/daemon.py:3084-3091 / 3109-3116): empty /
    non-integer / negative falls back to `default`; non-negative integers
    are honored (including 0, which the daemon treats as "cap disabled").
    Doctor reusing the same shape avoids the failure mode where doctor
    reports a cap value the daemon will treat differently.
    """
    s = (raw or "").strip()
    if not s:
        return default
    try:
        v = int(s)
    except ValueError:
        return default
    return v if v >= 0 else default


def auto_unfreeze_audit() -> AuditResult:
    """Pre-flight check on `AP2_AUTO_UNFREEZE_FIX_SHAPES` +
    `AP2_AUTO_UNFREEZE_DRY_RUN` configuration (TB-239, axis-2 mirror
    of `auto_approve_audit()`).

    `_maybe_auto_unfreeze` (daemon.py:3301-3303) silently early-returns
    when `AP2_AUTO_UNFREEZE_FIX_SHAPES` is unset/empty — EVEN when
    `AP2_AUTO_UNFREEZE_DRY_RUN=1` is set. An operator who flips dry-run
    expecting observation gets a silent no-op (zero
    `would_auto_unfreeze` events, zero `auto_unfreeze_skipped` events,
    no doctor warning). This audit fail-loud surfaces that
    misconfiguration at pre-flight time. WARN, not FAIL: operator
    authority preserved per goal.md L184-186 — doctor warns, doesn't
    refuse to run.

    Note on default asymmetry vs `auto_approve_audit()`: axis-1
    defaults are permissive (caps default to 0 = disabled = unbounded),
    so enabling auto-approve without caps is the loud-warn shape.
    Axis-2 defaults are conservative (allowlist defaults to empty =
    no-op; per-task cap defaults to 1; per-day cap defaults to 3), so
    the loud-warn shape here is flipping the dry-run knob without
    populating the allowlist (silent no-op).
    """
    res = AuditResult()
    allowlist_raw = os.environ.get("AP2_AUTO_UNFREEZE_FIX_SHAPES", "").strip()
    shapes = [s.strip() for s in allowlist_raw.split(",") if s.strip()]
    dry_run = _truthy(os.environ.get("AP2_AUTO_UNFREEZE_DRY_RUN", ""))
    per_task_cap = _parse_nonneg_int_with_default(
        os.environ.get("AP2_AUTO_UNFREEZE_MAX_PER_TASK", ""), 1,
    )
    per_day_cap = _parse_nonneg_int_with_default(
        os.environ.get("AP2_AUTO_UNFREEZE_MAX_PER_DAY", ""), 3,
    )

    if not shapes and not dry_run:
        # Default-off case: feature unconfigured, no operator engagement.
        res.add(
            "INFO",
            "auto-unfreeze disabled (allowlist unset) — "
            "set AP2_AUTO_UNFREEZE_FIX_SHAPES=<comma-list> to opt in",
        )
        return res

    if not shapes and dry_run:
        # The misconfiguration shape: dry-run set without allowlist.
        # `_maybe_auto_unfreeze` (daemon.py:3301-3303) early-returns
        # silently on empty allowlist BEFORE the dry-run check at
        # daemon.py:3416 — zero observable events, silent no-op.
        res.add(
            "WARN",
            "auto-unfreeze dry-run set without allowlist — silent no-op. "
            "`_maybe_auto_unfreeze` (ap2/daemon.py:3301-3303) early-"
            "returns on empty allowlist BEFORE the dry-run check, so "
            "zero `would_auto_unfreeze` events fire. Fix: set "
            "AP2_AUTO_UNFREEZE_FIX_SHAPES=<comma-list> before dry-run "
            "will emit observable decisions.",
        )
        return res

    # From here shapes is non-empty.
    n = len(shapes)
    if dry_run:
        res.add(
            "INFO",
            f"auto-unfreeze dry-run armed: {n} shapes, "
            f"per-task cap {per_task_cap}, per-day cap {per_day_cap}",
        )
    else:
        res.add(
            "INFO",
            f"auto-unfreeze live: {n} shapes, "
            f"per-task cap {per_task_cap}, per-day cap {per_day_cap}",
        )
    return res


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
    report.sections.append(("auto-approve safety floor", auto_approve_audit()))
    report.sections.append(("auto-unfreeze safety floor", auto_unfreeze_audit()))
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

"""OS-level sandbox user + per-project clone helpers.

Exposed via `python -m ap2 sandbox <user-audit|user-setup|project-setup|project-audit>`.

Destructive commands (dscl / useradd / git clone via sudo) are shown to the
user for approval before running. See plan/sandboxed-user-setup.md for the
runbook rationale.
"""
from __future__ import annotations

import os
import platform
import pwd
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_USER = "claude-agent"


# ---------------------------------------------------------------------------
# primitives

def _user_exists(user: str) -> bool:
    try:
        pwd.getpwnam(user)
        return True
    except KeyError:
        return False


def _user_home(user: str) -> Path | None:
    try:
        return Path(pwd.getpwnam(user).pw_dir)
    except KeyError:
        return None


def _path_owner(path: Path) -> str:
    return pwd.getpwuid(os.stat(path).st_uid).pw_name


def _can_read_as(user: str, repo: Path) -> bool:
    return subprocess.run(
        ["sudo", "-n", "-u", user, "git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
    ).returncode == 0


def _confirm(prompt: str, *, assume_yes: bool = False) -> bool:
    if assume_yes:
        return True
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


# ---------------------------------------------------------------------------
# audits

@dataclass
class AuditResult:
    ok: bool = True
    messages: list[tuple[str, str]] = field(default_factory=list)

    def add(self, level: str, text: str) -> None:
        self.messages.append((level, text))
        if level == "FAIL":
            self.ok = False

    def print(self) -> None:
        for lvl, txt in self.messages:
            print(f"  {lvl:5s} {txt}")


CRED_PATHS: list[tuple[str, str]] = [
    (".ssh/id_rsa", "SSH RSA key"),
    (".ssh/id_ed25519", "SSH Ed25519 key"),
    (".netrc", "netrc"),
    (".aws", "AWS creds dir"),
    (".config/gcloud", "gcloud creds dir"),
    (".docker/config.json", "docker auth"),
    (".config/gh", "gh CLI config"),
]

ENV_VARS = [
    "GH_TOKEN", "GITHUB_TOKEN",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
    "GOOGLE_APPLICATION_CREDENTIALS", "ANTHROPIC_API_KEY",
]


def user_audit(user: str = DEFAULT_USER) -> AuditResult:
    res = AuditResult()
    if not _user_exists(user):
        res.add("FAIL", f"user {user!r} does not exist")
        return res
    home = _user_home(user)
    if home is None:
        res.add("FAIL", f"cannot resolve home for {user!r}")
        return res

    for sub, desc in CRED_PATHS:
        p = home / sub
        if p.exists():
            res.add("FAIL", f"{desc} exists: {p}")
        else:
            res.add("OK", f"{desc} absent")

    for var in ENV_VARS:
        r = subprocess.run(
            ["sudo", "-u", user, "-i", "bash", "-c", f"printenv {var} 2>/dev/null || true"],
            capture_output=True, text=True,
        )
        val = r.stdout.strip()
        if val:
            res.add("FAIL", f"${var} is set in {user}'s env ({len(val)} chars)")
        else:
            res.add("OK", f"${var} unset")

    # cc-perms hook is intentionally bypassed for daemon sessions via
    # setting_sources=["project"], so no user-level venv check here.

    return res


def project_audit(path: Path, user: str = DEFAULT_USER) -> AuditResult:
    res = AuditResult()
    if not _user_exists(user):
        res.add("FAIL", f"user {user!r} does not exist")
        return res
    path = path.resolve()
    if not path.is_dir():
        res.add("FAIL", f"path does not exist: {path}")
        return res
    if not (path / ".git").is_dir():
        res.add("FAIL", f"not a git repo: {path}")
        return res

    try:
        owner = _path_owner(path)
    except KeyError:
        owner = "<unknown>"
    if owner == user:
        res.add("OK", f"owned by {user}")
    else:
        res.add("FAIL", f"owned by {owner}, expected {user}")

    r = subprocess.run(
        ["sudo", "-u", user, "git", "-C", str(path),
         "remote", "get-url", "--push", "upstream"],
        capture_output=True, text=True,
    )
    push = r.stdout.strip()
    if r.returncode != 0:
        res.add("INFO", "no 'upstream' remote configured")
    elif push == "/dev/null":
        res.add("OK", "upstream push URL disabled")
    else:
        res.add("FAIL", f"upstream push URL is live: {push}")

    r = subprocess.run(
        ["sudo", "-u", user, "git", "-C", str(path), "remote", "get-url", "local"],
        capture_output=True, text=True,
    )
    if r.returncode == 0:
        bare = r.stdout.strip()
        if subprocess.run(["sudo", "-u", user, "test", "-d", bare]).returncode == 0:
            res.add("OK", f"local bare remote: {bare}")
        else:
            res.add("FAIL", f"'local' remote points at missing path: {bare}")
    else:
        res.add("INFO", "no 'local' remote (add one to push safely)")

    if (path / ".venv").is_dir():
        res.add("OK", "project .venv present")
    else:
        res.add("INFO", f"no .venv (run 'uv sync' as {user})")

    return res


# ---------------------------------------------------------------------------
# setup

def _next_darwin_uid() -> int:
    r = subprocess.run(
        ["sudo", "-n", "dscl", ".", "-list", "/Users", "UniqueID"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return 601
    uids = []
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].isdigit():
            uids.append(int(parts[1]))
    return max(max(uids) + 1, 601) if uids else 601


def _darwin_create_commands(user: str, uid: int) -> list[list[str]]:
    path = f"/Users/{user}"
    return [
        ["sudo", "dscl", ".", "-create", path],
        ["sudo", "dscl", ".", "-create", path, "UserShell", "/bin/zsh"],
        ["sudo", "dscl", ".", "-create", path, "RealName", "Claude Autopilot Agent"],
        ["sudo", "dscl", ".", "-create", path, "UniqueID", str(uid)],
        ["sudo", "dscl", ".", "-create", path, "PrimaryGroupID", "20"],
        ["sudo", "dscl", ".", "-create", path, "NFSHomeDirectory", path],
        ["sudo", "createhomedir", "-c", "-u", user],
    ]


def _linux_create_commands(user: str) -> list[list[str]]:
    return [
        ["sudo", "useradd", "--create-home", "--shell", "/bin/bash", user],
        ["sudo", "passwd", "-l", user],
    ]


def _run_plan(title: str, cmds: list[list[str]], *, assume_yes: bool) -> int:
    print(f"# {title}")
    for c in cmds:
        print("  " + " ".join(c))
    print()
    if not _confirm("Proceed?", assume_yes=assume_yes):
        print("aborted.")
        return 1
    for c in cmds:
        print(f"+ {' '.join(c)}")
        if subprocess.run(c).returncode != 0:
            print(f"command failed: {' '.join(c)}", file=sys.stderr)
            return 1
    return 0


def user_setup(user: str = DEFAULT_USER, *, assume_yes: bool = False) -> int:
    if _user_exists(user):
        print(f"user {user!r} already exists")
        return 0

    sysname = platform.system()
    if sysname == "Darwin":
        cmds = _darwin_create_commands(user, _next_darwin_uid())
    elif sysname == "Linux":
        cmds = _linux_create_commands(user)
    else:
        print(f"unsupported OS: {sysname}", file=sys.stderr)
        return 1

    rc = _run_plan(f"Create sandbox user {user!r}:", cmds, assume_yes=assume_yes)
    if rc == 0:
        print(f"\nUser {user!r} created. Verify: python -m ap2 sandbox user-audit {user}")
    return rc


def project_setup(source: Path, user: str = DEFAULT_USER, *, assume_yes: bool = False) -> int:
    source = source.resolve()
    if not _user_exists(user):
        print(f"user {user!r} does not exist. Run: python -m ap2 sandbox user-setup {user}",
              file=sys.stderr)
        return 1
    if not (source / ".git").is_dir():
        print(f"source is not a git repo: {source}", file=sys.stderr)
        return 1

    home = _user_home(user)
    if home is None:
        print(f"cannot resolve home for {user!r}", file=sys.stderr)
        return 1

    name = source.name
    dst = home / "repos" / name
    bare = home / "repos" / f"{name}-local.git"

    if (dst / ".git").is_dir():
        print(f"clone already exists at {dst} — running project-audit instead.\n")
        return _print_audit(project_audit(dst, user))

    if not _can_read_as(user, source):
        print(f"user {user!r} cannot read {source}.")
        print(f"Fix: chmod -R g+rX {source}  (macOS: both users typically in 'staff')")
        return 1

    cmds = [
        ["sudo", "-u", user, "mkdir", "-p", str(home / "repos")],
        ["sudo", "-u", user, "git", "clone", "--origin", "upstream",
         str(source), str(dst)],
        ["sudo", "-u", user, "git", "-C", str(dst),
         "remote", "set-url", "--push", "upstream", "/dev/null"],
        ["sudo", "-u", user, "git", "init", "--bare", str(bare)],
        ["sudo", "-u", user, "git", "-C", str(dst),
         "remote", "add", "local", str(bare)],
    ]

    rc = _run_plan(
        f"Provision project {name!r} under {user!r} at {dst}:",
        cmds, assume_yes=assume_yes,
    )
    if rc != 0:
        return rc
    print()
    return _print_audit(project_audit(dst, user))


def _print_audit(res: AuditResult) -> int:
    res.print()
    print()
    if res.ok:
        print("AUDIT: clean.")
        return 0
    print("AUDIT: failures above.")
    return 1


# ---------------------------------------------------------------------------
# CLI glue (wired from ap2.cli)

def cmd_user_audit(cfg, args) -> int:        # noqa: ARG001
    return _print_audit(user_audit(args.user))


def cmd_user_setup(cfg, args) -> int:        # noqa: ARG001
    return user_setup(args.user, assume_yes=args.yes)


def cmd_project_setup(cfg, args) -> int:     # noqa: ARG001
    return project_setup(Path(args.source), args.user, assume_yes=args.yes)


def cmd_project_audit(cfg, args) -> int:     # noqa: ARG001
    return _print_audit(project_audit(Path(args.path), args.user))

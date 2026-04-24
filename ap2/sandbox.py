"""OS-level sandbox user + per-project clone helpers.

Exposed via `python -m ap2 sandbox <user-audit|user-setup|project-setup|project-audit>`.

Destructive commands (dscl / useradd / git clone via sudo) are shown to the
user for approval before running. See plan/sandboxed-user-setup.md for the
runbook rationale.
"""
from __future__ import annotations

import getpass
import grp
import os
import platform
import pwd
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_USER = "claude-agent"

# Marker lines used by install_oauth_token to make the block we own
# replaceable on re-run without touching the rest of the user's .zshenv.
_TOKEN_BEGIN = "# BEGIN ap2-managed: CLAUDE_CODE_OAUTH_TOKEN"
_TOKEN_END = "# END ap2-managed: CLAUDE_CODE_OAUTH_TOKEN"


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
    """Best-effort stat check: does `user` have r+x on `repo` and x on ancestors?

    Uses permission bits only — doesn't see macOS ACLs. If this returns False
    but the real `git clone` actually succeeds (ACLs grant access), that's a
    false alarm the caller can override. The check exists so the common case
    ("both users in staff, dir is g+rx") gives a fast, clear error before we
    start invoking sudo.
    """
    try:
        pw = pwd.getpwnam(user)
    except KeyError:
        return False
    uid = pw.pw_uid
    groups = {pw.pw_gid} | {g.gr_gid for g in grp.getgrall() if user in g.gr_mem}

    def _mode_ok(p: Path, *, need_read: bool) -> bool:
        try:
            st = os.stat(p)
        except OSError:
            return False
        m = st.st_mode
        if st.st_uid == uid:
            req = stat.S_IXUSR | (stat.S_IRUSR if need_read else 0)
        elif st.st_gid in groups:
            req = stat.S_IXGRP | (stat.S_IRGRP if need_read else 0)
        else:
            req = stat.S_IXOTH | (stat.S_IROTH if need_read else 0)
        return (m & req) == req

    p = repo.resolve()
    if not _mode_ok(p, need_read=True):
        return False
    while p != p.parent:
        p = p.parent
        if not _mode_ok(p, need_read=False):
            return False
    return True


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

    # CLAUDE_CODE_OAUTH_TOKEN is the *expected* Anthropic auth path for the
    # daemon — it sidesteps the macOS Keychain, which is locked for non-GUI
    # sessions and causes `claude` to exit-1 with empty stderr. Unset = WARN
    # on macOS (daemon will silently fail), INFO on Linux (no keychain issue).
    r = subprocess.run(
        ["sudo", "-u", user, "-i", "bash", "-c",
         "printenv CLAUDE_CODE_OAUTH_TOKEN 2>/dev/null || true"],
        capture_output=True, text=True,
    )
    if r.stdout.strip():
        res.add("OK", "$CLAUDE_CODE_OAUTH_TOKEN set (daemon uses file-backed auth)")
    elif platform.system() == "Darwin":
        res.add(
            "WARN",
            "$CLAUDE_CODE_OAUTH_TOKEN unset — `claude` subprocesses will try "
            "the Keychain and fail silently. Run: ap2 sandbox install-token",
        )
    else:
        res.add("INFO", "$CLAUDE_CODE_OAUTH_TOKEN unset (optional on Linux)")

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


def user_setup(
    user: str = DEFAULT_USER,
    *,
    assume_yes: bool = False,
    skip_token: bool = False,
) -> int:
    sysname = platform.system()
    already_existed = _user_exists(user)

    if already_existed:
        print(f"user {user!r} already exists")
    else:
        if sysname == "Darwin":
            cmds = _darwin_create_commands(user, _next_darwin_uid())
        elif sysname == "Linux":
            cmds = _linux_create_commands(user)
        else:
            print(f"unsupported OS: {sysname}", file=sys.stderr)
            return 1
        rc = _run_plan(f"Create sandbox user {user!r}:", cmds, assume_yes=assume_yes)
        if rc != 0:
            return rc
        print(f"\nUser {user!r} created.")

    # Token install (macOS really needs it; Linux accepts it for consistency).
    # Skip in --yes/--skip-token mode — scripted setup can't run the interactive
    # `claude setup-token` flow anyway, so we just print the follow-up command.
    if not skip_token and not assume_yes:
        _prompt_install_token(user)
    elif not skip_token:
        print(f"\nSkipped interactive token prompt (--yes). Install later with:")
        print(f"  ap2 sandbox install-token {user}")

    print(f"\nVerify: ap2 sandbox user-audit {user}")
    return 0


# ---------------------------------------------------------------------------
# oauth token install
#
# `claude` on macOS stores OAuth credentials in the login Keychain, which is
# locked for non-GUI sessions (sudo, launchd-without-GUI). When locked, Claude
# Code exits 1 with zero stderr — invisible to the daemon's stderr sink and
# very expensive to diagnose. The long-lived `CLAUDE_CODE_OAUTH_TOKEN` (from
# `claude setup-token`) is the official escape hatch: set it in the sandbox
# user's `.zshenv` and every `claude` subprocess the daemon spawns will pick
# it up via normal env inheritance, bypassing the Keychain entirely.

def _prompt_install_token(user: str) -> None:
    """Interactively ask the operator for an OAuth token and install it.

    Pressing ENTER skips — the user will be nudged by `user-audit` later.
    """
    print()
    print("macOS Keychain is locked for non-GUI sessions, so the daemon needs a")
    print("file-backed OAuth token to avoid silent `claude` subprocess failures.")
    print("Obtain one (in your own shell, not the sandbox user's) via:")
    print("  claude setup-token")
    print()
    try:
        token = getpass.getpass(
            f"Paste CLAUDE_CODE_OAUTH_TOKEN for {user} (or ENTER to skip): "
        ).strip()
    except (EOFError, KeyboardInterrupt):
        token = ""
    if not token:
        print(f"\nSkipped. Install later with: ap2 sandbox install-token {user}")
        return
    install_oauth_token(user, token)


def install_oauth_token(user: str, token: str) -> int:
    """Write/replace `CLAUDE_CODE_OAUTH_TOKEN` in ~<user>/.zshenv, chmod 0600.

    Idempotent: replaces the sentinel-bracketed block on re-run, leaving the
    rest of `.zshenv` untouched. Returns 0 on success.
    """
    if not _user_exists(user):
        print(f"user {user!r} does not exist", file=sys.stderr)
        return 1
    token = token.strip()
    if not token:
        print("empty token; refusing to write", file=sys.stderr)
        return 1

    home = _user_home(user)
    if home is None:
        print(f"cannot resolve home for {user!r}", file=sys.stderr)
        return 1
    zshenv = home / ".zshenv"

    # Read current contents via sudo (file may not exist yet — that's fine).
    r = subprocess.run(
        ["sudo", "-u", user, "sh", "-c",
         f"test -f {zshenv} && cat {zshenv} || true"],
        capture_output=True, text=True,
    )
    existing = r.stdout

    # Strip any previous ap2-managed block so rotating tokens doesn't accumulate.
    cleaned: list[str] = []
    skipping = False
    for line in existing.splitlines():
        if line.strip() == _TOKEN_BEGIN:
            skipping = True
            continue
        if skipping:
            if line.strip() == _TOKEN_END:
                skipping = False
            continue
        cleaned.append(line)
    prefix = "\n".join(cleaned).rstrip()
    block = f"{_TOKEN_BEGIN}\nexport CLAUDE_CODE_OAUTH_TOKEN={token}\n{_TOKEN_END}\n"
    new_contents = (prefix + "\n\n" if prefix else "") + block

    # Write via sudo tee, discarding stdout so the token never echoes.
    w = subprocess.run(
        ["sudo", "-u", user, "tee", str(zshenv)],
        input=new_contents, text=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    if w.returncode != 0:
        print(f"failed to write {zshenv}: {w.stderr}", file=sys.stderr)
        return 1

    c = subprocess.run(["sudo", "-u", user, "chmod", "600", str(zshenv)])
    if c.returncode != 0:
        print(f"chmod 600 {zshenv} failed", file=sys.stderr)
        return 1

    print(f"wrote CLAUDE_CODE_OAUTH_TOKEN to {zshenv} (mode 0600)")
    print(f"restart the daemon so the new env takes effect:")
    print(f"  sudo -u {user} -i ap2 --project <repo> stop && ap2 --project <repo> start")
    return 0


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

    # `-c safe.directory=*` is scoped to this single clone invocation — git
    # otherwise refuses to read a repo owned by a different user (the human).
    # We use `*` rather than the exact path because git matches against the
    # gitdir (`<source>/.git`), not the worktree, and the exact form is
    # awkward to get right across bare / non-bare / submodule cases.
    cmds = [
        ["sudo", "-u", user, "mkdir", "-p", str(home / "repos")],
        ["sudo", "-u", user, "git", "-c", "safe.directory=*",
         "clone", "--origin", "upstream", str(source), str(dst)],
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
    return user_setup(
        args.user,
        assume_yes=args.yes,
        skip_token=getattr(args, "skip_token", False),
    )


def cmd_project_setup(cfg, args) -> int:     # noqa: ARG001
    return project_setup(Path(args.source), args.user, assume_yes=args.yes)


def cmd_project_audit(cfg, args) -> int:     # noqa: ARG001
    return _print_audit(project_audit(Path(args.path), args.user))


def cmd_install_token(cfg, args) -> int:     # noqa: ARG001
    token = _resolve_token_arg(args)
    if token is None:
        return 1
    return install_oauth_token(args.user, token)


def _resolve_token_arg(args) -> str | None:
    """Source the token from --token-env, stdin, or interactive prompt."""
    if getattr(args, "token_env", None):
        val = os.environ.get(args.token_env)
        if not val:
            print(f"env var {args.token_env} is empty/unset", file=sys.stderr)
            return None
        return val.strip()
    if not sys.stdin.isatty():
        data = sys.stdin.read().strip()
        if not data:
            print("empty stdin", file=sys.stderr)
            return None
        return data
    try:
        return getpass.getpass(
            f"Paste CLAUDE_CODE_OAUTH_TOKEN for {args.user}: "
        ).strip() or None
    except (EOFError, KeyboardInterrupt):
        return None

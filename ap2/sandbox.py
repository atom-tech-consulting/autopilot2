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

# Labels used by the sentinel-block writer. One label per "topic" (OAuth token,
# MM credentials, MM channel) so re-running any single flow replaces just its
# block without touching others.
_LBL_OAUTH = "CLAUDE_CODE_OAUTH_TOKEN"
_LBL_MM_CREDS = "mattermost-credentials"
_LBL_MM_CHANNEL = "mattermost-channel"


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
    skip_statusline: bool = False,
    mm_url: str | None = None,
    mm_token: str | None = None,
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

    # Statusline (TB-78) — copy the project's statusline script into the
    # sandbox user's ~/.claude/ and wire it into their settings.json. Quiet
    # by default; --skip-statusline opts out (e.g. for users that don't run
    # `claude` interactively).
    if not skip_statusline:
        print()
        install_statusline(user)
    else:
        print(f"\nSkipped statusline install (--skip-statusline). Run later with:")
        print(f"  ap2 sandbox install-statusline {user}")

    # Mattermost credentials — only install if both supplied; otherwise print
    # the follow-up command. No interactive prompt: the `MATTERMOST_URL`/
    # `_TOKEN` are usually already in the human's shell, so an explicit flag
    # (or `install-mm` later) is the right UX — not another password prompt.
    if mm_url and mm_token:
        print()
        install_mm_credentials(user, mm_url, mm_token)
    else:
        print(f"\nMattermost credentials not installed. To install later:")
        print(f"  ap2 sandbox install-mm {user}")

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


def _replace_sentinel_block(existing: str, label: str, body: str) -> str:
    """Return `existing` with the `# BEGIN/END ap2-managed: <label>` block
    replaced by `body` (no trailing newline on body). If the block is absent,
    the new block is appended.

    Pure string op — no I/O — so it's unit-testable without sudo.
    """
    begin = f"# BEGIN ap2-managed: {label}"
    end = f"# END ap2-managed: {label}"
    cleaned: list[str] = []
    skipping = False
    for line in existing.splitlines():
        if line.strip() == begin:
            skipping = True
            continue
        if skipping:
            if line.strip() == end:
                skipping = False
            continue
        cleaned.append(line)
    prefix = "\n".join(cleaned).rstrip()
    block = f"{begin}\n{body}\n{end}\n"
    return (prefix + "\n\n" if prefix else "") + block


def _write_sentinel_block(
    file_path: Path,
    user: str,
    label: str,
    body: str,
) -> int:
    """Atomically replace the `label` block in `file_path`, writing via sudo
    as `user` so the file is owned by the sandbox user. chmod 600.
    """
    if not _user_exists(user):
        print(f"user {user!r} does not exist", file=sys.stderr)
        return 1
    # Read via sudo (file may not exist — that's fine).
    r = subprocess.run(
        ["sudo", "-u", user, "sh", "-c",
         f"test -f {file_path} && cat {file_path} || true"],
        capture_output=True, text=True,
    )
    new_contents = _replace_sentinel_block(r.stdout, label, body)
    # Ensure parent dir exists as the target user.
    subprocess.run(
        ["sudo", "-u", user, "mkdir", "-p", str(file_path.parent)],
        check=False,
    )
    # Write via sudo tee, discarding stdout so secrets never echo.
    w = subprocess.run(
        ["sudo", "-u", user, "tee", str(file_path)],
        input=new_contents, text=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    if w.returncode != 0:
        print(f"failed to write {file_path}: {w.stderr}", file=sys.stderr)
        return 1
    c = subprocess.run(["sudo", "-u", user, "chmod", "600", str(file_path)])
    if c.returncode != 0:
        print(f"chmod 600 {file_path} failed", file=sys.stderr)
        return 1
    return 0


def install_oauth_token(user: str, token: str) -> int:
    """Write/replace `CLAUDE_CODE_OAUTH_TOKEN` in ~<user>/.zshenv, chmod 0600."""
    token = token.strip()
    if not token:
        print("empty token; refusing to write", file=sys.stderr)
        return 1
    home = _user_home(user)
    if home is None:
        print(f"cannot resolve home for {user!r}", file=sys.stderr)
        return 1
    zshenv = home / ".zshenv"
    rc = _write_sentinel_block(
        zshenv, user, _LBL_OAUTH,
        f"export CLAUDE_CODE_OAUTH_TOKEN={token}",
    )
    if rc != 0:
        return rc
    print(f"wrote CLAUDE_CODE_OAUTH_TOKEN to {zshenv} (mode 0600)")
    print("restart the daemon so the new env takes effect:")
    print(f"  sudo -u {user} -i ap2 --project <repo> stop && ap2 --project <repo> start")
    return 0


def _statusline_source() -> Path:
    """Path to the canonical statusline script in this repo (TB-78).

    `__file__` is `ap2/sandbox.py`; parent.parent is the repo root, where
    `hooks/statusline-command.sh` lives.
    """
    return Path(__file__).resolve().parent.parent / "hooks" / "statusline-command.sh"


def install_statusline(user: str) -> int:
    """Install the project's statusline into ~<user>/.claude/ (TB-78).

    Two writes, both as the target user:
      1. Copy `hooks/statusline-command.sh` to `~<user>/.claude/statusline-command.sh`
         (mode 0755).
      2. Merge `statusLine: {type: command, command: "bash <abspath>"}` into
         `~<user>/.claude/settings.json`, preserving every other key.

    Idempotent: a re-run on a correctly-configured user is a no-op write
    (still chmods/owns to be safe). Returns 0 on success.
    """
    import json

    if not _user_exists(user):
        print(f"user {user!r} does not exist", file=sys.stderr)
        return 1
    home = _user_home(user)
    if home is None:
        print(f"cannot resolve home for {user!r}", file=sys.stderr)
        return 1

    src = _statusline_source()
    if not src.exists():
        print(f"statusline source missing: {src}", file=sys.stderr)
        return 1
    src_content = src.read_text()

    claude_dir = home / ".claude"
    target_script = claude_dir / "statusline-command.sh"
    target_settings = claude_dir / "settings.json"

    # Ensure ~<user>/.claude exists, owned by user.
    subprocess.run(
        ["sudo", "-u", user, "mkdir", "-p", str(claude_dir)], check=False,
    )

    # Step 1: write the script via sudo tee, chmod 0755.
    w = subprocess.run(
        ["sudo", "-u", user, "tee", str(target_script)],
        input=src_content, text=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    if w.returncode != 0:
        print(f"failed to write {target_script}: {w.stderr}", file=sys.stderr)
        return 1
    c = subprocess.run(
        ["sudo", "-u", user, "chmod", "0755", str(target_script)],
    )
    if c.returncode != 0:
        print(f"chmod 0755 {target_script} failed", file=sys.stderr)
        return 1

    # Step 2: merge `statusLine` into settings.json without touching other keys.
    r = subprocess.run(
        ["sudo", "-u", user, "sh", "-c",
         f"test -f {target_settings} && cat {target_settings} || echo '{{}}'"],
        capture_output=True, text=True,
    )
    try:
        settings = json.loads(r.stdout) if r.stdout.strip() else {}
    except json.JSONDecodeError as e:
        print(f"failed to parse {target_settings}: {e}", file=sys.stderr)
        return 1
    if not isinstance(settings, dict):
        print(f"{target_settings} is not a JSON object", file=sys.stderr)
        return 1

    desired = {
        "type": "command",
        "command": f"bash {target_script}",
    }
    if settings.get("statusLine") != desired:
        settings["statusLine"] = desired
        rendered = json.dumps(settings, indent=2) + "\n"
        w2 = subprocess.run(
            ["sudo", "-u", user, "tee", str(target_settings)],
            input=rendered, text=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        if w2.returncode != 0:
            print(f"failed to write {target_settings}: {w2.stderr}",
                  file=sys.stderr)
            return 1
        print(f"installed statusline + updated {target_settings}")
    else:
        print(f"statusline already current in {target_settings} (no-op)")
    return 0


def install_mm_credentials(user: str, url: str, token: str) -> int:
    """Write/replace `MATTERMOST_URL` + `MATTERMOST_TOKEN` in ~<user>/.zshenv."""
    url, token = url.strip().rstrip("/"), token.strip()
    if not url or not token:
        print("empty mattermost url or token; refusing to write", file=sys.stderr)
        return 1
    home = _user_home(user)
    if home is None:
        print(f"cannot resolve home for {user!r}", file=sys.stderr)
        return 1
    zshenv = home / ".zshenv"
    rc = _write_sentinel_block(
        zshenv, user, _LBL_MM_CREDS,
        f"export MATTERMOST_URL={url}\nexport MATTERMOST_TOKEN={token}",
    )
    if rc != 0:
        return rc
    print(f"wrote MATTERMOST_URL + MATTERMOST_TOKEN to {zshenv} (mode 0600)")
    return 0


# ---------------------------------------------------------------------------
# Mattermost API helpers — used to resolve channel NAME → ID at install time.
#
# Keeping them in sandbox.py (not mattermost.py) because (a) they're install-
# time only and (b) they require auth from the *human's* env, not the daemon's.


def _mm_api_get(url: str, token: str, path: str) -> dict | list:
    import json
    import urllib.error
    import urllib.request
    req = urllib.request.Request(
        url.rstrip("/") + path,
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"mattermost {path} → HTTP {e.code}") from e


def resolve_mm_channel(
    url: str, token: str, channel_name: str, team_id: str | None = None,
) -> tuple[str, str]:
    """Resolve a channel name to (channel_id, team_id).

    If `team_id` is None, uses the caller's first team from `/users/me/teams`.
    Accepts names with or without a leading `#`.
    """
    name = channel_name.lstrip("#").strip()
    if not name:
        raise ValueError("empty channel name")
    if team_id is None:
        teams = _mm_api_get(url, token, "/api/v4/users/me/teams")
        if not isinstance(teams, list) or not teams:
            raise RuntimeError("user has no teams — set AP2_MM_TEAM_ID explicitly")
        team_id = teams[0]["id"]
    ch = _mm_api_get(url, token, f"/api/v4/teams/{team_id}/channels/name/{name}")
    if not isinstance(ch, dict) or "id" not in ch:
        raise RuntimeError(f"channel {name!r} not found in team {team_id}")
    return ch["id"], team_id


def install_project_channel(
    project_root: Path,
    user: str,
    channel_id: str,
    *,
    channel_name: str | None = None,
) -> int:
    """Write AP2_MM_CHANNELS=<id> into <project>/.cc-autopilot/env.

    Idempotent sentinel block, so re-running (or switching channels) replaces
    cleanly. `channel_name` is only used for a comment line so humans reading
    the file can tell what the ID refers to.
    """
    channel_id = channel_id.strip()
    if not channel_id:
        print("empty channel id; refusing to write", file=sys.stderr)
        return 1
    env_file = project_root / ".cc-autopilot" / "env"
    note = f"# channel name: #{channel_name}\n" if channel_name else ""
    body = f"{note}AP2_MM_CHANNELS={channel_id}"
    rc = _write_sentinel_block(env_file, user, _LBL_MM_CHANNEL, body)
    if rc != 0:
        return rc
    print(f"wrote AP2_MM_CHANNELS to {env_file} (mode 0600)")
    return 0


def project_setup(
    source: Path,
    user: str = DEFAULT_USER,
    *,
    assume_yes: bool = False,
    mm_channel: str | None = None,
) -> int:
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

    # Optional mattermost channel install — resolves the name via the caller's
    # own MM creds (MATTERMOST_URL/MATTERMOST_TOKEN from the human's env), then
    # writes the resolved ID into <dst>/.cc-autopilot/env so the daemon picks
    # it up at next start.
    if mm_channel:
        print()
        rc = _install_channel_for_project(dst, user, mm_channel)
        if rc != 0:
            print("WARNING: channel install failed; project clone is otherwise OK")

    print()
    return _print_audit(project_audit(dst, user))


def _install_channel_for_project(project_root: Path, user: str, channel_name: str) -> int:
    """Resolve `channel_name` via the CALLER's MM env and install into project env."""
    url = os.environ.get("MATTERMOST_URL", "").strip().rstrip("/")
    token = os.environ.get("MATTERMOST_TOKEN", "").strip()
    if not url or not token:
        print("MATTERMOST_URL / MATTERMOST_TOKEN missing from current env; "
              "cannot resolve channel name", file=sys.stderr)
        return 1
    team_id = os.environ.get("AP2_MM_TEAM_ID") or None
    try:
        channel_id, team_id = resolve_mm_channel(url, token, channel_name, team_id)
    except (RuntimeError, ValueError) as e:
        print(f"channel resolve failed: {e}", file=sys.stderr)
        return 1
    print(f"resolved #{channel_name.lstrip('#')} → {channel_id} (team {team_id})")
    return install_project_channel(project_root, user, channel_id,
                                    channel_name=channel_name.lstrip("#"))


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
    mm_url, mm_token = _resolve_mm_url_token(args)
    return user_setup(
        args.user,
        assume_yes=args.yes,
        skip_token=getattr(args, "skip_token", False),
        skip_statusline=getattr(args, "skip_statusline", False),
        mm_url=mm_url,
        mm_token=mm_token,
    )


def cmd_install_statusline(cfg, args) -> int:  # noqa: ARG001
    return install_statusline(args.user)


def cmd_project_setup(cfg, args) -> int:     # noqa: ARG001
    return project_setup(
        Path(args.source),
        args.user,
        assume_yes=args.yes,
        mm_channel=getattr(args, "mm_channel", None),
    )


def cmd_install_mm(cfg, args) -> int:        # noqa: ARG001
    mm_url, mm_token = _resolve_mm_url_token(args)
    if not mm_url or not mm_token:
        print("MATTERMOST_URL / MATTERMOST_TOKEN not available (check --mm-url / "
              "--mm-url-env / current env)", file=sys.stderr)
        return 1
    return install_mm_credentials(args.user, mm_url, mm_token)


def cmd_install_channel(cfg, args) -> int:   # noqa: ARG001
    root = Path(args.project).resolve()
    if not (root / ".cc-autopilot").is_dir():
        print(f"not an ap2 project root: {root}", file=sys.stderr)
        return 1
    return _install_channel_for_project(root, args.user, args.channel)


def _resolve_mm_url_token(args) -> tuple[str | None, str | None]:
    """Common flag/env resolution for MM url+token.

    Priority: --mm-url/--mm-token, then --mm-url-env/--mm-token-env, then
    the caller's MATTERMOST_URL / MATTERMOST_TOKEN env vars. Returns (None,
    None) only if nothing is set.
    """
    url = getattr(args, "mm_url", None)
    token = getattr(args, "mm_token", None)
    if not url and getattr(args, "mm_url_env", None):
        url = os.environ.get(args.mm_url_env)
    if not token and getattr(args, "mm_token_env", None):
        token = os.environ.get(args.mm_token_env)
    if not url:
        url = os.environ.get("MATTERMOST_URL")
    if not token:
        token = os.environ.get("MATTERMOST_TOKEN")
    url = (url or "").strip() or None
    token = (token or "").strip() or None
    return url, token


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

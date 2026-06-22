"""OS-level sandbox user + per-project clone helpers.

Exposed via `python -m ap2 sandbox <user-audit|user-setup|project-setup|project-audit>`.

Destructive commands (dscl / useradd / git clone via sudo) are shown to the
user for approval before running. See sandboxed-user-setup.md (at the repo
root) for the runbook rationale.
"""
from __future__ import annotations

import getpass
import grp
import importlib.resources
import os
import platform
import pwd
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_USER = "claude-agent"

# TB-188 cross-ref: `TASK_AGENT_FENCED_PATHS` in `ap2.tools` lists
# `.cc-autopilot/ideation_proposals` (the per-proposal record dir) so
# the SDK rejects task-agent edits to records, mirroring the
# operator_queue.jsonl fence (TB-143). Sandbox-user clones don't need
# to pre-create the directory — the daemon writes it lazily on the
# first ideation `add_backlog` with the `review` blocker token. This
# comment is the wired-into-`TASK_AGENT_FENCED_PATHS` audit anchor the
# briefing's verification grep looks for.

# Repo-local git identity written by `project-setup` so the daemon's first
# state commit (typically the `state: cron status-report` commit on tick #1)
# succeeds. Fresh sandbox-user clones inherit no git user.name/user.email
# (global unset for claude-agent; repo-local unset on clone), and `git
# commit` fatals with "Author identity unknown" if neither is set. The
# operator can override either default via project-setup's --git-name /
# --git-email flags.
DEFAULT_GIT_NAME = "ap2 daemon"
DEFAULT_GIT_EMAIL = "ap2-daemon@localhost"

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


def _user_login_shell(user: str) -> str:
    """Return the user's login shell from passwd, or `/bin/sh` as a fallback.

    Doctor's env probes (`user_audit`, `_ap2_installed_for_user`) shell out
    via `sudo -u <user> -i <shell> -c '<cmd>'`. We must use the user's
    actual login shell (typically zsh on macOS, bash on Linux) so the probe
    sees the same environment the daemon will see when it is started from
    the user's normal shell. In particular, `~/.zshenv` — where `ap2 sandbox
    install-token` writes `CLAUDE_CODE_OAUTH_TOKEN` — is sourced by zsh on
    every invocation but ignored by bash, so a hard-coded `bash` probe
    produces a false `CLAUDE_CODE_OAUTH_TOKEN unset` WARN and a false
    `ap2 not on $PATH` FAIL when the user's real shell (zsh) sees both.
    """
    try:
        return pwd.getpwnam(user).pw_shell or "/bin/sh"
    except KeyError:
        return "/bin/sh"


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

    # Probe via the user's actual login shell (typically /bin/zsh on macOS,
    # /bin/bash on Linux). A hard-coded `bash` would miss exports installed
    # to `~/.zshenv` by `ap2 sandbox install-token` — bash login shells
    # don't source zsh's rc files, so the probe's view of the env diverges
    # from what the daemon will see when started from the user's real shell.
    shell = _user_login_shell(user)

    for var in ENV_VARS:
        r = subprocess.run(
            ["sudo", "-u", user, "-i", shell, "-c", f"printenv {var} 2>/dev/null || true"],
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
        ["sudo", "-u", user, "-i", shell, "-c",
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

    # Repo-local git identity. Without it, the daemon's first state-commit
    # tick fatals with "Author identity unknown" — fresh clones inherit no
    # name/email when the cloning user (sandbox claude-agent) has neither
    # repo-local nor global config set. project-setup writes these by
    # default; this check catches old clones that pre-date that fix.
    name = subprocess.run(
        ["sudo", "-u", user, "git", "-C", str(path), "config", "user.name"],
        capture_output=True, text=True,
    ).stdout.strip()
    email = subprocess.run(
        ["sudo", "-u", user, "git", "-C", str(path), "config", "user.email"],
        capture_output=True, text=True,
    ).stdout.strip()
    if name and email:
        res.add("OK", f"git identity: {name} <{email}>")
    else:
        missing = " + ".join(
            k for k, v in [("user.name", name), ("user.email", email)] if not v
        )
        res.add(
            "FAIL",
            f"git {missing} unset — daemon's first state commit will fatal "
            f"'Author identity unknown'. Fix: "
            f"sudo -u {user} git -C {path} config user.name '{DEFAULT_GIT_NAME}' && "
            f"sudo -u {user} git -C {path} config user.email '{DEFAULT_GIT_EMAIL}'",
        )

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

    # ap2 assets (TB-276, TB-406) — deploy `<repo>/skills/*` into the
    # sandbox user's runtime skills roots via the unified `sync-assets`
    # path so a fresh sandbox user gets the operator skill bundles in one
    # shot. (TB-105's `install_howto` step was folded in by TB-276, then
    # retired entirely by TB-406 when the operator manual became wholly
    # the skill bundles.)
    print()
    sync_assets(user, sbuser=False, apply=True)

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


def _replace_delimited_stanza(
    existing: str, begin: str, end: str, body: str,
) -> str:
    """Return `existing` with the lines between the `begin` and `end` marker
    lines (inclusive) replaced by `begin` + `body` + `end`. If the markers
    are absent, the stanza is appended after the existing content.

    Pure string op — no I/O — and idempotent: a second call with the same
    `body` reproduces its own output byte-for-byte, so repeated deploys
    converge instead of duplicating the stanza. Shared by the shell
    sentinel-block writer (`# BEGIN ap2-managed: <label>` markers in
    `.zshenv` / env files) and `sync_assets`'s markdown discovery-pointer
    management (`<!-- BEGIN ap2-managed: ... -->` markers in CLAUDE.md /
    AGENTS.md), which differ only in the marker syntax (TB-401).
    """
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
    stanza = f"{begin}\n{body}\n{end}\n"
    return (prefix + "\n\n" if prefix else "") + stanza


def _replace_sentinel_block(existing: str, label: str, body: str) -> str:
    """Return `existing` with the `# BEGIN/END ap2-managed: <label>` block
    replaced by `body` (no trailing newline on body). If the block is absent,
    the new block is appended.

    Thin wrapper over `_replace_delimited_stanza` with the shell-comment
    marker syntax — kept as a named entry point because the secrets-writing
    callers (`install_oauth_token`, `install_mm_credentials`,
    `install_project_channel`) read more clearly with a `label` than with
    raw begin/end markers. Pure string op — unit-testable without sudo.
    """
    return _replace_delimited_stanza(
        existing,
        f"# BEGIN ap2-managed: {label}",
        f"# END ap2-managed: {label}",
        body,
    )


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


def _skills_source() -> Path:
    """Path to the operator-skill bundles (`ap2-board-ops`, `ap2-task`,
    `migrate-to-ap2`, …) — the source of truth that `sync_assets` mirrors
    into `~<user>/.claude/skills/` + `~<user>/.agents/skills/`.

    The tree ships as package data under `ap2/skills/`, so it is resolved
    from the INSTALLED `ap2` package via `importlib.resources`. That works
    in BOTH install modes — after a non-editable `uv tool install` /
    `pip install` (where `ap2` lives in site-packages) and from an editable
    / dev checkout — closing the TB-422 gap where the prior
    `Path(__file__).parent.parent / "skills"` resolution only found the tree
    in a repo clone. A repo-relative fallback covers a bare source checkout
    that has not been installed at all."""
    try:
        pkg_root = Path(str(importlib.resources.files("ap2")))
        candidate = pkg_root / "skills"
        if candidate.is_dir():
            return candidate
    except (ModuleNotFoundError, TypeError, NotADirectoryError):
        pass
    return Path(__file__).resolve().parent / "skills"


def _agents_md_source() -> Path:
    """Path to the repo-root `AGENTS.md` (TB-401) — the agentskills.io /
    Codex operator reference that points a fresh agent session at the
    operator skills. Shipped with the package so re-installing the
    editable tool keeps the deployed `~/.agents/AGENTS.md` copy fresh.
    `__file__` is `ap2/sandbox.py`; `parent.parent` is the repo root."""
    return Path(__file__).resolve().parent.parent / "AGENTS.md"


# ---------------------------------------------------------------------------
# Cross-runtime asset deploy (TB-276, TB-401, TB-406)
#
# `sync_assets` mirrors `<repo>/skills/*` into BOTH runtime skills roots —
# Claude Code's `~/.claude/skills/` and the agentskills.io / Codex standard
# `~/.agents/skills/` — deploys the Codex operator reference (repo `AGENTS.md`
# → `~/.agents/AGENTS.md`), and MANAGES a discovery-pointer stanza in each
# runtime's global instructions file so a fresh session finds the deployed
# skills without a hand-edit. (TB-406 retired the former Claude-side
# quick-reference deploy target: the operator manual is now wholly the
# `skills/*` SKILL.md bundles, so there is no separate Claude-side
# reference file to deploy.)

# Markdown discovery-pointer stanza markers — HTML comments so they render
# cleanly in CLAUDE.md / AGENTS.md and never inject a stray heading. Shared
# begin/end label across both runtimes; only the body (skills root +
# reference paths) differs per runtime.
_POINTER_BEGIN = "<!-- BEGIN ap2-managed: skills-discovery -->"
_POINTER_END = "<!-- END ap2-managed: skills-discovery -->"

_CLAUDE_POINTER_BODY = (
    "## ap2 operator skills (auto-managed by `ap2 sandbox sync-assets`)\n"
    "\n"
    "ap2's operator manual ships as agentskills.io `SKILL.md` bundles,\n"
    "deployed under `~/.claude/skills/`. Read the relevant\n"
    "`~/.claude/skills/<skill>/SKILL.md` before driving the board."
)

_CODEX_POINTER_BODY = (
    "## ap2 operator skills (auto-managed by `ap2 sandbox sync-assets`)\n"
    "\n"
    "ap2's operator manual ships as agentskills.io `SKILL.md` bundles,\n"
    "deployed under `~/.agents/skills/`. Read the relevant\n"
    "`~/.agents/skills/<skill>/SKILL.md` before driving the board; the\n"
    "Codex operator reference lives at `~/.agents/AGENTS.md`."
)


def _sync_skill_tree(
    sudo_prefix: list[str],
    skill_subdirs: list[Path],
    dest_root: Path,
    *,
    apply: bool,
    label_prefix: str,
) -> int | None:
    """rsync each skill subdir into `dest_root/<name>` with `--delete` so
    renames/deletions propagate. Returns the total drift count across the
    tree, or None on a hard rsync failure (caller aborts with rc=1).

    Shared by the Claude (`~/.claude/skills/`) and Codex (`~/.agents/skills/`)
    targets so both roots get identical per-skill mirror semantics (TB-401).
    """
    if not skill_subdirs:
        print(f"  {label_prefix}/  (none under source — nothing to sync)")
        return 0
    drift = 0
    for src in skill_subdirs:
        name = src.name
        dst = dest_root / name
        # Per-skill drift summary via rsync --dry-run --itemize-changes.
        diff = subprocess.run(
            sudo_prefix + [
                "rsync", "-an", "--delete", "--itemize-changes",
                str(src) + "/", str(dst) + "/",
            ],
            capture_output=True, text=True,
        )
        # Itemize lines: ">f.....", "<f...", "cd+++++", or "*deleting".
        lines = [
            line for line in diff.stdout.splitlines()
            if line and (line[0] in "<>c" or line.startswith("*deleting"))
        ]
        sent = sum(1 for line in lines if line[0] in "<>c")
        deleted = sum(1 for line in lines if line.startswith("*deleting"))
        d = sent + deleted
        label = f"{label_prefix}/{name}"
        if d == 0:
            print(f"  {label:<28} in sync")
            continue
        drift += d
        if apply:
            r = subprocess.run(
                sudo_prefix + [
                    "rsync", "-a", "--delete",
                    str(src) + "/", str(dst) + "/",
                ],
            )
            if r.returncode != 0:
                print(f"sync-assets: rsync failed for {label}", file=sys.stderr)
                return None
            print(f"  {label:<28} synced ({sent} updated, {deleted} deleted)")
        else:
            print(
                f"  {label:<28} drift ({sent} would update, "
                f"{deleted} would delete)"
            )
    return drift


def _sync_overwrite_file(
    sudo_prefix: list[str],
    body: str,
    dest: Path,
    *,
    apply: bool,
    label: str,
) -> int | None:
    """Deploy `body` to `dest` as a single overwrite via `tee`, skipping the
    write when the destination already matches byte-for-byte. Returns 1 on
    drift (applied / would-apply), 0 if in sync, or None on a hard write
    failure. Used for the Codex operator reference file
    (`~/.agents/AGENTS.md`); parent dirs are pre-created by the caller's
    mkdir pass."""
    existing = subprocess.run(
        sudo_prefix + [
            "sh", "-c", f"test -f {dest} && cat {dest} || true",
        ],
        capture_output=True, text=True,
    ).stdout
    if existing == body:
        print(f"  {label:<28} in sync")
        return 0
    if apply:
        w = subprocess.run(
            sudo_prefix + ["tee", str(dest)],
            input=body, text=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        if w.returncode != 0:
            print(
                f"sync-assets: failed to write {dest}: {w.stderr}",
                file=sys.stderr,
            )
            return None
        print(f"  {label:<28} synced ({len(body):,} chars)")
    else:
        print(f"  {label:<28} drift ({len(body):,} chars would write)")
    return 1


def _sync_pointer_stanza(
    sudo_prefix: list[str],
    dest: Path,
    body: str,
    *,
    apply: bool,
    label: str,
) -> int | None:
    """Idempotently write/update the `skills-discovery` stanza in a runtime's
    global instructions file (`~/.claude/CLAUDE.md` or `~/.codex/AGENTS.md`).
    Reads the current file, rewrites just the delimited stanza in place
    (preserving any surrounding operator content), and writes back only on
    drift. Returns 1 on drift, 0 if already current, None on a hard write
    failure.

    Idempotent by construction: `_replace_delimited_stanza` reproduces its
    own output, so a repeated `sync-assets` converges (no duplicate
    stanza). Parent dirs are pre-created by the caller's mkdir pass."""
    existing = subprocess.run(
        sudo_prefix + [
            "sh", "-c", f"test -f {dest} && cat {dest} || true",
        ],
        capture_output=True, text=True,
    ).stdout
    desired = _replace_delimited_stanza(
        existing, _POINTER_BEGIN, _POINTER_END, body,
    )
    if existing == desired:
        print(f"  {label:<28} in sync")
        return 0
    if apply:
        w = subprocess.run(
            sudo_prefix + ["tee", str(dest)],
            input=desired, text=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        if w.returncode != 0:
            print(
                f"sync-assets: failed to write {dest}: {w.stderr}",
                file=sys.stderr,
            )
            return None
        print(f"  {label:<28} pointer updated")
    else:
        print(f"  {label:<28} drift (pointer stanza would update)")
    return 1


def sync_assets(
    user: str | None = None,
    *,
    sbuser: bool = False,
    apply: bool = False,
    dest: Path | None = None,
) -> int:
    """Deploy ap2's operator assets into BOTH runtime homes in one
    invocation (TB-276, TB-401): the Claude Code tree (`~/.claude/`) and
    the agentskills.io / Codex tree (`~/.agents/` + `~/.codex/`).

    Per runtime, `sync_assets`:
      - mirrors `<repo>/skills/*` into the runtime's skills root
        (`~/.claude/skills/` and `~/.agents/skills/`) via per-skill
        `rsync --delete` (shared `_sync_skill_tree` helper) so
        renames/deletions propagate to both roots;
      - deploys the Codex operator reference (repo `AGENTS.md` →
        `~/.agents/AGENTS.md`) — the Claude side has no separate
        reference file; its operator manual is the `skills/*` bundles
        (TB-406 retired the former Claude-side quick-reference target);
      - manages an idempotent `skills-discovery` pointer stanza in the
        runtime's global instructions file (`~/.claude/CLAUDE.md` and
        `~/.codex/AGENTS.md`) so a fresh session discovers the deployed
        skills without a hand-edit.

    Two write models:
      - `sbuser=True` (with `user=None`): write to the CURRENT user's
        `$HOME` directly, NO sudo. Use when a Claude/Codex session
        already running AS the sandbox user (which lacks sudoer
        privileges) wants to refresh its own assets.
      - `sbuser=False` (with `user=<sandbox-user>`): write to `~<user>/`
        via `sudo -u <user>`. The default operator path — the operator
        user has sudo, the sandbox user owns the target home.

    `--sbuser` and a positional `user` are mutually exclusive — sbuser
    means "current user is the target, skip sudo," so naming a
    different user there is a contradiction.

    `dest` overrides the `.claude` root (used by tests to write into a tmp
    dir bypassing the real home-dir lookup); the sibling `.agents` /
    `.codex` roots are then resolved relative to `dest.parent`, so a
    single tmp home holds every runtime target.

    Default is dry-run with a per-asset drift summary; pass `apply=True`
    to actually mutate.
    """
    if sbuser and user:
        print(
            "sync-assets: --sbuser and a positional user arg are mutually "
            "exclusive (sbuser → current user is the target, no sudo)",
            file=sys.stderr,
        )
        return 2
    if not sbuser and not user:
        print(
            "sync-assets: either --sbuser or a positional user arg is required",
            file=sys.stderr,
        )
        return 2

    # Resolve the home base + the sudo prefix.
    if sbuser:
        sudo_prefix: list[str] = []
        home_base = Path.home()
        target_label = "current user"
    else:
        if not _user_exists(user):
            print(f"sync-assets: user {user!r} does not exist", file=sys.stderr)
            return 1
        home_base = _user_home(user)
        if home_base is None:
            print(f"sync-assets: cannot resolve home for {user!r}", file=sys.stderr)
            return 1
        sudo_prefix = ["sudo", "-u", user]
        target_label = f"~{user}"

    # `dest` overrides the .claude root; the sibling .agents / .codex roots
    # resolve under its parent so a single tmp dir can hold every runtime
    # target in tests. In real deploys (`dest is None`) all roots hang off
    # the resolved home.
    if dest is not None:
        claude_dir = dest
        home_base = dest.parent
    else:
        claude_dir = home_base / ".claude"
    agents_dir = home_base / ".agents"

    skills_src = _skills_source()
    agents_md_src = _agents_md_source()
    if not skills_src.is_dir():
        print(f"sync-assets: skills source missing: {skills_src}", file=sys.stderr)
        return 1
    if not agents_md_src.is_file():
        print(
            f"sync-assets: AGENTS.md source missing: {agents_md_src}",
            file=sys.stderr,
        )
        return 1

    claude_skills_root = claude_dir / "skills"
    agents_skills_root = agents_dir / "skills"
    agents_md_dest = agents_dir / "AGENTS.md"
    claude_pointer = claude_dir / "CLAUDE.md"
    codex_pointer = home_base / ".codex" / "AGENTS.md"

    mode_label = "apply" if apply else "dry-run"
    print(f"sync-assets: {mode_label} → {target_label}")
    print(f"  skills source: {skills_src}")
    print(f"  agents source: {agents_md_src}")
    print(f"  claude dest:   {claude_dir}")
    print(f"  agents dest:   {agents_dir}")
    print()

    # On apply, ensure parent dirs exist (owned by the target user). The
    # skills-root mkdirs also create `claude_dir` / `agents_dir` (parents of
    # the AGENTS.md / CLAUDE.md targets); the `.codex` mkdir covers
    # the Codex pointer.
    if apply:
        for d in (claude_skills_root, agents_skills_root, codex_pointer.parent):
            rc = subprocess.run(sudo_prefix + ["mkdir", "-p", str(d)]).returncode
            if rc != 0:
                print(f"sync-assets: failed to mkdir {d}", file=sys.stderr)
                return 1

    overall_drift = 0
    skill_subdirs = sorted(d for d in skills_src.iterdir() if d.is_dir())

    # ----- skills: mirror into BOTH runtime roots (shared rsync helper) -----
    for dest_root, prefix in (
        (claude_skills_root, "skills"),
        (agents_skills_root, "agents-skills"),
    ):
        d = _sync_skill_tree(
            sudo_prefix, skill_subdirs, dest_root,
            apply=apply, label_prefix=prefix,
        )
        if d is None:
            return 1
        overall_drift += d

    # ----- Codex operator reference file (single overwrite) -----
    # The Claude runtime has no separate reference file — its operator
    # manual is the `skills/*` bundles (TB-406 retired the former
    # Claude-side quick-reference deploy target).
    d = _sync_overwrite_file(
        sudo_prefix, agents_md_src.read_text(), agents_md_dest,
        apply=apply, label="AGENTS.md",
    )
    if d is None:
        return 1
    overall_drift += d

    # ----- runtime discovery-pointer stanzas (idempotent) -----
    for pdest, pbody, label in (
        (claude_pointer, _CLAUDE_POINTER_BODY, "CLAUDE.md"),
        (codex_pointer, _CODEX_POINTER_BODY, "AGENTS.md (pointer)"),
    ):
        d = _sync_pointer_stanza(sudo_prefix, pdest, pbody, apply=apply, label=label)
        if d is None:
            return 1
        overall_drift += d

    print()
    if apply:
        print("sync-assets: apply complete.")
    elif overall_drift == 0:
        print("sync-assets: dry-run — all assets in sync.")
    else:
        print("sync-assets: dry-run — drift detected. Re-run with --apply to sync.")
    return 0


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
    git_name: str = DEFAULT_GIT_NAME,
    git_email: str = DEFAULT_GIT_EMAIL,
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
    # Repo-local git identity is set immediately after the clone so the
    # daemon's first state-commit tick (cron status-report) doesn't fatal
    # with "Author identity unknown". The sandbox user's global git config
    # is intentionally bare (cred-clean), and a fresh clone has no
    # repo-local user.name/user.email — without this, the very first
    # commit blows up before any task can run.
    cmds = [
        ["sudo", "-u", user, "mkdir", "-p", str(home / "repos")],
        ["sudo", "-u", user, "git", "-c", "safe.directory=*",
         "clone", "--origin", "upstream", str(source), str(dst)],
        ["sudo", "-u", user, "git", "-C", str(dst),
         "remote", "set-url", "--push", "upstream", "/dev/null"],
        ["sudo", "-u", user, "git", "-C", str(dst),
         "config", "user.name", git_name],
        ["sudo", "-u", user, "git", "-C", str(dst),
         "config", "user.email", git_email],
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
        git_name=getattr(args, "git_name", None) or DEFAULT_GIT_NAME,
        git_email=getattr(args, "git_email", None) or DEFAULT_GIT_EMAIL,
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


def cmd_sync_assets(cfg, args) -> int:        # noqa: ARG001
    """`ap2 sandbox sync-assets [USER] [--sbuser] [--apply] [--dest DIR]` (TB-276).

    Deploys `<repo>/skills/*` into both runtime skills roots (Claude
    `~/.claude/skills/` and Codex `~/.agents/skills/`) plus the Codex
    `AGENTS.md` reference in one invocation — the unified replacement for
    the old `sync-skills` + `install-howto` verbs (TB-406 dropped the
    Claude-side quick-reference deploy: the operator manual is wholly the
    skill bundles).

    Modes:
      - default: positional `USER` selects the sandbox user; writes
        via `sudo -u <user>` into `~user/.claude/`.
      - `--sbuser`: writes into the CURRENT user's `$HOME/.claude/`
        directly, no sudo (for sandbox-user Claude sessions that lack
        sudoer privileges).
    """
    dest = Path(args.dest) if getattr(args, "dest", None) else None
    sbuser = bool(getattr(args, "sbuser", False))
    # When --sbuser is set, ignore any positional `user` (argparse leaves
    # it as the DEFAULT_USER default); sync_assets() enforces the
    # mutual-exclusion at the function boundary.
    user = None if sbuser else getattr(args, "user", DEFAULT_USER)
    return sync_assets(
        user, sbuser=sbuser, apply=bool(args.apply), dest=dest,
    )


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

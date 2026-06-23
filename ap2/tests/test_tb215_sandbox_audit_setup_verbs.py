"""TB-215: happy + error path coverage for the four sandbox audit/setup CLI
verbs that TB-209's `test_coverage_drift.py` docstring (prior to this
change, L408-411 of the comment-block shim) tags as TB-205-shape coverage
debt — the final four of the twelve CLI-verb names TB-209 enumerated.

The four verbs — `ap2 sandbox project-audit`, `ap2 sandbox project-setup`,
`ap2 sandbox user-audit`, `ap2 sandbox user-setup` — are the pre-onboarding
readiness-check surface: the two `*-audit` verbs return PASS/WARN/FAIL
diagnostics about a sandbox user or per-project clone, and the two
`*-setup` verbs are the corresponding fix paths. Prior to TB-215, none of
the four CLI handlers (`sandbox.cmd_user_audit`, `sandbox.cmd_user_setup`,
`sandbox.cmd_project_setup`, `sandbox.cmd_project_audit`) had a single
test reference under `ap2/tests/` — only the substring drift gate's
comment-block enumeration kept the gate green. A future refactor of the
handlers (e.g. dropping the `_user_exists` precheck in `user_audit`,
flipping the rc=1 short-circuit in `project_setup`, changing the
`_print_audit` exit-code convention) could silently break operator
onboarding while the drift gate stays green via the shim.

This module mirrors TB-205's `test_env_knobs.py` / TB-210's
`test_tb210_env_knobs.py` / TB-213's `test_tb213_daemon_lifecycle_verbs.py`
/ TB-214's `test_tb214_sandbox_install_verbs.py` shape on the CLI-verb
axis: per-verb happy-path test + at-least-one error-path test, calls go
through the public CLI handler (`sandbox.cmd_<verb>`) rather than reaching
into implementation internals, side-effects asserted on captured stdout
+ stubbed subprocess.run + the AuditResult return shape.

The existing `test_sandbox.py` module already pins the lower-level
`user_audit` / `project_audit` / `project_setup` helpers; this module adds
the missing CLI-handler layer where:

  - `cmd_user_audit` wraps `_print_audit(user_audit(args.user))` —
    the user passed via argparse, the AuditResult printed for the
    operator, and `rc=0` iff every check is OK/INFO/WARN (not FAIL).
  - `cmd_user_setup` runs `_resolve_mm_url_token(args)` and wires
    `skip_token` / `assume_yes` / `mm_url` / `mm_token`
    into `user_setup(...)`. The handler is the only place where
    `getattr(args, "skip_token", False)` resolves the optional argparse
    flag into the helper kwargs.
  - `cmd_project_setup` adapts `args.source` → `Path`, threads
    `--user`/`--yes`/`--mm-channel`/`--git-name`/`--git-email`, and
    falls back to `DEFAULT_GIT_NAME`/`DEFAULT_GIT_EMAIL` when either
    git-* arg is None — a path the helper-level tests don't cover
    because they construct the kwargs directly.
  - `cmd_project_audit` wraps `_print_audit(project_audit(Path(args.path),
    args.user))`. Pure adapter, but pin the path-conversion + rc-derivation
    so a refactor that changes the exit-code policy surfaces.

Test-function names follow the convention
`test_cmd_sandbox_<verb_underscored>_<aspect>` (e.g.
`test_cmd_sandbox_project_audit_happy_path`,
`test_cmd_sandbox_user_setup_unsupported_os`). The auto-verifier bullets
in the briefing grep for
`def test_cmd_sandbox_(project_audit|project_setup|user_audit|user_setup)`
across this file + `test_sandbox.py` + `test_cli.py`; the minimum is
≥4 test functions matching that pattern (one happy-path per verb), which
this module satisfies on its own.

Removing the four matching rows from `test_coverage_drift.py`'s
discovered-at-landing comment block (`#   - ap2 sandbox project-audit` /
`project-setup` / `user-audit` / `user-setup`) is paired with this file
landing — the comment-block shim was a "test mention waiting to happen"
entry, redundant once a real test references the verb name. With TB-213
(daemon-lifecycle) + TB-214 (install-*) + TB-215 (audit/setup) all
landed, the entire 12-row CLI-verb section of the comment block is now
empty; a final cleanness-axis pass may remove the now-empty section
header.

CLI-verb substring pins (one per verb, kept here so the drift gate's
`name in blob` resolves against THIS module rather than the deleted
comment-block shim):

    "ap2 sandbox project-audit"
    "ap2 sandbox project-setup"
    "ap2 sandbox user-audit"
    "ap2 sandbox user-setup"
"""
from __future__ import annotations

import subprocess
from argparse import Namespace
from pathlib import Path

import pytest

from ap2 import sandbox
from ap2.cli import main as cli_main


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _stub_run_clean(monkeypatch):
    """All subprocess.run calls succeed silently with empty stdout/stderr.

    Used by audit happy-path tests where every probe (`printenv FOO`,
    `git config user.name`, etc.) should look "clean" (rc=0 + empty
    stdout for printenv, rc=1 + empty for missing git remotes, etc.)
    — except we override return code per call site below where needed.
    """
    captures: list[list[str]] = []

    def fake_run(argv, *a, **kw):
        captures.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return captures


# ===========================================================================
# (1) `ap2 sandbox user-audit` — `sandbox.cmd_user_audit`
#
# Handler at `ap2/sandbox.py`:
#     def cmd_user_audit(cfg, args) -> int:        # noqa: ARG001
#         return _print_audit(user_audit(args.user))
#
# Pure adapter: argparse `user` → `user_audit(user)` → `_print_audit` →
# rc=0 iff AuditResult.ok else 1. We pin the happy path (clean user → rc 0,
# OK lines on stdout) AND the missing-user error branch (rc=1, "does not
# exist" in stdout via _print_audit's formatter).
# ===========================================================================


def test_cmd_sandbox_user_audit_happy_path(monkeypatch, tmp_path, capsys):
    """`cmd_user_audit(cfg, Namespace(user="claude-agent"))` against a
    clean fake home returns 0 and prints OK lines for every credential
    probe. Pin the operator-visible contract: handler returns 0, the
    AuditResult is rendered to stdout via `_print_audit`."""
    home = tmp_path / "agent-home"
    home.mkdir()
    monkeypatch.setattr(sandbox, "_user_exists", lambda u: True)
    monkeypatch.setattr(sandbox, "_user_home", lambda u: home)
    monkeypatch.setattr(sandbox, "_user_login_shell", lambda u: "/bin/zsh")
    # All subprocess.run probes return rc=0 + empty stdout → env vars unset.
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: subprocess.CompletedProcess(a[0], 0, stdout="", stderr=""),
    )

    rc = sandbox.cmd_user_audit(None, Namespace(user="claude-agent"))

    assert rc == 0
    out = capsys.readouterr().out
    # `_print_audit` renders each AuditResult message + the "AUDIT: clean."
    # footer when ok is True.
    assert "AUDIT: clean." in out
    # Every CRED_PATHS entry rendered as an OK line.
    assert "SSH RSA key absent" in out
    assert "GH_TOKEN unset" in out


def test_cmd_sandbox_user_audit_missing_user(monkeypatch, capsys):
    """Error path: `cmd_user_audit` against an unknown user returns 1
    and `_print_audit` prints "AUDIT: failures above." with a `FAIL`
    line naming the user. Pin so a refactor that drops the `_user_exists`
    precheck (or the `_print_audit` exit-code convention) surfaces."""
    monkeypatch.setattr(sandbox, "_user_exists", lambda u: False)

    rc = sandbox.cmd_user_audit(None, Namespace(user="ghost"))

    assert rc == 1
    out = capsys.readouterr().out
    assert "ghost" in out
    assert "does not exist" in out
    assert "AUDIT: failures above." in out


def test_cmd_sandbox_user_audit_flags_credential_env(monkeypatch, tmp_path, capsys):
    """Error path: a printenv probe that returns a non-empty value → rc 1
    with a FAIL line naming the env var. Pin so a refactor that flips the
    printenv-empty-is-FAIL polarity surfaces."""
    home = tmp_path / "agent-home"
    home.mkdir()
    monkeypatch.setattr(sandbox, "_user_exists", lambda u: True)
    monkeypatch.setattr(sandbox, "_user_home", lambda u: home)
    monkeypatch.setattr(sandbox, "_user_login_shell", lambda u: "/bin/zsh")

    def fake_run(argv, *a, **kw):
        joined = " ".join(argv)
        # GH_TOKEN is "set" — every other probe is clean.
        if "printenv GH_TOKEN" in joined:
            return subprocess.CompletedProcess(
                argv, 0, stdout="ghp_secret_value\n", stderr="",
            )
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    rc = sandbox.cmd_user_audit(None, Namespace(user="claude-agent"))

    assert rc == 1
    out = capsys.readouterr().out
    assert "$GH_TOKEN is set" in out
    assert "AUDIT: failures above." in out


# ===========================================================================
# (2) `ap2 sandbox user-setup` — `sandbox.cmd_user_setup`
#
# Handler at `ap2/sandbox.py`:
#     def cmd_user_setup(cfg, args) -> int:        # noqa: ARG001
#         mm_url, mm_token = _resolve_mm_url_token(args)
#         return user_setup(
#             args.user,
#             assume_yes=args.yes,
#             skip_token=getattr(args, "skip_token", False),
#             mm_url=mm_url,
#             mm_token=mm_token,
#         )
#
# The handler is the ONLY layer where `_resolve_mm_url_token` is invoked
# pre-`user_setup` and where the `getattr(args, "skip_*", False)` defaults
# resolve missing argparse flags. We pin:
#   - happy path: `user_setup` called with the right kwargs when user
#     already exists (no destructive sudo paths to fake);
#   - error path: unsupported OS short-circuits to rc=1 via `user_setup`.
# ===========================================================================


def test_cmd_sandbox_user_setup_happy_path(monkeypatch, capsys):
    """`cmd_user_setup` against an existing user returns 0 (user_setup
    short-circuits on the already-exists branch and runs the post-create
    install nudges). Pin the handler's `_resolve_mm_url_token` call +
    `getattr(args, "skip_*", False)` defaults — the helper-level
    `test_user_setup_already_exists` doesn't exercise these adapter bits."""
    monkeypatch.setattr(sandbox, "_user_exists", lambda u: True)
    # sync-assets (skills + howto, TB-276) runs unconditionally on the
    # already-exists path; stub it so we don't shell out.
    monkeypatch.setattr(
        sandbox, "sync_assets",
        lambda u=None, *, sbuser=False, apply=False, dest=None: 0,
    )

    args = Namespace(
        user="claude-agent",
        yes=True,                # assume_yes → bypass the interactive token prompt
        skip_token=True,
        # _resolve_mm_url_token reads these:
        mm_url=None,
        mm_token=None,
        mm_url_env=None,
        mm_token_env=None,
    )
    # No MM env in scope → _resolve_mm_url_token returns (None, None).
    monkeypatch.delenv("MATTERMOST_URL", raising=False)
    monkeypatch.delenv("MATTERMOST_TOKEN", raising=False)

    rc = sandbox.cmd_user_setup(None, args)

    assert rc == 0
    out = capsys.readouterr().out
    assert "already exists" in out
    # mm creds absent → MM install nudge printed (not the install confirmation).
    assert "ap2 sandbox install-mm claude-agent" in out
    # Verify nudge always appears at the end.
    assert "Verify: ap2 sandbox user-audit claude-agent" in out


def test_cmd_sandbox_user_setup_threads_mm_creds(monkeypatch, capsys):
    """`cmd_user_setup` honors --mm-url-env / --mm-token-env via
    `_resolve_mm_url_token`, then threads (url, token) into `user_setup`.
    Pin so a refactor that bypasses the resolver (e.g. reads
    args.mm_url/args.mm_token directly) drops the env-fallback path."""
    monkeypatch.setattr(sandbox, "_user_exists", lambda u: True)
    monkeypatch.setattr(
        sandbox, "sync_assets",
        lambda u=None, *, sbuser=False, apply=False, dest=None: 0,
    )
    monkeypatch.setattr(sandbox, "install_mm_credentials", lambda u, url, tok: 0)

    captured: dict[str, object] = {}
    real_user_setup = sandbox.user_setup

    def spy(user, **kw):
        captured["user"] = user
        captured.update(kw)
        return real_user_setup(user, **kw)
    monkeypatch.setattr(sandbox, "user_setup", spy)

    monkeypatch.setenv("MY_MM_URL", "https://env.example.com")
    monkeypatch.setenv("MY_MM_TOKEN", "tok-from-env")
    args = Namespace(
        user="claude-agent",
        yes=True,
        skip_token=True,
        mm_url=None,
        mm_token=None,
        mm_url_env="MY_MM_URL",
        mm_token_env="MY_MM_TOKEN",
    )

    rc = sandbox.cmd_user_setup(None, args)

    assert rc == 0
    assert captured["user"] == "claude-agent"
    assert captured["mm_url"] == "https://env.example.com"
    assert captured["mm_token"] == "tok-from-env"
    assert captured["assume_yes"] is True
    assert captured["skip_token"] is True


def test_cmd_sandbox_user_setup_unsupported_os(monkeypatch, capsys):
    """Error path: when the OS is neither Darwin nor Linux, `user_setup`
    short-circuits to rc=1 BEFORE running any sudo command. The handler
    surfaces that rc directly. Pin so a refactor that adds an OS branch
    without updating the unsupported-fallback surfaces."""
    monkeypatch.setattr(sandbox, "_user_exists", lambda u: False)
    monkeypatch.setattr(sandbox.platform, "system", lambda: "FreeBSD")

    def boom_run(argv, *a, **kw):
        raise AssertionError(f"no subprocess.run should fire: {argv}")
    monkeypatch.setattr(subprocess, "run", boom_run)

    args = Namespace(
        user="claude-agent",
        yes=True,
        skip_token=True,
        mm_url=None,
        mm_token=None,
        mm_url_env=None,
        mm_token_env=None,
    )
    rc = sandbox.cmd_user_setup(None, args)

    assert rc == 1
    assert "unsupported OS" in capsys.readouterr().err


# ===========================================================================
# (3) `ap2 sandbox project-setup` — `sandbox.cmd_project_setup`
#
# Handler at `ap2/sandbox.py`:
#     def cmd_project_setup(cfg, args) -> int:     # noqa: ARG001
#         return project_setup(
#             Path(args.source),
#             args.user,
#             assume_yes=args.yes,
#             mm_channel=getattr(args, "mm_channel", None),
#             git_name=getattr(args, "git_name", None) or DEFAULT_GIT_NAME,
#             git_email=getattr(args, "git_email", None) or DEFAULT_GIT_EMAIL,
#         )
#
# The handler is the ONLY layer where:
#   - `args.source` is widened from str → Path;
#   - `getattr(args, "git_name", None) or DEFAULT_GIT_NAME` applies the
#     fallback when the operator passes `--git-name ""` or omits the flag;
#   - `getattr(args, "mm_channel", None)` resolves the optional channel.
# Pin happy-path (project_setup invoked with correct kwargs) + error path
# (missing user → rc=1, no sudo fires).
# ===========================================================================


def test_cmd_sandbox_project_setup_happy_path(monkeypatch, tmp_path, capsys):
    """`cmd_project_setup` against a fake source repo + claude-agent user
    threads --user/--yes/--git-name/--git-email into `project_setup` and
    returns its rc. Pin the public contract: handler returns 0, the
    git_name/git_email kwargs are correctly resolved (default fallback
    when None)."""
    source = tmp_path / "stoch"
    (source / ".git").mkdir(parents=True)
    home = tmp_path / "agent-home"
    home.mkdir()

    monkeypatch.setattr(sandbox, "_user_exists", lambda u: True)
    monkeypatch.setattr(sandbox, "_user_home", lambda u: home)
    monkeypatch.setattr(sandbox, "_can_read_as", lambda u, p: True)
    monkeypatch.setattr(
        sandbox, "project_audit", lambda p, u: sandbox.AuditResult(),
    )

    captured: dict[str, object] = {}
    real_project_setup = sandbox.project_setup

    def spy(src, user, **kw):
        captured["source"] = src
        captured["user"] = user
        captured.update(kw)
        return real_project_setup(src, user, **kw)
    monkeypatch.setattr(sandbox, "project_setup", spy)

    seen: list[list[str]] = []

    def fake_run(argv, *a, **kw):
        seen.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)

    args = Namespace(
        source=str(source),
        user="claude-agent",
        yes=True,
        mm_channel=None,
        # Pass None for git_* → handler falls back to DEFAULT_*.
        git_name=None,
        git_email=None,
    )
    rc = sandbox.cmd_project_setup(None, args)

    assert rc == 0
    # Source string widened to Path.
    assert isinstance(captured["source"], Path)
    assert captured["source"] == source.resolve()
    assert captured["user"] == "claude-agent"
    # `None or DEFAULT_*` fallback applied.
    assert captured["git_name"] == sandbox.DEFAULT_GIT_NAME
    assert captured["git_email"] == sandbox.DEFAULT_GIT_EMAIL
    assert captured["assume_yes"] is True
    assert captured["mm_channel"] is None
    # `git config user.name`/`user.email` ran against the clone via sudo.
    name_cfg = next(a for a in seen if "config" in a and "user.name" in a)
    email_cfg = next(a for a in seen if "config" in a and "user.email" in a)
    assert name_cfg[-1] == sandbox.DEFAULT_GIT_NAME
    assert email_cfg[-1] == sandbox.DEFAULT_GIT_EMAIL


def test_cmd_sandbox_project_setup_honors_git_overrides(monkeypatch, tmp_path):
    """`cmd_project_setup` with non-None --git-name / --git-email overrides
    the defaults. Pin so a refactor of the `getattr(..., None) or DEFAULT`
    fallback that accidentally hard-wires the defaults surfaces."""
    source = tmp_path / "stoch"
    (source / ".git").mkdir(parents=True)
    home = tmp_path / "agent-home"
    home.mkdir()

    monkeypatch.setattr(sandbox, "_user_exists", lambda u: True)
    monkeypatch.setattr(sandbox, "_user_home", lambda u: home)
    monkeypatch.setattr(sandbox, "_can_read_as", lambda u, p: True)
    monkeypatch.setattr(
        sandbox, "project_audit", lambda p, u: sandbox.AuditResult(),
    )

    seen: list[list[str]] = []

    def fake_run(argv, *a, **kw):
        seen.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)

    args = Namespace(
        source=str(source),
        user="claude-agent",
        yes=True,
        mm_channel=None,
        git_name="Custom Operator",
        git_email="operator@example.com",
    )
    rc = sandbox.cmd_project_setup(None, args)

    assert rc == 0
    name_cfg = next(a for a in seen if "config" in a and "user.name" in a)
    email_cfg = next(a for a in seen if "config" in a and "user.email" in a)
    assert name_cfg[-1] == "Custom Operator"
    assert email_cfg[-1] == "operator@example.com"


def test_cmd_sandbox_project_setup_missing_user(monkeypatch, tmp_path, capsys):
    """Error path: `cmd_project_setup` for an unknown sandbox user → rc 1
    and stderr nudges the operator to `user-setup` BEFORE any sudo
    invocation fires. Pin so a refactor that defers the _user_exists check
    (and lets the clone proceed against a non-existent sudo target) surfaces."""
    monkeypatch.setattr(sandbox, "_user_exists", lambda u: False)

    def boom_run(argv, *a, **kw):
        raise AssertionError(f"no subprocess.run should fire: {argv}")
    monkeypatch.setattr(subprocess, "run", boom_run)

    args = Namespace(
        source=str(tmp_path / "stoch"),
        user="ghost",
        yes=True,
        mm_channel=None,
        git_name=None,
        git_email=None,
    )
    rc = sandbox.cmd_project_setup(None, args)

    assert rc == 1
    err = capsys.readouterr().err
    assert "ghost" in err
    assert "does not exist" in err
    assert "ap2 sandbox user-setup" in err


# ===========================================================================
# (4) `ap2 sandbox project-audit` — `sandbox.cmd_project_audit`
#
# Handler at `ap2/sandbox.py`:
#     def cmd_project_audit(cfg, args) -> int:     # noqa: ARG001
#         return _print_audit(project_audit(Path(args.path), args.user))
#
# Pure adapter: argparse `path` (str) → `Path(args.path)` → `project_audit`
# → `_print_audit` → rc=0 iff AuditResult.ok else 1. We pin the happy path
# (clean repo → rc 0, OK lines on stdout) AND the git-identity FAIL branch
# (rc=1, the "Author identity unknown" prose fires) — the FAIL path
# was the original TB-125 surface, and the operator-visible exit code
# depends on the AuditResult bookkeeping behaving correctly.
# ===========================================================================


def test_cmd_sandbox_project_audit_happy_path(monkeypatch, tmp_path, capsys):
    """`cmd_project_audit` against a clean repo (git identity set, no
    upstream push, owner matches) returns 0 and renders OK lines.
    Pin the public contract: handler returns 0, AuditResult printed."""
    repo = tmp_path / "stoch"
    (repo / ".git").mkdir(parents=True)

    monkeypatch.setattr(sandbox, "_user_exists", lambda u: True)
    monkeypatch.setattr(sandbox, "_path_owner", lambda p: "claude-agent")

    def fake_run(argv, *a, **kw):
        if "config" in argv and "user.name" in argv:
            return subprocess.CompletedProcess(
                argv, 0, stdout="ap2 daemon\n", stderr="",
            )
        if "config" in argv and "user.email" in argv:
            return subprocess.CompletedProcess(
                argv, 0, stdout="ap2-daemon@localhost\n", stderr="",
            )
        # No upstream remote / no local remote → rc=1, empty stdout
        # (treated as INFO, not FAIL, by project_audit).
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)

    rc = sandbox.cmd_project_audit(
        None, Namespace(path=str(repo), user="claude-agent"),
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "AUDIT: clean." in out
    assert "owned by claude-agent" in out
    assert "git identity: ap2 daemon <ap2-daemon@localhost>" in out


def test_cmd_sandbox_project_audit_missing_git_identity(
    monkeypatch, tmp_path, capsys,
):
    """Error path (TB-125 surface): a repo with no repo-local
    user.name/user.email → rc 1, the "Author identity unknown" FAIL line
    surfaces. Pin so a refactor of project_audit's identity check (or
    the handler's exit-code policy) surfaces immediately."""
    repo = tmp_path / "stoch"
    (repo / ".git").mkdir(parents=True)

    monkeypatch.setattr(sandbox, "_user_exists", lambda u: True)
    monkeypatch.setattr(sandbox, "_path_owner", lambda p: "claude-agent")
    # All probes return rc=1 + empty stdout — including `git config
    # user.name`/`user.email`, simulating a fresh clone with no identity.
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: subprocess.CompletedProcess(a[0], 1, stdout="", stderr=""),
    )

    rc = sandbox.cmd_project_audit(
        None, Namespace(path=str(repo), user="claude-agent"),
    )

    assert rc == 1
    out = capsys.readouterr().out
    assert "Author identity unknown" in out
    assert "user.name" in out and "user.email" in out
    assert "AUDIT: failures above." in out


def test_cmd_sandbox_project_audit_widens_path_arg(monkeypatch, tmp_path):
    """The handler must widen `args.path` from str → Path before calling
    `project_audit`. Pin so a refactor that drops the Path() wrapper
    (causing project_audit to crash on `.resolve()` against a str) surfaces.
    """
    captured: dict[str, object] = {}

    def spy(path, user):
        captured["path"] = path
        captured["user"] = user
        return sandbox.AuditResult()  # clean
    monkeypatch.setattr(sandbox, "project_audit", spy)

    rc = sandbox.cmd_project_audit(
        None, Namespace(path="/tmp/some/repo", user="claude-agent"),
    )

    assert rc == 0
    assert isinstance(captured["path"], Path)
    assert str(captured["path"]) == "/tmp/some/repo"
    assert captured["user"] == "claude-agent"


# ===========================================================================
# Cross-verb sanity: argv → handler dispatch via `cli.main`.
#
# The per-verb tests above invoke `sandbox.cmd_<verb>` directly with a
# hand-built Namespace. That covers the handler's behavior but bypasses
# the argparse layer — a refactor that drops `set_defaults(func=...)` or
# renames an argparse field could break the verb dispatch without
# tripping the per-verb tests. This end-to-end test pins the
# argv → parser.parse_args → args.func(cfg, args) chain for one verb
# (user-audit, the simplest signature: just `user`), so a parser-level
# regression for the audit/setup verb group surfaces here.
# ===========================================================================


def test_cli_main_dispatches_to_user_audit(monkeypatch, tmp_path):
    """`cli_main(["--project", <root>, "sandbox", "user-audit",
    "claude-agent"])` routes through `build_parser` and ends up invoking
    `sandbox.cmd_user_audit` with the user from argv."""
    from ap2.init import init_project

    project = tmp_path / "proj"
    init_project(project)

    seen: list[str] = []
    real_handler = sandbox.cmd_user_audit

    def spy(cfg, args):
        seen.append(args.user)
        return real_handler(cfg, args)
    monkeypatch.setattr(sandbox, "cmd_user_audit", spy)

    # user-audit's `_user_exists(False)` short-circuit gives a deterministic
    # rc=1 without needing to fake any subprocess.run probes.
    monkeypatch.setattr(sandbox, "_user_exists", lambda u: False)

    rc = cli_main([
        "--project", str(project),
        "sandbox", "user-audit", "claude-agent",
    ])

    assert rc == 1
    assert seen == ["claude-agent"], f"argv → handler dispatch failed: {seen}"

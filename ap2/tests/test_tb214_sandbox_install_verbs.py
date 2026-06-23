"""TB-214: happy + error path coverage for the sandbox install-* CLI
verbs that TB-209's `test_coverage_drift.py` docstring (prior to this
change, L415-418 of the comment-block shim) tags as TB-205-shape coverage
debt.

The two remaining verbs — `ap2 sandbox install-channel`,
`ap2 sandbox install-mm` — are the operator's first-touch wiring surface
on a fresh project (MM creds + MM channel). Two original verbs were
since retired: `ap2 sandbox install-howto` folded into the unified
`ap2 sandbox sync-assets` verb in TB-276 (skills + howto deploy in one
shot, with a `--sbuser` non-sudo mode; its replacement coverage lives in
`test_sync_assets.py`), and the cosmetic per-user UI helper was dropped
in TB-423. Each remaining verb is the only path to wire one of those
subsystems, and prior to TB-214 none of the CLI handlers
(`sandbox.cmd_install_channel`, `sandbox.cmd_install_mm`) had a single
test reference under `ap2/tests/` — only the substring drift gate's
comment-block enumeration kept the gate green. A future refactor of the
`cmd_install_*` handlers (e.g. swapping the `_resolve_mm_url_token`
precedence, dropping the `_user_exists` precheck, changing the
project-root validation) could silently break operator onboarding while
the drift gate stays green via the shim.

This module mirrors TB-205's `test_env_knobs.py` / TB-210's
`test_tb210_env_knobs.py` / TB-213's `test_tb213_daemon_lifecycle_verbs.py`
shape on the CLI-verb axis: per-verb default behavior + at-least-one
error-path test, calls go through the public CLI handler
(`sandbox.cmd_install_<verb>`) rather than reaching into implementation
internals, side-effects asserted on stubbed `subprocess.run` captures +
return codes + stderr.

The existing `test_sandbox.py` module already pins the lower-level
`install_mm_credentials` / `install_project_channel`
helpers; this module adds the missing CLI-handler layer where the
operator-visible verb dispatch + argparse-wired args + env-var resolution
live (e.g. `cmd_install_mm` wraps `install_mm_credentials` but also runs
`_resolve_mm_url_token` against `args.mm_url`/`args.mm_token`/the env, and
exits with rc=1 on missing creds BEFORE the helper sees a thing — a path
the helper-level tests can't cover).

  1. `ap2 sandbox install-mm` — `sandbox.cmd_install_mm`:
      runs `_resolve_mm_url_token(args)` (precedence: `--mm-url`/`--mm-token`
      → `--mm-url-env`/`--mm-token-env` → caller's MATTERMOST_URL /
      MATTERMOST_TOKEN env), then `install_mm_credentials(args.user, url,
      token)`. The handler is the ONLY layer where the env-var fallback +
      the missing-creds rc=1 short-circuit live; the lower-level helper
      already-tests-clean if you hand it both creds directly. Error path:
      missing creds → rc 1 with stderr nudge.

  2. `ap2 sandbox install-channel` — `sandbox.cmd_install_channel`:
      validates that `args.project` is an ap2 project root (has a
      `.cc-autopilot/` dir), then routes to
      `_install_channel_for_project(root, args.user, args.channel)` which
      reads MM creds from the CALLER's env (NOT the sandbox user's),
      resolves the channel name to an ID via the MM API, and writes the
      ID into `<project>/.cc-autopilot/env`. Error paths: not-an-ap2-root
      → rc 1, missing MM creds in caller env → rc 1.

Test-function names follow the convention `test_cmd_sandbox_install_<verb>_<aspect>`
(e.g. `test_cmd_sandbox_install_channel_happy_path`,
`test_cmd_sandbox_install_mm_missing_creds`). The auto-verifier bullets
in the briefing grep for `def test_cmd_sandbox_install_(channel|mm)`
across this file + `test_sandbox.py` + `test_cli.py`; the minimum is
≥2 test functions matching that pattern (one happy-path per verb), which
this module satisfies on its own.

Removing the matching rows from `test_coverage_drift.py`'s
discovered-at-landing comment block (`#   - ap2 sandbox install-channel`
/ `install-mm`) is paired with this file landing
— the comment-block shim was a "test mention waiting to happen" entry,
redundant once a real test references the verb name. The 4 sandbox
audit/setup rows (project-audit/-setup, user-audit/-setup) stay in the
shim until a sibling TB closes them.

CLI-verb substring pins (one per verb, kept here so the drift gate's
`name in blob` resolves against THIS module rather than the deleted
comment-block shim):

    "ap2 sandbox install-channel"
    "ap2 sandbox install-mm"
"""
from __future__ import annotations

import subprocess
from argparse import Namespace
from pathlib import Path

import pytest

from ap2 import sandbox
from ap2.cli import main as cli_main


# ---------------------------------------------------------------------------
# Shared fixtures: subprocess.run stubs + a minimal sandbox-user home. The
# cmd_install_* handlers all ignore the `cfg` argument (`# noqa: ARG001`),
# so the tests pass `None` for cfg UNLESS they go through
# `cli_main(["sandbox", ...])`, which constructs a real Config from
# --project. The cli_main tests use a temp project init helper that
# mirrors test_cli.py's `_project` pattern.
# ---------------------------------------------------------------------------


def _wire_user_home(monkeypatch, tmp_path: Path) -> Path:
    """Stub `_user_exists` + `_user_home` so install_* helpers see a
    fake sandbox-user home directory. Returns the home path."""
    home = tmp_path / "agent-home"
    home.mkdir()
    monkeypatch.setattr(sandbox, "_user_exists", lambda u: True)
    monkeypatch.setattr(sandbox, "_user_home", lambda u: home)
    return home


def _capture_run(monkeypatch, *, settings_text: str = "{}", tee_rc: int = 0):
    """Build a subprocess.run stub that captures every tee/chmod/mkdir/sh
    invocation and returns the requested settings.json body for any merge
    probe. Returns the capture list."""
    captures: list[tuple[tuple[str, ...], str]] = []

    def fake_run(argv, *a, **kw):
        captures.append((tuple(argv), kw.get("input", "")))
        if "tee" in argv:
            return subprocess.CompletedProcess(argv, tee_rc, stdout="", stderr="")
        if "sh" in argv and any("settings.json" in str(t) for t in argv):
            return subprocess.CompletedProcess(
                argv, 0, stdout=settings_text, stderr="",
            )
        if "sh" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return captures


# ===========================================================================
# (1) `ap2 sandbox install-mm` — `sandbox.cmd_install_mm`
#
# Handler at `ap2/sandbox.py`:
#     def cmd_install_mm(cfg, args) -> int:        # noqa: ARG001
#         mm_url, mm_token = _resolve_mm_url_token(args)
#         if not mm_url or not mm_token:
#             print("MATTERMOST_URL / MATTERMOST_TOKEN not available ...",
#                   file=sys.stderr)
#             return 1
#         return install_mm_credentials(args.user, mm_url, mm_token)
#
# `_resolve_mm_url_token` precedence: --mm-url/--mm-token, then
# --mm-url-env/--mm-token-env, then caller's MATTERMOST_URL /
# MATTERMOST_TOKEN. The handler is the ONLY layer where the
# env-var-fallback + missing-creds-rc=1 short-circuit live; the
# lower-level helper test_install_mm_credentials_* in test_sandbox.py
# already covers the helper's sentinel-block writing and empty-string
# refusal (different error: empty url/token at helper boundary).
# ===========================================================================


def test_cmd_sandbox_install_mm_happy_path(monkeypatch, tmp_path, capsys):
    """`cmd_install_mm` with --mm-url + --mm-token resolves the creds
    and dispatches to `install_mm_credentials`. Pin the public contract:
    handler returns 0, tee writes both env vars into ~user/.zshenv."""
    home = _wire_user_home(monkeypatch, tmp_path)
    captures = _capture_run(monkeypatch)
    args = Namespace(
        user="claude-agent",
        mm_url="https://mm.example.com/",
        mm_token="tok-xyz",
        mm_url_env=None,
        mm_token_env=None,
    )

    rc = sandbox.cmd_install_mm(None, args)

    assert rc == 0
    tee_writes = [(argv, b) for argv, b in captures if "tee" in argv]
    assert tee_writes, "expected at least one tee write to .zshenv"
    body = tee_writes[0][1]
    assert f"# BEGIN ap2-managed: {sandbox._LBL_MM_CREDS}" in body
    assert "export MATTERMOST_URL=https://mm.example.com" in body  # trailing / stripped
    assert "export MATTERMOST_TOKEN=tok-xyz" in body
    # Tee target was the right home/.zshenv.
    tee_argv = tee_writes[0][0]
    assert tee_argv[tee_argv.index("tee") + 1] == str(home / ".zshenv")


def test_cmd_sandbox_install_mm_resolves_from_env_vars(monkeypatch, tmp_path):
    """`cmd_install_mm` honors --mm-url-env / --mm-token-env, reading
    the actual values from the named env vars. Pin the precedence
    layer — a refactor that hard-wires only --mm-url/--mm-token would
    silently break the operator's preferred "creds-by-name" workflow
    (avoids creds-on-argv leaking into shell history)."""
    home = _wire_user_home(monkeypatch, tmp_path)
    monkeypatch.setenv("MY_MM_URL", "https://env.example.com")
    monkeypatch.setenv("MY_MM_TOKEN", "tok-from-env")
    captures = _capture_run(monkeypatch)
    args = Namespace(
        user="claude-agent",
        mm_url=None,
        mm_token=None,
        mm_url_env="MY_MM_URL",
        mm_token_env="MY_MM_TOKEN",
    )

    rc = sandbox.cmd_install_mm(None, args)

    assert rc == 0
    tee_writes = [(argv, b) for argv, b in captures if "tee" in argv]
    body = tee_writes[0][1]
    assert "export MATTERMOST_URL=https://env.example.com" in body
    assert "export MATTERMOST_TOKEN=tok-from-env" in body
    assert str(home)  # silence flake8


def test_cmd_sandbox_install_mm_missing_creds(monkeypatch, capsys):
    """Error path: `cmd_install_mm` with no creds via any precedence
    layer (no --mm-url, no --mm-url-env value, no MATTERMOST_URL in
    env) returns 1 and stderr nudges the operator to the three knobs.

    Pin so a refactor that fails-open (e.g. dispatches to
    install_mm_credentials with empty strings, which the helper then
    rejects with a different error) doesn't silently change the error
    surface."""
    monkeypatch.delenv("MATTERMOST_URL", raising=False)
    monkeypatch.delenv("MATTERMOST_TOKEN", raising=False)
    args = Namespace(
        user="claude-agent",
        mm_url=None,
        mm_token=None,
        mm_url_env=None,
        mm_token_env=None,
    )

    # Defensive: no subprocess.run call should fire — handler must
    # short-circuit before touching any sudo invocation.
    def fake_run(argv, *a, **kw):
        raise AssertionError(f"unexpected subprocess.run: {argv}")
    monkeypatch.setattr(subprocess, "run", fake_run)

    rc = sandbox.cmd_install_mm(None, args)

    assert rc == 1
    err = capsys.readouterr().err
    assert "MATTERMOST_URL" in err and "MATTERMOST_TOKEN" in err
    # The nudge must reference at least one of the three precedence knobs.
    assert "--mm-url" in err or "current env" in err


# ===========================================================================
# (2) `ap2 sandbox install-channel` — `sandbox.cmd_install_channel`
#
# Handler at `ap2/sandbox.py`:
#     def cmd_install_channel(cfg, args) -> int:   # noqa: ARG001
#         root = Path(args.project).resolve()
#         if not (root / ".cc-autopilot").is_dir():
#             print(f"not an ap2 project root: {root}", file=sys.stderr)
#             return 1
#         return _install_channel_for_project(root, args.user, args.channel)
#
# Two-stage handler: synchronous ap2-project-root validation, then route
# to `_install_channel_for_project`, which itself reads
# MATTERMOST_URL/MATTERMOST_TOKEN from the CALLER's env, resolves the
# channel name to an ID via the MM API, and writes
# AP2_MM_CHANNELS=<id> into <project>/.cc-autopilot/env. We pin BOTH
# the project-root rejection (synchronous) AND the missing-creds
# rejection (in the routed helper, before any API call fires).
# ===========================================================================


def test_cmd_sandbox_install_channel_happy_path(monkeypatch, tmp_path, capsys):
    """`cmd_install_channel` against an ap2 project root + creds in env
    resolves the channel name and writes AP2_MM_CHANNELS into
    <project>/.cc-autopilot/env. Pin the public contract: handler
    returns 0, project env file is tee-written with the resolved id."""
    project = tmp_path / "stoch"
    (project / ".cc-autopilot").mkdir(parents=True)
    monkeypatch.setenv("MATTERMOST_URL", "https://mm.example.com")
    monkeypatch.setenv("MATTERMOST_TOKEN", "tok-abc")
    monkeypatch.delenv("AP2_MM_TEAM_ID", raising=False)

    def fake_api(url, token, path):
        if path == "/api/v4/users/me/teams":
            return [{"id": "team-1", "name": "primary"}]
        if path == "/api/v4/teams/team-1/channels/name/stoch":
            return {"id": "chan-42", "name": "stoch"}
        raise AssertionError(f"unexpected MM API path: {path}")
    monkeypatch.setattr(sandbox, "_mm_api_get", fake_api)
    captures = _capture_run(monkeypatch)
    args = Namespace(project=str(project), user="claude-agent", channel="#stoch")

    rc = sandbox.cmd_install_channel(None, args)

    assert rc == 0
    tee_writes = [(argv, b) for argv, b in captures if "tee" in argv]
    assert tee_writes, "expected a tee write into <project>/.cc-autopilot/env"
    argv, body = tee_writes[0]
    assert argv[argv.index("tee") + 1] == str(project / ".cc-autopilot" / "env")
    assert "AP2_MM_CHANNELS=chan-42" in body
    assert "# channel name: #stoch" in body
    out = capsys.readouterr().out
    assert "chan-42" in out  # operator-visible resolve confirmation


def test_cmd_sandbox_install_channel_not_an_ap2_root(monkeypatch, tmp_path, capsys):
    """Error path: `cmd_install_channel` with a path that has no
    `.cc-autopilot/` dir → rc 1, "not an ap2 project root" stderr.
    Pin the synchronous validation so a refactor that defers the check
    (or drops it) doesn't accidentally write into a non-ap2 directory."""
    plain = tmp_path / "plain"
    plain.mkdir()
    args = Namespace(project=str(plain), user="claude-agent", channel="stoch")

    # No MM API or subprocess call should fire — handler must reject
    # the path before touching either.
    def boom_api(*a, **kw):
        raise AssertionError("MM API must not be called for non-ap2 root")
    monkeypatch.setattr(sandbox, "_mm_api_get", boom_api)

    def boom_run(argv, *a, **kw):
        raise AssertionError(f"unexpected subprocess.run: {argv}")
    monkeypatch.setattr(subprocess, "run", boom_run)

    rc = sandbox.cmd_install_channel(None, args)

    assert rc == 1
    err = capsys.readouterr().err
    assert "not an ap2 project root" in err
    assert str(plain.resolve()) in err  # error names the rejected path


def test_cmd_sandbox_install_channel_missing_mm_env(monkeypatch, tmp_path, capsys):
    """Error path: `cmd_install_channel` against a valid ap2 project
    but with MATTERMOST_URL/MATTERMOST_TOKEN unset in the caller's env
    → rc 1 with "MATTERMOST_URL / MATTERMOST_TOKEN missing" stderr,
    BEFORE any MM API call or tee write fires.

    Pin so a refactor that hands empty strings to `resolve_mm_channel`
    (which would then 401 or 404 with a less-clear error) doesn't
    silently change the operator-facing failure mode."""
    project = tmp_path / "stoch"
    (project / ".cc-autopilot").mkdir(parents=True)
    monkeypatch.delenv("MATTERMOST_URL", raising=False)
    monkeypatch.delenv("MATTERMOST_TOKEN", raising=False)

    def boom_api(*a, **kw):
        raise AssertionError("MM API must not be called when creds are missing")
    monkeypatch.setattr(sandbox, "_mm_api_get", boom_api)

    def boom_run(argv, *a, **kw):
        raise AssertionError(f"unexpected subprocess.run: {argv}")
    monkeypatch.setattr(subprocess, "run", boom_run)

    args = Namespace(project=str(project), user="claude-agent", channel="stoch")
    rc = sandbox.cmd_install_channel(None, args)

    assert rc == 1
    err = capsys.readouterr().err
    assert "MATTERMOST_URL" in err and "MATTERMOST_TOKEN" in err
    assert "missing" in err


# ===========================================================================
# Cross-verb sanity: argv → handler dispatch via `cli.main`.
#
# The per-verb tests above invoke `sandbox.cmd_install_<verb>` directly
# with a hand-built Namespace. That covers the handler's behavior but
# bypasses the argparse layer — a refactor that drops `set_defaults(func=...)`
# or renames an argparse field could break the verb dispatch without
# tripping the per-verb tests. This end-to-end test pins the
# argv → parser.parse_args → args.func(cfg, args) chain for one verb
# (install-mm), so a parser-level regression surfaces here. The other
# verb shares the same `args.func = sandbox.cmd_install_<verb>` wiring
# shape — if this test passes, the parser is correctly wiring the
# install-* group.
# ===========================================================================


def test_cli_main_dispatches_to_install_mm(monkeypatch, tmp_path):
    """`cli_main(["--project", <root>, "sandbox", "install-mm",
    "claude-agent", "--mm-url", ..., "--mm-token", ...])` should route
    through `build_parser` and end up invoking `sandbox.cmd_install_mm`
    with the user from argv."""
    from ap2.init import init_project

    project = tmp_path / "proj"
    init_project(project)

    home = _wire_user_home(monkeypatch, tmp_path)
    _capture_run(monkeypatch)

    seen: list[str] = []

    real_handler = sandbox.cmd_install_mm

    def spy(cfg, args):
        seen.append(args.user)
        return real_handler(cfg, args)
    monkeypatch.setattr(sandbox, "cmd_install_mm", spy)

    rc = cli_main([
        "--project", str(project),
        "sandbox", "install-mm", "claude-agent",
        "--mm-url", "https://mm.example.com", "--mm-token", "tok-xyz",
    ])

    assert rc == 0
    assert seen == ["claude-agent"], (
        f"argv → handler dispatch failed: {seen}"
    )
    assert str(home)  # silence flake8

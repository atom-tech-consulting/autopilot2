"""TB-276 regression-pin tests for `ap2 sandbox sync-assets`.

The unified verb replaces the pre-TB-276 split between
`ap2 sandbox sync-skills` (rsynced repo/skills/* to the OPERATOR's
~/.claude/skills/, no sudo) and `ap2 sandbox install-howto` (copied
ap2/howto.md into a sandbox user's ~/.claude/ via `sudo -u <user> tee`).
The split was a footgun: one Claude session couldn't deploy both
assets, the two verbs used different target-user semantics, and
operators routinely forgot one or the other after a doc/skill edit.

Coverage shape mirrors `test_tb214_sandbox_install_verbs.py` (handler-
level happy + error paths) plus an end-to-end regression-pin against a
`--dest`/tmp target for both modes — `--sbuser` (no-sudo self-write,
real subprocess) and the default sudo path (subprocess.run stubbed so
the sudo argv can be asserted without needing a real second user).

The briefing's verification grep:
    ap2 sandbox --help 2>&1 | grep -qE "sync-assets|sync-claude|sync-all"
resolves against the `sandbox sync-assets` subparser registered in
`ap2/cli.py`; this module also pins the parser shape via
`build_parser()` so a refactor that drops the verb fails here as well.

CLI-verb substring pin (kept here so the docs/coverage drift gates'
`name in blob` resolves against THIS module after TB-276 deleted
`install-howto` / `sync-skills`):

    "ap2 sandbox sync-assets"
"""
from __future__ import annotations

import subprocess
from argparse import Namespace
from pathlib import Path

import pytest

from ap2 import cli, sandbox


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SKILLS_SRC = REPO_ROOT / "skills"
HOWTO_SRC = REPO_ROOT / "ap2" / "howto.md"


# ---------------------------------------------------------------------------
# CLI surface — parser shape pin
# ---------------------------------------------------------------------------


def test_cli_sandbox_sync_assets_subcommand_registered():
    """`build_parser()` registers `sandbox sync-assets` with the
    documented flags (--sbuser / --apply / --dest) and a positional
    sandbox-user arg with a default. Pin so the briefing's `--help`
    grep target stays alive."""
    parser = cli.build_parser()
    ns = parser.parse_args([
        "sandbox", "sync-assets", "claude-agent", "--apply", "--dest", "/tmp/x",
    ])
    assert ns.user == "claude-agent"
    assert ns.apply is True
    assert ns.sbuser is False
    assert ns.dest == "/tmp/x"
    assert ns.func is sandbox.cmd_sync_assets


def test_cli_sandbox_sync_assets_dry_run_is_default():
    """Without --apply, `apply` stays False and the operator gets a
    drift summary (TB-140 dry-run-by-default ergonomic, carried over
    into the unified verb)."""
    parser = cli.build_parser()
    ns = parser.parse_args(["sandbox", "sync-assets"])
    assert ns.apply is False
    assert ns.sbuser is False
    assert ns.dest is None


def test_cli_sandbox_sync_assets_sbuser_flag_parses():
    """`--sbuser` parses as a store_true switch; the positional `user`
    falls back to the DEFAULT_USER default (the handler routes around
    it when sbuser is set)."""
    parser = cli.build_parser()
    ns = parser.parse_args(["sandbox", "sync-assets", "--sbuser", "--apply"])
    assert ns.sbuser is True
    assert ns.apply is True


def test_cli_sandbox_sync_assets_help_surface():
    """The `ap2 sandbox --help` text mentions `sync-assets` so an
    operator discovers the unified verb (the structural reason TB-276
    unified the prior `sync-skills` + `install-howto` split).

    Walks to the `sandbox` group's subparser explicitly — the top-level
    parser's `--help` omits nested subcommands."""
    import argparse as _argparse

    parser = cli.build_parser()
    sandbox_parser = None
    for action in parser._actions:
        if isinstance(action, _argparse._SubParsersAction):
            sandbox_parser = action.choices.get("sandbox")
            break
    assert sandbox_parser is not None, "sandbox subparser not registered"
    help_text = sandbox_parser.format_help()
    assert "sync-assets" in help_text
    # The help text also references the briefing's two assets so an
    # operator reading the help sees BOTH skills and howto land.
    assert "skills" in help_text
    assert "howto" in help_text


# ---------------------------------------------------------------------------
# Function-level — argument validation
# ---------------------------------------------------------------------------


def test_sync_assets_rejects_sbuser_with_explicit_user(capsys):
    """`--sbuser` + a positional user is contradictory (sbuser ⇒ current
    user IS the target); the function returns 2 with a clear stderr
    nudge BEFORE any subprocess call fires."""
    rc = sandbox.sync_assets("claude-agent", sbuser=True)
    assert rc == 2
    err = capsys.readouterr().err
    assert "mutually exclusive" in err


def test_sync_assets_rejects_neither_mode_specified(capsys):
    """Calling `sync_assets()` with neither `user` nor `sbuser=True` is
    a programming error; surface it with rc=2 + a clear hint rather
    than silently no-op'ing."""
    rc = sandbox.sync_assets(None, sbuser=False)
    assert rc == 2
    err = capsys.readouterr().err
    assert "--sbuser" in err and "user arg is required" in err


def test_sync_assets_rejects_missing_user(monkeypatch, capsys):
    """Default mode against an unknown sandbox user → rc=1 with the
    "does not exist" stderr surface, mirroring the pre-TB-276
    `install_howto` user-precheck contract."""
    monkeypatch.setattr(sandbox, "_user_exists", lambda u: False)
    rc = sandbox.sync_assets("ghost", sbuser=False)
    assert rc == 1
    assert "does not exist" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Regression-pin: --sbuser mode end-to-end (real subprocess, no sudo)
# ---------------------------------------------------------------------------


def test_sync_assets_sbuser_apply_lands_both_assets(tmp_path):
    """End-to-end `--sbuser apply` against a `--dest` tmp target writes
    BOTH `skills/*` AND `ap2-howto.md` into the target .claude/ dir
    with no sudo. This is THE regression-pin the briefing calls for:
    the unified command deploys both assets in one invocation, and the
    sandbox-user path writes directly to the current process's home
    without any sudo intermediary."""
    dest = tmp_path / "claude"
    rc = sandbox.sync_assets(sbuser=True, apply=True, dest=dest)
    assert rc == 0
    # Skills landed (per-skill rsync --delete semantics preserved).
    assert (dest / "skills" / "ap2" / "SKILL.md").is_file()
    assert (dest / "skills" / "ap2-task" / "SKILL.md").is_file()
    assert (dest / "skills" / "migrate-to-ap2" / "SKILL.md").is_file()
    # Howto landed with the source body.
    howto = dest / "ap2-howto.md"
    assert howto.is_file()
    assert howto.read_text() == HOWTO_SRC.read_text()


def test_sync_assets_sbuser_dry_run_does_not_mutate(tmp_path, capsys):
    """`--sbuser` without `apply=True` prints a per-asset drift summary
    but leaves the dest untouched. Pin the dry-run-by-default
    ergonomic the briefing calls out (carry-over from sync-skills'
    TB-140 contract)."""
    dest = tmp_path / "claude"
    rc = sandbox.sync_assets(sbuser=True, apply=False, dest=dest)
    assert rc == 0
    # No mutations under dest.
    assert not (dest / "skills" / "ap2" / "SKILL.md").exists()
    assert not (dest / "ap2-howto.md").exists()
    out = capsys.readouterr().out
    assert "dry-run" in out
    # Per-asset summary mentions both classes.
    assert "skills/ap2" in out
    assert "ap2-howto.md" in out


def test_sync_assets_sbuser_apply_is_idempotent(tmp_path, capsys):
    """Two consecutive `--sbuser apply` runs leave dest matching the
    source; the second run reports 'in sync' for every asset. Pin so a
    refactor that drops the rsync --delete or the howto idempotency
    surfaces here."""
    dest = tmp_path / "claude"
    assert sandbox.sync_assets(sbuser=True, apply=True, dest=dest) == 0
    capsys.readouterr()  # drain first-run output
    assert sandbox.sync_assets(sbuser=True, apply=False, dest=dest) == 0
    out = capsys.readouterr().out
    assert "all assets in sync" in out
    assert "drift" not in out


def test_sync_assets_sbuser_apply_propagates_skill_deletions(tmp_path):
    """A stale file in `dest/skills/<name>/` (no source counterpart) is
    removed on the next `--sbuser apply`. Pin the per-skill rsync
    `--delete` semantics the briefing explicitly calls 'still
    propagate' as out-of-scope-to-change."""
    dest = tmp_path / "claude"
    # First apply.
    sandbox.sync_assets(sbuser=True, apply=True, dest=dest)
    # Plant a stale file under skills/ap2/ that has no source-side counterpart.
    stale = dest / "skills" / "ap2" / "stale-leftover.md"
    stale.write_text("old file from a renamed skill\n")
    assert stale.exists()
    # Re-apply — the stale file must be deleted.
    sandbox.sync_assets(sbuser=True, apply=True, dest=dest)
    assert not stale.exists()


# ---------------------------------------------------------------------------
# Regression-pin: default sudo mode (subprocess stubs)
#
# We can't run a real `sudo -u <other-user>` from the test harness, so we
# stub `subprocess.run` to capture every argv the function would issue
# and assert the sudo prefix is correct for BOTH the skills rsync AND
# the howto tee. The point of these tests is to pin the cross-user
# write model (sudo prefix + the right argv), not the actual write —
# the --sbuser tests above cover end-to-end side-effects.
# ---------------------------------------------------------------------------


def _wire_user_home(monkeypatch, tmp_path: Path) -> Path:
    """Stub `_user_exists` + `_user_home` so the function sees a fake
    sandbox-user home (mirrors the pattern in
    `test_tb214_sandbox_install_verbs.py`)."""
    home = tmp_path / "agent-home"
    home.mkdir()
    monkeypatch.setattr(sandbox, "_user_exists", lambda u: True)
    monkeypatch.setattr(sandbox, "_user_home", lambda u: home)
    return home


def test_sync_assets_default_mode_issues_sudo_prefix(monkeypatch, tmp_path, capsys):
    """Default mode (positional user, no --sbuser): every subprocess
    call to mkdir / rsync / tee is prefixed with `sudo -u <user>` so
    the write lands as the sandbox user. Pin so a refactor that drops
    the sudo prefix silently breaks the cross-user semantics — the
    operator user can't write directly into ~claude-agent/.claude/
    without it."""
    _wire_user_home(monkeypatch, tmp_path)
    dest = tmp_path / "claude"

    captures: list[tuple[tuple[str, ...], str]] = []

    def fake_run(argv, *a, **kw):
        captures.append((tuple(argv), kw.get("input", "") or ""))
        # rsync dry-run (-an --itemize-changes) needs to return something
        # so the function reports drift and proceeds to apply.
        if "rsync" in argv and "-an" in argv:
            return subprocess.CompletedProcess(
                argv, 0, stdout=">f+++++++++ SKILL.md\n", stderr="",
            )
        # The howto drift probe (sh -c 'test -f ... && cat ... || true')
        # returns empty stdout, signalling the howto is absent in dest.
        if "sh" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)

    rc = sandbox.sync_assets(
        "claude-agent", sbuser=False, apply=True, dest=dest,
    )
    assert rc == 0

    # Every subprocess call should carry the sudo prefix.
    for argv, _body in captures:
        assert argv[:3] == ("sudo", "-u", "claude-agent"), (
            f"non-sudo call leaked through default mode: {argv}"
        )

    # At least one rsync apply (real, not dry-run) was issued per skill,
    # and at least one tee write to the howto target.
    rsync_apply_calls = [
        argv for argv, _ in captures
        if "rsync" in argv and "-a" in argv and "-an" not in argv
    ]
    assert rsync_apply_calls, "expected at least one `rsync -a --delete` apply"

    tee_calls = [(argv, body) for argv, body in captures if "tee" in argv]
    assert tee_calls, "expected a tee write for ap2-howto.md"
    tee_argv, tee_body = tee_calls[0]
    assert tee_argv[tee_argv.index("tee") + 1] == str(dest / "ap2-howto.md")
    assert tee_body == HOWTO_SRC.read_text()


def test_sync_assets_default_mode_targets_users_claude_dir(monkeypatch, tmp_path):
    """When no `dest` override is passed, default mode resolves the
    target to `~<user>/.claude/` via `_user_home(user)`. Pin so a
    refactor that hard-codes a path or skips the home-dir lookup
    surfaces (would break cross-user deploys against non-default
    homes)."""
    home = _wire_user_home(monkeypatch, tmp_path)
    captures: list[tuple[str, ...]] = []

    def fake_run(argv, *a, **kw):
        captures.append(tuple(argv))
        if "rsync" in argv and "-an" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if "sh" in argv:
            # Pretend the howto already matches so we skip the tee write.
            return subprocess.CompletedProcess(
                argv, 0, stdout=HOWTO_SRC.read_text(), stderr="",
            )
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)

    rc = sandbox.sync_assets("claude-agent", sbuser=False, apply=False)
    assert rc == 0

    # The expected target paths all live under <home>/.claude/.
    target_prefix = str(home / ".claude")
    rsync_calls = [argv for argv in captures if "rsync" in argv]
    assert rsync_calls, "expected rsync calls"
    for argv in rsync_calls:
        # Last positional in each rsync call is the destination dir with
        # a trailing slash.
        dst_arg = argv[-1]
        assert dst_arg.startswith(target_prefix), (
            f"rsync dest didn't resolve under ~user/.claude/: {dst_arg}"
        )


def test_sync_assets_default_mode_skips_unchanged_howto(monkeypatch, tmp_path):
    """If the destination howto matches the source byte-for-byte, the
    function skips the tee write (idempotency). Pin so a refactor that
    drops the drift comparison and always re-writes surfaces here —
    that'd be a regression both for performance (unnecessary sudo
    writes on a no-op refresh) and for `ap2-howto.md`'s mtime
    stability."""
    _wire_user_home(monkeypatch, tmp_path)
    captures: list[tuple[str, ...]] = []

    def fake_run(argv, *a, **kw):
        captures.append(tuple(argv))
        if "rsync" in argv and "-an" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if "sh" in argv:
            # Existing howto matches source.
            return subprocess.CompletedProcess(
                argv, 0, stdout=HOWTO_SRC.read_text(), stderr="",
            )
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)

    rc = sandbox.sync_assets(
        "claude-agent", sbuser=False, apply=True, dest=tmp_path / "claude",
    )
    assert rc == 0
    # No tee write should have fired — howto is already in sync.
    assert not any("tee" in argv for argv in captures), (
        "expected no tee write when howto matches source byte-for-byte"
    )


# ---------------------------------------------------------------------------
# Handler dispatch — cmd_sync_assets resolves args correctly
# ---------------------------------------------------------------------------


def test_cmd_sync_assets_routes_sbuser_namespace(monkeypatch, tmp_path):
    """`cmd_sync_assets` with `--sbuser` set ignores the positional user
    default and calls `sync_assets(None, sbuser=True, ...)`. Pin the
    `args.user → None` mapping the handler does — without it, the
    function would raise the mutual-exclusion error."""
    seen: dict[str, object] = {}

    def spy(user, *, sbuser, apply, dest):
        seen.update({"user": user, "sbuser": sbuser, "apply": apply, "dest": dest})
        return 0
    monkeypatch.setattr(sandbox, "sync_assets", spy)

    rc = sandbox.cmd_sync_assets(
        None,
        Namespace(
            user=sandbox.DEFAULT_USER,
            sbuser=True,
            apply=True,
            dest=str(tmp_path / "out"),
        ),
    )
    assert rc == 0
    assert seen["user"] is None
    assert seen["sbuser"] is True
    assert seen["apply"] is True
    assert isinstance(seen["dest"], Path)
    assert str(seen["dest"]) == str(tmp_path / "out")


def test_cmd_sync_assets_routes_default_namespace(monkeypatch):
    """`cmd_sync_assets` without `--sbuser` forwards the positional
    user verbatim into `sync_assets(user, sbuser=False, ...)`."""
    seen: dict[str, object] = {}

    def spy(user, *, sbuser, apply, dest):
        seen.update({"user": user, "sbuser": sbuser, "apply": apply, "dest": dest})
        return 0
    monkeypatch.setattr(sandbox, "sync_assets", spy)

    rc = sandbox.cmd_sync_assets(
        None,
        Namespace(user="claude-agent", sbuser=False, apply=False, dest=None),
    )
    assert rc == 0
    assert seen["user"] == "claude-agent"
    assert seen["sbuser"] is False
    assert seen["apply"] is False
    assert seen["dest"] is None

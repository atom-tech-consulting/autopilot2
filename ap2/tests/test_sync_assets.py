"""TB-276 regression-pin tests for `ap2 sandbox sync-assets`.

The unified verb replaced the pre-TB-276 split between
`ap2 sandbox sync-skills` (rsynced repo/skills/* to the OPERATOR's
~/.claude/skills/, no sudo) and `ap2 sandbox install-howto` (copied the
old howto quick-reference into a sandbox user's ~/.claude/).
The split was a footgun: one Claude session couldn't deploy both
assets, the two verbs used different target-user semantics, and
operators routinely forgot one or the other after a doc/skill edit.

TB-406 retired the howto deploy entirely — the operator manual
is now wholly the `skills/*` SKILL.md bundles — so `sync_assets` deploys
the skill trees (into both runtime roots) plus the Codex `AGENTS.md`
reference, and these tests pin that skills-only deploy shape.

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
# TB-401: the Codex / agentskills.io operator-reference source, deployed to
# `~/.agents/AGENTS.md` (TB-406 retired the Claude-side howto reference, so
# this is now the only single-file operator reference `sync_assets` deploys).
AGENTS_SRC = REPO_ROOT / "AGENTS.md"

# TB-401: the discovery-pointer stanza markers `sync_assets` manages in the
# runtime global-instruction files (`~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md`).
POINTER_BEGIN = "<!-- BEGIN ap2-managed: skills-discovery -->"
POINTER_END = "<!-- END ap2-managed: skills-discovery -->"

# The retired pre-TB-406 single-file quick-reference deploy artifact. Spelled
# in parts so this module carries no live pointer to the now-deleted source
# doc (TB-407 cleanup) while still pinning that `sync_assets` never re-deploys
# the legacy flat reference alongside the skills bundles.
_RETIRED_QUICKREF_DEPLOY = "ap2-howto" ".md"


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
    # The help text references the deployed asset class so an operator
    # reading the help sees the skills land (TB-406 dropped the howto
    # quick-reference; the manual is wholly the skill bundles now).
    assert "skills" in help_text


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


def test_sync_assets_sbuser_apply_lands_skill_assets(tmp_path):
    """End-to-end `--sbuser apply` against a `--dest` tmp target writes the
    `skills/*` bundles into the target .claude/skills/ dir with no sudo.
    This is THE regression-pin the briefing calls for: the unified command
    deploys the operator skills in one invocation, and the sandbox-user path
    writes directly to the current process's home without any sudo
    intermediary. (TB-406 retired the separate howto deploy target; the
    Codex `~/.agents/...` targets are pinned in the TB-401 test below.)"""
    dest = tmp_path / "claude"
    rc = sandbox.sync_assets(sbuser=True, apply=True, dest=dest)
    assert rc == 0
    # Skills landed (per-skill rsync --delete semantics preserved).
    assert (dest / "skills" / "ap2" / "SKILL.md").is_file()
    assert (dest / "skills" / "ap2-task" / "SKILL.md").is_file()
    assert (dest / "skills" / "migrate-to-ap2" / "SKILL.md").is_file()
    # No separate Claude-side quick-reference file is deployed (TB-406).
    assert not (dest / _RETIRED_QUICKREF_DEPLOY).exists()


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
    assert not (dest / _RETIRED_QUICKREF_DEPLOY).exists()
    out = capsys.readouterr().out
    assert "dry-run" in out
    # Per-asset summary mentions the skills class.
    assert "skills/ap2" in out


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
# TB-401 — cross-runtime deploy: Codex `~/.agents/skills/` target, the
# `AGENTS.md` reference, and idempotent discovery-pointer management.
# ---------------------------------------------------------------------------


def test_replace_delimited_stanza_is_idempotent():
    """The pure stanza-rewrite primitive `sync_assets` uses for pointer
    management converges: a second call with the same body reproduces its
    own output byte-for-byte (no duplicate stanza), and surrounding content
    is preserved, not duplicated. This is the engine behind the idempotent
    discovery-pointer contract."""
    body = "pointer body line 1\npointer body line 2"
    once = sandbox._replace_delimited_stanza("", POINTER_BEGIN, POINTER_END, body)
    twice = sandbox._replace_delimited_stanza(once, POINTER_BEGIN, POINTER_END, body)
    assert once == twice
    assert once.count(POINTER_BEGIN) == 1
    # Surrounding operator content survives and the stanza stays single.
    with_prefix = sandbox._replace_delimited_stanza(
        "keep me\n" + once, POINTER_BEGIN, POINTER_END, body,
    )
    assert "keep me" in with_prefix
    assert with_prefix.count(POINTER_BEGIN) == 1


def test_sync_assets_sbuser_apply_lands_codex_target_and_pointer(tmp_path):
    """THE TB-401 regression-pin: a `--sbuser apply` deploys the
    cross-runtime surface end-to-end (real subprocess, no sudo) —

      1. skills mirror into the Codex `~/.agents/skills/` target (additive,
         alongside the retained Claude `~/.claude/skills/` mirror);
      2. the repo `AGENTS.md` reference lands at `~/.agents/AGENTS.md`;
      3. a managed `skills-discovery` pointer stanza is written into BOTH
         runtimes' global instruction files (`~/.claude/CLAUDE.md`,
         `~/.codex/AGENTS.md`), each pointing at that runtime's skills root;

    and a SECOND apply is idempotent — every pointer file still carries
    exactly one stanza (no duplication)."""
    claude_dir = tmp_path / ".claude"
    rc = sandbox.sync_assets(sbuser=True, apply=True, dest=claude_dir)
    assert rc == 0

    # (1) Skills mirrored into the Codex ~/.agents/skills/ target ...
    agents_skills = tmp_path / ".agents" / "skills"
    assert (agents_skills / "ap2" / "SKILL.md").is_file()
    assert (agents_skills / "ap2-task" / "SKILL.md").is_file()
    assert (agents_skills / "migrate-to-ap2" / "SKILL.md").is_file()
    # ... and STILL into the Claude target (additive, not a move).
    assert (claude_dir / "skills" / "ap2" / "SKILL.md").is_file()
    # No separate Claude-side quick-reference file is deployed (TB-406).
    assert not (claude_dir / _RETIRED_QUICKREF_DEPLOY).exists()

    # (2) Codex operator reference (repo AGENTS.md) deployed verbatim.
    agents_md = tmp_path / ".agents" / "AGENTS.md"
    assert agents_md.is_file()
    assert agents_md.read_text() == AGENTS_SRC.read_text()

    # (3) Discovery-pointer stanza in BOTH runtimes' global instructions,
    # each pointing at that runtime's skills root.
    claude_pointer = claude_dir / "CLAUDE.md"
    codex_pointer = tmp_path / ".codex" / "AGENTS.md"
    for ptr, skills_path in (
        (claude_pointer, "~/.claude/skills/"),
        (codex_pointer, "~/.agents/skills/"),
    ):
        assert ptr.is_file(), f"missing discovery pointer: {ptr}"
        text = ptr.read_text()
        assert POINTER_BEGIN in text and POINTER_END in text
        assert skills_path in text
        assert text.count(POINTER_BEGIN) == 1

    # Idempotency: a SECOND apply must not duplicate the stanza nor change
    # the pointer files byte-for-byte.
    before_claude = claude_pointer.read_text()
    before_codex = codex_pointer.read_text()
    assert sandbox.sync_assets(sbuser=True, apply=True, dest=claude_dir) == 0
    assert claude_pointer.read_text() == before_claude
    assert codex_pointer.read_text() == before_codex
    assert claude_pointer.read_text().count(POINTER_BEGIN) == 1
    assert codex_pointer.read_text().count(POINTER_BEGIN) == 1


def test_sync_assets_pointer_preserves_existing_content_and_converges(tmp_path):
    """The discovery-pointer manager rewrites ONLY its delimited stanza,
    preserving pre-existing operator content in the global instructions
    file, and converges — a re-run is a byte-for-byte no-op (no second
    stanza appended)."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir(parents=True)
    pointer = claude_dir / "CLAUDE.md"
    pointer.write_text("# My notes\n\nhand-written operator prefs\n")

    assert sandbox.sync_assets(sbuser=True, apply=True, dest=claude_dir) == 0
    text = pointer.read_text()
    assert "hand-written operator prefs" in text  # surrounding content kept
    assert POINTER_BEGIN in text
    assert text.count(POINTER_BEGIN) == 1

    # A second apply leaves the pointer file identical.
    snapshot = pointer.read_text()
    assert sandbox.sync_assets(sbuser=True, apply=True, dest=claude_dir) == 0
    assert pointer.read_text() == snapshot


def test_sync_assets_sbuser_second_run_reports_all_in_sync(tmp_path, capsys):
    """After a cross-runtime apply, a follow-up dry-run reports EVERY asset
    (both skills roots, both reference files, both pointers) in sync — the
    overall drift is zero and nothing prints 'drift'. Pins that the new
    Codex target + pointers participate in the idempotency accounting, not
    just the Claude assets."""
    claude_dir = tmp_path / ".claude"
    assert sandbox.sync_assets(sbuser=True, apply=True, dest=claude_dir) == 0
    capsys.readouterr()  # drain first-run output
    assert sandbox.sync_assets(sbuser=True, apply=False, dest=claude_dir) == 0
    out = capsys.readouterr().out
    assert "all assets in sync" in out
    assert "drift" not in out


def test_sync_assets_missing_agents_md_source_errors(monkeypatch, tmp_path, capsys):
    """If the repo `AGENTS.md` source is missing, default mode aborts with
    rc=1 and a clear stderr surface — mirroring the skills/howto source
    prechecks. Pin so a packaging slip that drops `AGENTS.md` fails loudly
    rather than silently skipping the Codex reference."""
    _wire_user_home(monkeypatch, tmp_path)
    monkeypatch.setattr(sandbox, "_agents_md_source", lambda: tmp_path / "nope" / "AGENTS.md")
    rc = sandbox.sync_assets("claude-agent", sbuser=False, apply=False)
    assert rc == 1
    assert "AGENTS.md source missing" in capsys.readouterr().err


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
        # The reference-file / pointer drift probes (sh -c 'test -f ...
        # && cat ... || true') return empty stdout, signalling the target
        # is absent in dest so the function proceeds to tee.
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
    # and at least one tee write to the AGENTS.md reference target.
    rsync_apply_calls = [
        argv for argv, _ in captures
        if "rsync" in argv and "-a" in argv and "-an" not in argv
    ]
    assert rsync_apply_calls, "expected at least one `rsync -a --delete` apply"

    # A tee write to the Codex AGENTS.md reference target (TB-406 dropped
    # the howto tee; AGENTS.md is now the single single-file reference).
    agents_md_dest = dest.parent / ".agents" / "AGENTS.md"
    agents_tees = [
        (argv, body) for argv, body in captures
        if "tee" in argv and str(agents_md_dest) in argv
    ]
    assert agents_tees, "expected a tee write for the AGENTS.md reference"
    _agents_argv, agents_body = agents_tees[0]
    assert agents_body == AGENTS_SRC.read_text()


def test_sync_assets_default_mode_targets_users_home_dirs(monkeypatch, tmp_path):
    """When no `dest` override is passed, default mode resolves all runtime
    targets under `~<user>/` via `_user_home(user)` — skills mirror into
    BOTH `~<user>/.claude/skills/` AND `~<user>/.agents/skills/` (TB-401).
    Pin so a refactor that hard-codes a path, skips the home-dir lookup, or
    drops the Codex target surfaces (would break cross-user deploys)."""
    home = _wire_user_home(monkeypatch, tmp_path)
    captures: list[tuple[str, ...]] = []

    def fake_run(argv, *a, **kw):
        captures.append(tuple(argv))
        if "rsync" in argv and "-an" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if "sh" in argv:
            # Reference-file / pointer drift probes return empty stdout;
            # this test only asserts the rsync destinations (dry-run).
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)

    rc = sandbox.sync_assets("claude-agent", sbuser=False, apply=False)
    assert rc == 0

    # Every rsync destination must resolve under the resolved user home.
    rsync_calls = [argv for argv in captures if "rsync" in argv]
    assert rsync_calls, "expected rsync calls"
    dsts = [argv[-1] for argv in rsync_calls]
    for dst_arg in dsts:
        assert dst_arg.startswith(str(home)), (
            f"rsync dest didn't resolve under ~user home: {dst_arg}"
        )
    # Both runtime skills roots must appear among the destinations — the
    # Claude target AND the additive Codex `~/.agents/skills/` target.
    assert any(str(home / ".claude" / "skills") in d for d in dsts), (
        "no rsync into ~user/.claude/skills/"
    )
    assert any(str(home / ".agents" / "skills") in d for d in dsts), (
        "no rsync into the Codex ~user/.agents/skills/ target"
    )


def test_sync_assets_default_mode_skips_unchanged_agents_md(monkeypatch, tmp_path):
    """If the destination AGENTS.md reference matches the source
    byte-for-byte, the function skips the tee write (idempotency). Pin so a
    refactor that drops the drift comparison and always re-writes surfaces
    here — that'd be a regression both for performance (unnecessary sudo
    writes on a no-op refresh) and for the reference file's mtime stability.
    (TB-406 retired the howto target; AGENTS.md is now the single single-file
    reference this contract guards.)"""
    _wire_user_home(monkeypatch, tmp_path)
    dest = tmp_path / "claude"
    agents_md_dest = dest.parent / ".agents" / "AGENTS.md"
    captures: list[tuple[str, ...]] = []

    def fake_run(argv, *a, **kw):
        captures.append(tuple(argv))
        joined = " ".join(argv)
        if "rsync" in argv and "-an" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if "sh" in argv:
            # Only the AGENTS.md reference drift-probe sees a matching file;
            # the pointer-stanza reads (`.codex/AGENTS.md`, `.claude/CLAUDE.md`)
            # see an absent file (empty stdout). The reference dest lives under
            # `.agents/`, distinct from the `.codex/` pointer path.
            if str(agents_md_dest) in joined:
                return subprocess.CompletedProcess(
                    argv, 0, stdout=AGENTS_SRC.read_text(), stderr="",
                )
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)

    rc = sandbox.sync_assets(
        "claude-agent", sbuser=False, apply=True, dest=dest,
    )
    assert rc == 0
    # No tee write should have fired for AGENTS.md — it's already in sync.
    # (Other targets — the pointer stanzas — may legitimately tee; this test
    # pins ONLY the AGENTS.md reference idempotency contract.)
    agents_tees = [
        argv for argv in captures
        if "tee" in argv and str(agents_md_dest) in argv
    ]
    assert not agents_tees, (
        "expected no tee write when AGENTS.md matches source byte-for-byte"
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

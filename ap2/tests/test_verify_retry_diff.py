"""Unit tests for the per-task verifier's diff-resolver (TB-127, TB-136).

Two-layer evolution captured here:

  - **TB-127** introduced ``_find_task_commit`` / ``_git_show_for_task`` so
    that on retry of a task whose first attempt already committed an
    implementation, the prose-bullet judge would see the implementation diff
    instead of HEAD's daemon ``state:`` bookkeeping diff. That fix picked
    the MOST RECENT ``<task_id>:`` commit.

  - **TB-136** flips the choice to the OLDEST ``<task_id>:`` commit and
    folds it into a cumulative range diff, ``git diff <first>^..HEAD --
    :!.cc-autopilot/``. The prior "most recent" pick falsely failed
    bullets whose evidence lived in the FIRST commit when a retry
    appended a small incremental fix (TB-135 case: 95% of work in
    f839194, 5% in 248957f; the verifier saw only 248957f and missed
    the tests that landed in f839194). The cumulative range gives the
    judge every code change across all retries, with daemon state-file
    noise stripped via the pathspec exclude.

These tests pin the helpers directly (no SDK). The e2e wiring through
``verify_task`` is exercised in ``tests/e2e/test_verify_per_task.py``.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from ap2.verify import (
    CUMULATIVE_DIFF_EXCLUDES,
    _cumulative_task_diff,
    _find_first_task_commit,
    _git_show_head,
)


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd)] + args,
        capture_output=True, text=True, check=True,
    )


def _git_init(cwd: Path) -> None:
    _git(["init", "--initial-branch=main"], cwd)
    _git(["config", "user.email", "test@example.com"], cwd)
    _git(["config", "user.name", "Test"], cwd)
    _git(["commit", "--allow-empty", "-m", "init"], cwd)


def _state_commit(cwd: Path, msg: str = "state: TB-5 → Backlog") -> None:
    """A daemon-style bookkeeping commit (mutates only `.cc-autopilot/`).

    Mirrors how the daemon's state moves look in production: TASKS.md
    edits + retry counter under `.cc-autopilot/`. The cumulative diff
    excludes `:!.cc-autopilot/`, so anything we want stripped lives there.
    """
    sub = cwd / ".cc-autopilot"
    sub.mkdir(exist_ok=True)
    state_file = sub / "retry_state.json"
    prev = state_file.read_text() if state_file.exists() else ""
    state_file.write_text(prev + "{}\n")
    _git(["add", ".cc-autopilot/retry_state.json"], cwd)
    _git(["commit", "-m", msg], cwd)


# ---------------------------------------------------------------------------
# _find_first_task_commit (TB-136 — was _find_task_commit, picked-most-recent)
# ---------------------------------------------------------------------------


def test_find_first_task_commit_returns_none_outside_git_repo(tmp_path: Path):
    """Skip path: no `.git` dir → None (mirrors _git_show_head's contract)."""
    assert _find_first_task_commit(tmp_path, "TB-5") is None


def test_find_first_task_commit_returns_none_when_no_matching_subject(tmp_path: Path):
    _git_init(tmp_path)
    (tmp_path / "a.py").write_text("a = 1\n")
    _git(["add", "a.py"], tmp_path)
    _git(["commit", "-m", "unrelated: chore"], tmp_path)

    assert _find_first_task_commit(tmp_path, "TB-5") is None


def test_find_first_task_commit_finds_head_commit_when_subject_matches(tmp_path: Path):
    """Happy path: agent just committed, HEAD's subject names the task."""
    _git_init(tmp_path)
    (tmp_path / "foo.py").write_text("def foo(): pass\n")
    _git(["add", "foo.py"], tmp_path)
    _git(["commit", "-m", "TB-5: add foo"], tmp_path)

    head_sha = _git(["rev-parse", "HEAD"], tmp_path).stdout.strip()
    found = _find_first_task_commit(tmp_path, "TB-5")
    assert found == head_sha


def test_find_first_task_commit_walks_past_state_commits_to_implementation(
    tmp_path: Path,
):
    """The retry case (TB-127, still required under TB-136): TB-5's
    implementation commit is N commits back; HEAD is a `state:` bookkeeping
    commit. The walker must skip the state commits and return the
    implementation SHA so the cumulative diff anchors at it.
    """
    _git_init(tmp_path)

    # Implementation commit (first attempt).
    (tmp_path / "foo.py").write_text("def foo(): return 42\n")
    _git(["add", "foo.py"], tmp_path)
    _git(["commit", "-m", "TB-5: implement foo with tests"], tmp_path)
    impl_sha = _git(["rev-parse", "HEAD"], tmp_path).stdout.strip()

    # Several daemon state commits on top — these are what HEAD currently
    # points at when a retry runs.
    for _ in range(3):
        _state_commit(tmp_path)

    head_sha = _git(["rev-parse", "HEAD"], tmp_path).stdout.strip()
    assert head_sha != impl_sha  # paranoia

    found = _find_first_task_commit(tmp_path, "TB-5")
    assert found == impl_sha


def test_find_first_task_commit_strict_token_match_avoids_substring_collision(
    tmp_path: Path,
):
    """`TB-12` must not match a subject for `TB-127: ...` — the matcher
    splits on `[:\\s]` and compares the first token exactly. Mirrors the
    same convention used by `daemon._infer_result_from_head` (TB-74).
    """
    _git_init(tmp_path)
    (tmp_path / "x.py").write_text("x = 1\n")
    _git(["add", "x.py"], tmp_path)
    _git(["commit", "-m", "TB-127: bigger task that mentions TB-12"], tmp_path)

    assert _find_first_task_commit(tmp_path, "TB-12") is None
    assert _find_first_task_commit(tmp_path, "TB-127") is not None


def test_find_first_task_commit_picks_oldest_when_multiple_match(tmp_path: Path):
    """TB-136 inversion of TB-127: when the same task has committed twice
    (a follow-up incremental commit on retry), return the OLDEST match —
    that's the anchor for the cumulative diff range. The motivating bug:
    TB-135 had f839194 (95% of work, all flagged tests) and 248957f (small
    editor-mode follow-up). The TB-127 helper picked 248957f and the judge
    falsely failed bullets whose evidence lived in f839194's diff.
    """
    _git_init(tmp_path)
    (tmp_path / "a.py").write_text("a = 1\n")
    _git(["add", "a.py"], tmp_path)
    _git(["commit", "-m", "TB-5: first attempt (the bulk)"], tmp_path)
    first_sha = _git(["rev-parse", "HEAD"], tmp_path).stdout.strip()

    (tmp_path / "b.py").write_text("b = 2\n")
    _git(["add", "b.py"], tmp_path)
    _git(["commit", "-m", "TB-5: gap-fill on retry"], tmp_path)

    found = _find_first_task_commit(tmp_path, "TB-5")
    assert found == first_sha, (
        "regression: helper went back to TB-127's most-recent pick; "
        "the cumulative diff range needs the OLDEST anchor"
    )


def test_find_first_task_commit_picks_oldest_across_three_with_state_commits(
    tmp_path: Path,
):
    """The full TB-135-shape: three task-id commits scattered across the
    log with daemon state commits interleaved between them. The walker
    must still anchor at the oldest task-id commit regardless of how many
    state commits separate it from HEAD.
    """
    _git_init(tmp_path)

    # Retry 1 (oldest task-id commit — the anchor).
    (tmp_path / "a.py").write_text("a = 1\n")
    _git(["add", "a.py"], tmp_path)
    _git(["commit", "-m", "TB-5: first attempt"], tmp_path)
    anchor_sha = _git(["rev-parse", "HEAD"], tmp_path).stdout.strip()

    _state_commit(tmp_path)

    # Retry 2.
    (tmp_path / "b.py").write_text("b = 2\n")
    _git(["add", "b.py"], tmp_path)
    _git(["commit", "-m", "TB-5: retry 2 fix"], tmp_path)

    _state_commit(tmp_path)
    _state_commit(tmp_path)

    # Retry 3 (most recent task-id commit).
    (tmp_path / "c.py").write_text("c = 3\n")
    _git(["add", "c.py"], tmp_path)
    _git(["commit", "-m", "TB-5: retry 3 polish"], tmp_path)

    _state_commit(tmp_path)

    found = _find_first_task_commit(tmp_path, "TB-5")
    assert found == anchor_sha


# ---------------------------------------------------------------------------
# _cumulative_task_diff
# ---------------------------------------------------------------------------


def test_cumulative_task_diff_returns_implementation_diff_under_state_commits(
    tmp_path: Path,
):
    """When the implementation is buried under state commits, the cumulative
    diff anchored at the task-id commit returns the implementation
    contents (`foo.py` / `def foo`), not just the bookkeeping diff.
    """
    _git_init(tmp_path)
    (tmp_path / "foo.py").write_text("def foo(): return 42\n")
    _git(["add", "foo.py"], tmp_path)
    _git(["commit", "-m", "TB-5: implement foo"], tmp_path)

    _state_commit(tmp_path)

    # Sanity: HEAD's diff is just a `.cc-autopilot/` state file.
    head_diff = _git_show_head(tmp_path)
    assert ".cc-autopilot/" in head_diff
    assert "foo.py" not in head_diff

    # Cumulative diff: contains the implementation file.
    diff = _cumulative_task_diff(tmp_path, "TB-5")
    assert "foo.py" in diff
    assert "def foo()" in diff


def test_cumulative_task_diff_strips_dot_cc_autopilot_state_noise(tmp_path: Path):
    """The pathspec exclude (`:!.cc-autopilot/`) must keep daemon state files
    out of the diff handed to the prose judge — that's the noise filter
    half of the TB-136 design.
    """
    _git_init(tmp_path)
    (tmp_path / "foo.py").write_text("def foo(): return 42\n")
    _git(["add", "foo.py"], tmp_path)
    _git(["commit", "-m", "TB-5: implement foo"], tmp_path)

    # Add a state-file commit AFTER the task commit. The cumulative range
    # `<first>^..HEAD` would otherwise include the state file's diff.
    _state_commit(tmp_path, msg="state: TB-5 → Backlog")

    diff = _cumulative_task_diff(tmp_path, "TB-5")
    # Implementation present.
    assert "foo.py" in diff
    # State noise stripped.
    assert ".cc-autopilot/retry_state.json" not in diff


def test_cumulative_task_diff_concatenates_changes_across_two_task_commits(
    tmp_path: Path,
):
    """Cumulative range diff with two task-id commits + a state commit
    between them: every file added by EITHER task commit shows up. This
    is the TB-135 case in miniature — the prior helper saw only the
    follow-up commit and falsely flagged tests added in the first.
    """
    _git_init(tmp_path)

    # First task commit (the bulk).
    (tmp_path / "first.py").write_text("def first(): return 1\n")
    (tmp_path / "test_first.py").write_text("def test_first(): assert True\n")
    _git(["add", "first.py", "test_first.py"], tmp_path)
    _git(["commit", "-m", "TB-5: first attempt with tests"], tmp_path)

    _state_commit(tmp_path)

    # Second task commit (incremental fix).
    (tmp_path / "second.py").write_text("def second(): return 2\n")
    _git(["add", "second.py"], tmp_path)
    _git(["commit", "-m", "TB-5: gap-fill on retry"], tmp_path)

    diff = _cumulative_task_diff(tmp_path, "TB-5")
    # First commit's contents.
    assert "first.py" in diff
    assert "test_first.py" in diff
    assert "def test_first" in diff
    # Second commit's contents.
    assert "second.py" in diff
    assert "def second" in diff


def test_cumulative_task_diff_concatenates_three_task_commits_with_state(
    tmp_path: Path,
):
    """Three task-id commits with state commits scattered between them —
    the cumulative diff still surfaces every code file added across all
    three retries, with `.cc-autopilot/` state-file diffs filtered out.
    """
    _git_init(tmp_path)

    # Retry 1.
    (tmp_path / "r1.py").write_text("r1 = 1\n")
    _git(["add", "r1.py"], tmp_path)
    _git(["commit", "-m", "TB-5: retry 1"], tmp_path)

    _state_commit(tmp_path)

    # Retry 2.
    (tmp_path / "r2.py").write_text("r2 = 2\n")
    _git(["add", "r2.py"], tmp_path)
    _git(["commit", "-m", "TB-5: retry 2"], tmp_path)

    _state_commit(tmp_path)
    _state_commit(tmp_path)

    # Retry 3.
    (tmp_path / "r3.py").write_text("r3 = 3\n")
    _git(["add", "r3.py"], tmp_path)
    _git(["commit", "-m", "TB-5: retry 3"], tmp_path)

    _state_commit(tmp_path)

    diff = _cumulative_task_diff(tmp_path, "TB-5")
    assert "r1.py" in diff
    assert "r2.py" in diff
    assert "r3.py" in diff
    assert ".cc-autopilot/retry_state.json" not in diff


def test_cumulative_task_diff_handles_root_task_commit(tmp_path: Path):
    """If the first task-id commit is the repo's root commit (no parent),
    `<first>^` doesn't resolve. The resolver must fall back to the empty
    tree as the synthetic base so the diff still includes the root commit's
    contents.
    """
    _git(["init", "--initial-branch=main"], tmp_path)
    _git(["config", "user.email", "test@example.com"], tmp_path)
    _git(["config", "user.name", "Test"], tmp_path)

    # No prior `init` commit: this IS the root.
    (tmp_path / "foo.py").write_text("def foo(): pass\n")
    _git(["add", "foo.py"], tmp_path)
    _git(["commit", "-m", "TB-5: bootstrap"], tmp_path)

    diff = _cumulative_task_diff(tmp_path, "TB-5")
    assert "foo.py" in diff
    assert "def foo" in diff


def test_cumulative_task_diff_falls_back_to_head_when_no_match(tmp_path: Path):
    """When no commit subject matches the task id, fall back to HEAD —
    preserves legacy behavior for tasks whose first attempt never
    committed (the prose judge will then see whatever HEAD has).
    """
    _git_init(tmp_path)
    (tmp_path / "x.py").write_text("x = 1\n")
    _git(["add", "x.py"], tmp_path)
    _git(["commit", "-m", "unrelated commit"], tmp_path)

    head_diff = _git_show_head(tmp_path)
    diff = _cumulative_task_diff(tmp_path, "TB-99")
    assert diff == head_diff


def test_cumulative_task_diff_uses_head_when_task_id_is_none(tmp_path: Path):
    """Backward-compat: callers that don't pass `task_id` keep getting HEAD."""
    _git_init(tmp_path)
    (tmp_path / "x.py").write_text("x = 1\n")
    _git(["add", "x.py"], tmp_path)
    _git(["commit", "-m", "TB-5: implement x"], tmp_path)

    assert _cumulative_task_diff(tmp_path, None) == _git_show_head(tmp_path)


def test_cumulative_diff_excludes_pin_dot_cc_autopilot(tmp_path: Path):
    """Lock the exclude set to `.cc-autopilot/`. A future change that drops
    the exclude or broadens it should fail loudly here — losing the
    exclude floods the judge with TASKS.md state moves; broadening it
    risks hiding actual code changes.
    """
    assert CUMULATIVE_DIFF_EXCLUDES == (":!.cc-autopilot/",)


# ---------------------------------------------------------------------------
# _judge_prose_bullet — TB-136: judge gets read-only repo tools so it can
# verify presence in the working tree when the diff is ambiguous.
# ---------------------------------------------------------------------------


def test_judge_repo_read_tools_pin_read_glob_grep():
    """Lock the judge's tool set: Read + Glob + Grep, in that order. These
    are the read-only tools that let the judge verify a test/symbol/file
    actually lives in HEAD before declaring it missing (the TB-136 belt-
    and-suspenders companion to the cumulative diff). A future change
    that drops Grep or adds Edit/Write must fail loudly here.
    """
    from ap2.verify import JUDGE_REPO_READ_TOOLS

    assert JUDGE_REPO_READ_TOOLS == ["Read", "Glob", "Grep"]


def test_judge_passes_read_glob_grep_to_sdk():
    """Integration pin: when `_judge_prose_bullet` builds its
    `ClaudeAgentOptions`, `allowed_tools` must include Read, Glob, and
    Grep (and only those — the judge is read-only by design). Without
    these the judge falls back to diff-only reasoning and re-introduces
    the TB-135 false-negative class.
    """
    import asyncio

    from ap2 import verify

    captured: dict = {}

    class _OptionsRecorder:
        def __init__(self, **kw):
            captured["options"] = kw

    async def _gen(prompt, options):  # noqa: ARG001
        from ap2.tests.e2e._fakes import _FakeMsg
        yield _FakeMsg('{"status": "pass", "rationale": "ok"}')

    class _SDK:
        ClaudeAgentOptions = _OptionsRecorder

        @staticmethod
        def query(*, prompt, options):
            return _gen(prompt, options)

    bullet = verify.VerifyBullet(kind="prose", text="some bullet")
    asyncio.run(verify._judge_prose_bullet(
        bullet,
        project_root=Path("/tmp"),  # not actually read; the SDK is fake
        sdk=_SDK,
        diff_text="diff goes here",
    ))

    opts = captured["options"]
    allowed = opts.get("allowed_tools")
    assert allowed is not None, "allowed_tools must be passed explicitly"
    # Pin exact identity so a future change to JUDGE_REPO_READ_TOOLS
    # propagates to the SDK call. Use set comparison to allow ordering
    # changes; presence is what's load-bearing.
    assert set(allowed) == {"Read", "Glob", "Grep"}, allowed
    # The judge must NOT carry write/exec tools — read-only by design.
    for forbidden in ("Edit", "Write", "Bash", "NotebookEdit"):
        assert forbidden not in allowed, (
            f"{forbidden!r} must not be in the prose-judge allowed_tools; "
            "the judge is read-only"
        )
    # cwd must scope to project_root so Read/Glob/Grep don't escape.
    assert opts.get("cwd") == "/tmp"
    # Allow tool roundtrips: a max_turns of 1 would prevent the judge from
    # ever using its tools (one turn = either reply or one tool call).
    assert int(opts.get("max_turns", 0)) >= 2, opts


def test_judge_max_turns_default_is_twenty(monkeypatch):
    """TB-137: with `AP2_VERIFY_JUDGE_MAX_TURNS` unset, the SDK options handed
    to `_judge_prose_bullet` carry ``max_turns=20``. Eight (the previous
    default) was too tight for non-trivial repo navigation — a bullet
    asserting a test exists in a moved file may take 3-4 Grep/Glob/Read
    round-trips, and hitting the cap mid-investigation forced an
    unverified/fail verdict despite the bullet being satisfied in HEAD.
    """
    import asyncio

    from ap2 import verify

    monkeypatch.delenv("AP2_CORE_VERIFY_JUDGE_MAX_TURNS", raising=False)

    captured: dict = {}

    class _OptionsRecorder:
        def __init__(self, **kw):
            captured["options"] = kw

    async def _gen(prompt, options):  # noqa: ARG001
        from ap2.tests.e2e._fakes import _FakeMsg
        yield _FakeMsg('{"status": "pass", "rationale": "ok"}')

    class _SDK:
        ClaudeAgentOptions = _OptionsRecorder

        @staticmethod
        def query(*, prompt, options):
            return _gen(prompt, options)

    bullet = verify.VerifyBullet(kind="prose", text="some bullet")
    asyncio.run(verify._judge_prose_bullet(
        bullet,
        project_root=Path("/tmp"),
        sdk=_SDK,
        diff_text="diff goes here",
    ))

    assert int(captured["options"]["max_turns"]) == 20, captured["options"]


def test_judge_max_turns_env_override_still_works(monkeypatch):
    """TB-137: bumping the default from 8 to 20 must not break the env
    override path. Operators who want to tighten the budget for cost
    reasons can still set ``AP2_VERIFY_JUDGE_MAX_TURNS=4`` and have it
    flow through to the SDK options unchanged.
    """
    import asyncio

    from ap2 import verify

    monkeypatch.setenv("AP2_CORE_VERIFY_JUDGE_MAX_TURNS", "4")

    captured: dict = {}

    class _OptionsRecorder:
        def __init__(self, **kw):
            captured["options"] = kw

    async def _gen(prompt, options):  # noqa: ARG001
        from ap2.tests.e2e._fakes import _FakeMsg
        yield _FakeMsg('{"status": "pass", "rationale": "ok"}')

    class _SDK:
        ClaudeAgentOptions = _OptionsRecorder

        @staticmethod
        def query(*, prompt, options):
            return _gen(prompt, options)

    bullet = verify.VerifyBullet(kind="prose", text="some bullet")
    asyncio.run(verify._judge_prose_bullet(
        bullet,
        project_root=Path("/tmp"),
        sdk=_SDK,
        diff_text="diff goes here",
    ))

    assert int(captured["options"]["max_turns"]) == 4, captured["options"]


def test_judge_prompt_instructs_to_treat_working_tree_as_authoritative():
    """The prose-judge prompt must tell the judge that when the diff and
    the working tree disagree, HEAD wins. Without this instruction the
    model might still trust the (potentially stale) diff and falsely
    fail bullets whose evidence is on disk but not in the diff window.
    """
    import asyncio

    from ap2 import verify

    captured: dict = {}

    class _Options:
        def __init__(self, **kw):
            self.kw = kw

    async def _gen(prompt, options):  # noqa: ARG001
        captured["prompt"] = prompt
        from ap2.tests.e2e._fakes import _FakeMsg
        yield _FakeMsg('{"status": "pass", "rationale": "ok"}')

    class _SDK:
        ClaudeAgentOptions = _Options

        @staticmethod
        def query(*, prompt, options):
            return _gen(prompt, options)

    bullet = verify.VerifyBullet(kind="prose", text="some bullet about a test")
    asyncio.run(verify._judge_prose_bullet(
        bullet,
        project_root=Path("/tmp"),
        sdk=_SDK,
        diff_text="some diff",
    ))

    prompt = captured["prompt"]
    # Three load-bearing instructions per the TB-136 design:
    #   1. The diff is cumulative across retries (so the judge knows it
    #      isn't seeing only the latest commit).
    #   2. The working tree is authoritative when the diff is ambiguous.
    #   3. The judge has Read/Glob/Grep available and should USE them
    #      before declaring a test/symbol/file missing.
    lower = prompt.lower()
    assert "cumulative" in lower
    assert "authoritative" in lower
    assert "working tree" in lower
    assert "grep" in lower or "glob" in lower


# ---------------------------------------------------------------------------
# _run_shell_bullet — TB-147: shell bullets execute under /bin/bash, not
# /bin/sh. Without this override, common bash-only constructs (process
# substitution, `[[ ]]`, arrays, `set -o pipefail`) fail at the parser stage
# under sh (bash-in-POSIX-mode on macOS, dash on Debian-family Linux). Bullet
# authors invariably write bash; the verifier should match.
# ---------------------------------------------------------------------------


def _shell_bullet(cmd: str):
    """Build a VerifyBullet that `_run_shell_bullet` will execute as `cmd`."""
    from ap2.verify import VerifyBullet

    return VerifyBullet(kind="shell", text=f"`{cmd}`", command=cmd)


def test_run_shell_bullet_supports_process_substitution(tmp_path: Path):
    """`<(...)` is bash's process substitution. Under /bin/sh on macOS
    (bash-in-POSIX-mode) it fails with `syntax error near unexpected token
    '('`; under dash (Debian /bin/sh) it isn't implemented at all. With the
    /bin/bash override, the same bullet parses and runs cleanly to exit 0.
    Pins the bash override at the call site.
    """
    from ap2.verify import _run_shell_bullet

    bullet = _shell_bullet("diff <(echo a) <(echo a)")
    result = _run_shell_bullet(bullet, project_root=tmp_path, timeout_s=30)
    assert result.status == "pass", (
        f"process substitution failed: notes={result.notes!r}; "
        "regression — verifier may have reverted to /bin/sh"
    )


def test_run_shell_bullet_supports_double_bracket_conditional(tmp_path: Path):
    """`[[ ... ]]` is bash's extended test expression. POSIX sh parses `[[`
    as a command name, so it exits 127 (`[[: not found`). With the bash
    override, `[[ -d <existing_dir> ]]` returns 0.
    """
    from ap2.verify import _run_shell_bullet

    (tmp_path / "marker").mkdir()
    bullet = _shell_bullet("[[ -d marker ]]")
    result = _run_shell_bullet(bullet, project_root=tmp_path, timeout_s=30)
    assert result.status == "pass", (
        f"[[ ]] conditional failed: notes={result.notes!r}; "
        "regression — verifier may have reverted to /bin/sh"
    )


def test_run_shell_bullet_still_fails_on_genuine_command_error(tmp_path: Path):
    """Belt-and-suspenders: bash isn't covering for an actual non-zero exit.
    A bullet that deliberately exits 1 must still return `fail` — the bash
    override only fixes shell-parser surprises, not command semantics.
    """
    from ap2.verify import _run_shell_bullet

    bullet = _shell_bullet("python3 -c 'raise SystemExit(1)'")
    result = _run_shell_bullet(bullet, project_root=tmp_path, timeout_s=30)
    assert result.status == "fail"
    assert "exit=1" in result.notes


def test_run_shell_bullet_call_site_pins_bin_bash_executable():
    """Defense in depth: pin the literal `executable="/bin/bash"` argument
    in the call site so a maintainer can't silently drop it (a la "let's
    make this more portable"). The behavioral tests above catch a dropped
    override on bash-only constructs, but they'd silently pass on a system
    where /bin/sh happens to be a recent bash and not POSIX-mode (some
    Linux distros symlink /bin/sh -> /bin/bash). This test pins the source.
    """
    import inspect

    from ap2 import verify

    src = inspect.getsource(verify._run_shell_bullet)
    assert 'executable="/bin/bash"' in src, (
        "regression: TB-147's /bin/bash override is missing from "
        "_run_shell_bullet — bash-only bullets will fail under sh"
    )


# ---------------------------------------------------------------------------
# TB-156: per-call-site effort knob for the prose-bullet judge + diff
# truncation cap lowered from 100KB to 30KB.
#
# The judge runs once per prose bullet — multiplied across every retry of
# every task with prose criteria, the per-call cost adds up. Two tier-1
# token-saving levers:
#
#   1. Trim the worst-case-defensive diff cap (100KB → 30KB). The judge has
#      Read/Glob/Grep (TB-136) and the prompt tells it the working tree at
#      HEAD is authoritative when the diff is ambiguous, so the cap is now a
#      soft hint rather than a hard wall.
#   2. Lower the judge's reasoning-effort budget from `xhigh` → `high`. The
#      judge's job (read a diff, optionally Grep/Read for confirmation,
#      emit a one-line JSON verdict) doesn't need multi-step reasoning.
#
# Both are pinned below. The effort knob is a per-site env var
# (AP2_VERIFY_JUDGE_EFFORT) that takes precedence over the global
# AP2_AGENT_EFFORT and falls through to a per-site default of `high` when
# neither is set — task agents and the MM handler stay on the global
# default (currently `xhigh`).
# ---------------------------------------------------------------------------


def _capture_judge_options(monkeypatch=None) -> dict:
    """Run `_judge_prose_bullet` once with a fake SDK and return the
    `{prompt, options}` dict captured from the SDK call. Used by the
    TB-156 effort + truncation tests below."""
    import asyncio

    from ap2 import verify

    captured: dict = {}

    class _OptionsRecorder:
        def __init__(self, **kw):
            captured["options"] = kw

    async def _gen(prompt, options):  # noqa: ARG001
        captured["prompt"] = prompt
        from ap2.tests.e2e._fakes import _FakeMsg
        yield _FakeMsg('{"status": "pass", "rationale": "ok"}')

    class _SDK:
        ClaudeAgentOptions = _OptionsRecorder

        @staticmethod
        def query(*, prompt, options):
            return _gen(prompt, options)

    bullet = verify.VerifyBullet(kind="prose", text="some bullet")
    asyncio.run(verify._judge_prose_bullet(
        bullet,
        project_root=Path("/tmp"),
        sdk=_SDK,
        diff_text="x" * 50_000,  # callers that don't care about the diff
                                 # still see truncation behavior here
    ))
    return captured


def test_judge_default_effort_is_high_when_no_env_set(monkeypatch):
    """TB-156: with neither `AP2_VERIFY_JUDGE_EFFORT` nor `AP2_AGENT_EFFORT`
    set, the per-site default kicks in and the SDK options carry
    `extra_args["effort"] == "high"` — NOT `xhigh` (the pre-TB-156 global
    default that this knob displaces for the judge specifically)."""
    monkeypatch.delenv("AP2_CORE_VERIFY_JUDGE_EFFORT", raising=False)
    monkeypatch.delenv("AP2_CORE_AGENT_EFFORT", raising=False)

    captured = _capture_judge_options()
    extra = captured["options"]["extra_args"]
    assert extra["effort"] == "high", extra


def test_judge_effort_per_site_env_takes_precedence(monkeypatch):
    """TB-156: `AP2_VERIFY_JUDGE_EFFORT` overrides the global
    `AP2_AGENT_EFFORT`. With per-site=`medium` and global=`xhigh`, the SDK
    options carry `medium` — operators can dial the judge separately from
    the rest of the agent fleet."""
    monkeypatch.setenv("AP2_CORE_VERIFY_JUDGE_EFFORT", "medium")
    monkeypatch.setenv("AP2_CORE_AGENT_EFFORT", "xhigh")

    captured = _capture_judge_options()
    assert captured["options"]["extra_args"]["effort"] == "medium"


def test_judge_effort_falls_through_to_global_when_per_site_unset(monkeypatch):
    """TB-156: precedence chain — when `AP2_VERIFY_JUDGE_EFFORT` is unset
    but `AP2_AGENT_EFFORT` is set, the global wins (and so the judge
    inherits whatever global override the operator pinned). Only when
    BOTH are unset does the per-site default of `high` kick in."""
    monkeypatch.delenv("AP2_CORE_VERIFY_JUDGE_EFFORT", raising=False)
    monkeypatch.setenv("AP2_CORE_AGENT_EFFORT", "xhigh")

    captured = _capture_judge_options()
    assert captured["options"]["extra_args"]["effort"] == "xhigh"


def test_judge_diff_truncated_to_30kb_in_prompt(monkeypatch):
    """TB-156: the prompt sent to the SDK contains at most 30,000 chars
    of the cumulative diff. A synthetic 50KB diff is truncated to 30KB
    before being interpolated; the remaining 20KB never reaches the SDK
    (the judge can Grep/Read for what it needs — TB-136). This pins the
    new cap; a regression that bumps it back to 100KB would re-introduce
    the ~70KB-of-padding judge-token waste that motivated TB-156."""
    monkeypatch.delenv("AP2_CORE_VERIFY_JUDGE_EFFORT", raising=False)
    monkeypatch.delenv("AP2_CORE_AGENT_EFFORT", raising=False)

    captured = _capture_judge_options()
    prompt = captured["prompt"]
    # The diff is wrapped in a fenced ```...``` block in the prompt body.
    # We don't pin the exact wrapping (the prompt may evolve); we just
    # assert the truncated diff content (30K of 'x') lands and the rest
    # does not. Use a counter on 'x' chars between the fences.
    assert "x" * 30_000 in prompt, (
        "the first 30,000 diff chars must reach the prompt"
    )
    assert "x" * 30_001 not in prompt, (
        "diff truncation cap is no longer 30,000 — TB-156 regression"
    )


def test_judge_diff_truncation_cap_pinned_at_call_site():
    """Defense in depth: pin the literal `diff_text[:30_000]` slice in
    the source so a maintainer can't silently bump it back to 100,000
    (`[:100_000]` is the pre-TB-156 cap) without a test failure. The
    behavioral test above catches a bump on the synthetic 50K diff, but
    a bump to e.g. 35,000 would silently pass it — pinning the literal
    here closes that loophole."""
    import inspect

    from ap2 import verify

    src = inspect.getsource(verify._judge_prose_bullet)
    assert "diff_text[:30_000]" in src, (
        "regression: TB-156's 30KB diff truncation cap is missing from "
        "_judge_prose_bullet — judge tokens will balloon"
    )
    # Anti-regression: the previous 100KB cap must NOT reappear.
    assert "diff_text[:100_000]" not in src, (
        "regression: pre-TB-156 100KB diff cap is back in "
        "_judge_prose_bullet — re-introduces ~70KB of padding tokens"
    )


def test_judge_effort_env_knob_present_in_source():
    """Source-level pin so a maintainer can't silently drop the
    `AP2_VERIFY_JUDGE_EFFORT` env knob and revert the judge to the
    global `AP2_AGENT_EFFORT`. The verification grep
    (`AP2_VERIFY_JUDGE_EFFORT in ap2/verify.py`) backs this up at the
    daemon level; this test catches the same regression in CI."""
    import inspect

    from ap2 import verify

    src = inspect.getsource(verify._judge_prose_bullet)
    assert "AP2_VERIFY_JUDGE_EFFORT" in src, (
        "regression: TB-156's per-site effort knob is missing from "
        "_judge_prose_bullet"
    )

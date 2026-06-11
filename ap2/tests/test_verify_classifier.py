"""Regression-pin tests for the per-task verifier's prose-vs-shell bullet
classifier (TB-219).

Pins the four observed-in-the-wild failure shapes from the
2026-05-12 → 2026-05-13 incident window (TB-204, TB-206, TB-207, TB-209)
as parametrized cases against `parse_verification_section`. The classifier
change in TB-219 is one-way — once it ships, the only thing that'll cause
these classes to fail again is a regression in the parse step itself. These
tests are the anti-drift gate.

Each test names the motivating TB-N in a docstring or comment so future
debuggers can map "this test failed" → "this is the n=Nth re-occurrence
of the shape; here's the original incident."

See `ap2/verify.py` for the classifier rules and the **ap2-task** skill's
"Authoring `## Verification` bullets" section (`skills/ap2-task/SKILL.md`,
consolidated out of `ap2/howto.md` in TB-400) for the operator-facing
convention these tests pin.
"""
from __future__ import annotations

import pytest

from ap2.verify import (
    JUDGE_INDICATOR_PHRASES,
    PROSE_PREFIX,
    parse_verification_section,
)


def _parse_one(bullet_md: str):
    """Wrap a single bullet line in a minimal `## Verification` section
    and return the parsed single `VerifyBullet`. Keeps each test focused
    on the one bullet under examination without `## Verification` boilerplate
    noise in every parametrize entry.
    """
    text = f"## Verification\n{bullet_md}\n"
    bullets = parse_verification_section(text)
    assert bullets is not None, f"section parsed to None for: {bullet_md!r}"
    assert len(bullets) == 1, (
        f"expected exactly one bullet from {bullet_md!r}, got {bullets!r}"
    )
    return bullets[0]


# --- TB-204-shape: directory-arg grep without `-r` ----------------------
#
# The bullet WAS correctly classified as shell — the failure was at runtime
# (the shell command's body returned 2 because `grep` against a directory
# without `-r` exits with "Is a directory"). This regression-pin asserts
# the classifier still routes this shape to `shell` so a future tightening
# doesn't accidentally route it to prose and mask the operator-side bug.
def test_tb204_shape_dir_arg_grep_classifies_as_shell():
    """TB-204: bullet `[ "$(grep -lE 'pat' dir/ 2>/dev/null | wc -l)" -ge 1 ]`
    is a well-formed shell bullet (codespan leading, no `Prose:` prefix,
    no judge-indicator phrases, no stray backtick). The classifier MUST
    still route it to shell; the missing-`-r` runtime bug is operator-side
    and orthogonal to the classifier.
    """
    bullet = _parse_one(
        "- `[ \"$(grep -lE 'pat' dir/ 2>/dev/null | wc -l)\" -ge 1 ]` "
        "— directory match-count gate"
    )
    assert bullet.kind == "shell", (bullet.kind, bullet.command_error)
    assert bullet.command is not None
    assert "grep -lE 'pat' dir/" in bullet.command
    # Command should round-trip through `_list_item_leading_codespan`
    # without truncation — there's no literal backtick to confuse mistune.
    assert bullet.command.endswith("-ge 1 ]"), bullet.command


# --- TB-206-shape: `!` exit-inversion prefix ----------------------------
#
# Same story as TB-204: classifier correctly routed to shell. The failure
# was a bullet-body authoring bug (the script lacked the `!` so the
# absence check inverted to a presence check at runtime). Pin the
# classification.
def test_tb206_shape_bang_prefix_absence_check_classifies_as_shell():
    """TB-206: bullet `! grep "absent string" file` is a well-formed shell
    bullet using bash's exit-status inversion (`!`) to assert absence. The
    classifier MUST still route it to shell; the operator-side fix
    (remembering to include `!`) is orthogonal to the classifier.
    """
    bullet = _parse_one(
        "- `! grep \"absent-string\" path/to/file` — absence check"
    )
    assert bullet.kind == "shell", (bullet.kind, bullet.command_error)
    assert bullet.command == '! grep "absent-string" path/to/file'


# --- TB-207-shape: literal backtick inside a single-backtick codespan ---
#
# This IS a classifier bug — markdown's single-backtick codespan cannot
# represent a literal backtick, so mistune truncates the codespan at the
# inner backtick and the remaining half of the shell command leaks into
# the bullet's prose body. Previously the classifier silently ran the
# truncated half-command (exit 2 or worse). The TB-219 fix emits a
# `kind="malformed"` bullet so the operator sees a typed error rather
# than a downstream shell exit code.
@pytest.mark.parametrize(
    "bullet_md,expect_kind,expect_command_substring",
    [
        # (a) Original BROKEN shape: literal `` ` `` inside single-backtick
        # codespan. Classifier should detect the truncation and emit
        # kind="malformed" with a helpful command_error.
        (
            "- `[ \"$(grep -cE '^\\| `pat' file)\" -ge 1 ]` — count check",
            "malformed",
            None,
        ),
        # (b) FIXED shape: double-backtick wrapping preserves the inner
        # backtick. Classifier should emit kind="shell" with the FULL
        # command (including the literal backtick) extracted.
        (
            "- `` [ \"$(grep -cE '^\\| `pat' file)\" -ge 1 ] `` — count check",
            "shell",
            "`pat",  # literal backtick must survive into the command
        ),
    ],
    ids=["tb207-broken-single-backtick", "tb207-fixed-double-backtick"],
)
def test_tb207_shape_literal_backtick_in_shell_bullet(
    bullet_md, expect_kind, expect_command_substring,
):
    """TB-207: literal backtick inside a shell bullet's command body.
    Broken markdown shape (single-backtick wrapping) → `kind="malformed"`
    with a `command_error` that names the trap and suggests rewrites.
    Fixed shape (double-backtick wrapping) → `kind="shell"` with the
    literal backtick preserved in the extracted command.

    Either outcome is acceptable per the briefing's contract ("extracts
    the full intended command OR explicitly reports a malformed-bullet
    error") — what's forbidden is the OLD behavior of silently truncating
    to a half-command.
    """
    bullet = _parse_one(bullet_md)
    assert bullet.kind == expect_kind, (bullet.kind, bullet.command, bullet.command_error)
    if expect_kind == "malformed":
        assert bullet.command_error is not None
        # Error must name the trap concretely so the operator can rewrite
        # without re-running the verifier blindly.
        assert "backtick" in bullet.command_error.lower()
        assert bullet.command is None
    else:
        assert bullet.command is not None
        # Crucial: the literal backtick is NOT truncated away.
        assert expect_command_substring in bullet.command


# --- TB-209-shape: prose bullet leading with a backtick-fenced filename --
#
# This is the n=4 incident's literal trigger. Pre-TB-219 classifier: codespan
# lead → shell → verifier executes the bare file path → `Permission denied`
# (exit 126) → 3 retries → Frozen. Post-TB-219 classifier: BOTH the `Prose:`
# hard-override branch AND the judge-indicator heuristic branch must route
# the bullet to prose. Pin both branches.
@pytest.mark.parametrize(
    "bullet_md,branch",
    [
        # Branch A: explicit `Prose:` hard-override prefix (the convention
        # operators have been writing organically since TB-206/207/209
        # fix briefings).
        (
            "- `ap2/tests/test_coverage_drift.py` Prose: "
            "the file includes the expected `_COVERAGE_DRIFT_EXEMPT_SURFACES` "
            "fixture; judge confirms via Read.",
            "prose-prefix",
        ),
        # Branch B: no `Prose:` prefix, but a judge-indicator phrase
        # ("Judge confirms") in the suffix tells the classifier this is
        # judge-routed even though it leads with a backtick-fenced path.
        (
            "- `ap2/tests/test_coverage_drift.py` exists and asserts on "
            "the expected fixture set. Judge confirms via `Read` of the "
            "new test body.",
            "judge-indicator",
        ),
    ],
    ids=["tb209-prose-prefix", "tb209-judge-indicator"],
)
def test_tb209_shape_codespan_lead_prose_classifies_as_prose(bullet_md, branch):
    """TB-209: prose bullet whose grammatical subject is a backtick-fenced
    filename. BOTH the `Prose:` hard-override AND the judge-indicator
    heuristic must route the bullet to prose so the verifier doesn't
    try to exec the bare path.
    """
    bullet = _parse_one(bullet_md)
    assert bullet.kind == "prose", (branch, bullet.kind, bullet.command)
    # Prose bullets carry no command; the codespan-leading filename is part
    # of the bullet's text, not a parsed shell command.
    assert bullet.command is None
    assert bullet.command_error is None


# --- Backward-compat: existing well-formed bullets still work -----------
#
# Belt-and-suspenders for the briefing's "Existing well-formed bullets
# continue to be classified as shell" contract — if a future refactor
# accidentally broadens the prose detection, this catches it before
# the n=5 incident.
@pytest.mark.parametrize(
    "bullet_md,expect_command",
    [
        ("- `uv run pytest -q` — tests pass", "uv run pytest -q"),
        ("- `ruff check .` — lint clean", "ruff check ."),
        # Double-backtick form, well-formed: backslash inside a path
        ("- `` `test -f scripts/run.py` `` — file present", "test -f scripts/run.py"),
    ],
    ids=["plain-pytest", "ruff-check", "double-backtick-wrapped"],
)
def test_well_formed_shell_bullets_still_shell(bullet_md, expect_command):
    """No `Prose:` prefix, no judge-indicator phrase, no stray backtick —
    these bullets must continue to route to `shell` post-TB-219."""
    bullet = _parse_one(bullet_md)
    assert bullet.kind == "shell", (bullet.kind, bullet.command_error)
    assert bullet.command == expect_command


def test_judge_indicator_phrases_constant_includes_documented_set():
    """The briefing-authoring docs (`ap2/howto.md`) name the convention
    by referencing the constant. This pins the contents so a docs reader
    can rely on the constant matching the listed phrases."""
    # Strict superset check: all documented phrases must be in the constant.
    documented = {
        "Judge confirms",
        "(judged by",
        "judge confirms",
        "the SDK against the diff",
        "judged via",
    }
    assert documented.issubset(set(JUDGE_INDICATOR_PHRASES)), (
        documented - set(JUDGE_INDICATOR_PHRASES)
    )


def test_prose_prefix_constant_is_literal_token():
    """The `Prose:` hard-override prefix is the operator-authored signal.
    Pin its exact form so a future "let's allow `prose:` (lowercase)"
    drift either ships with a docs update or fails this test loudly."""
    assert PROSE_PREFIX == "Prose:"

"""Per-task verification agent (TB-69).

Pairs with TB-66's project-wide gate: where TB-66 answers "did the project
break", this answers "did the agent actually do THIS task's work" by running
each bullet under the briefing's `## Verification` section.

Two kinds of bullets are supported:

  - **Shell bullets** — the bullet starts with a backtick-fenced command
    like `` `uv run pytest -q` ``. The runner executes it via
    `subprocess.run` with `cfg.verify_timeout_s`, exit 0 = pass.

  - **Prose bullets** — free-form acceptance text. Judged by an SDK call
    against the agent's diff (`git show HEAD`). When no SDK is provided
    (e.g. the daemon ran without one wired through, or a legacy briefing
    has prose-only criteria pre-deployment), prose bullets are recorded as
    `unverified` — non-blocking, but surfaced in the verdict so the operator
    can see what wasn't checked.

Verdict aggregation:

  - `pass`    — every shell bullet passed AND every prose bullet was
    judged `pass` (or there were no prose bullets).
  - `fail`    — at least one shell bullet failed OR at least one prose
    bullet was judged `fail`.
  - `partial` — no failures, but at least one prose bullet is `unverified`
    (no SDK to judge it). The daemon treats `partial` as a soft pass:
    Complete proceeds, but a `verification_partial` event is emitted for
    auditability.

Skip path: `verify_task` returns `None` when the briefing is missing, the
file doesn't exist, or has no `## Verification` section. Existing tasks
without verification sections keep working unchanged.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .json_extract import extract_rightmost_json_object


# Mistune AST parser — used for `## Verification` section detection and bullet
# classification (TB-102). Replaces the regex tower (`_VERIFICATION_HEADER_RE`,
# `_BULLET_RE`, `_SHELL_LEAD_RE`, `_SHELL_DOUBLE_RE`) that successively bit us:
#   - TB-91: `\s*$` rejected `## Verification (launch-task — ...)` headings
#   - TB-146 round 1: same regex variant
#   - TB-146 round 2: shell-extraction regex didn't handle double-backtick
#     code spans (`` ` `cmd` ` ``)
# Markdown is rich enough that ad-hoc regex against it is a losing
# arms race. mistune's AST handles all of these natively.
#
# TB-153: import lazily. Importing mistune at module load made `from
# ap2.verify import parse_verification_section` (and every transitive
# importer such as `ap2.tools`) fail under any python interpreter that
# doesn't carry mistune in site-packages — notably the system `python3`
# the per-task verifier shells `python3 -c "..."` bullets through. The
# parser only matters when the daemon walks bullets in-process, so we
# defer the import to first call.
_MD = None  # type: ignore[var-annotated]


def _get_md():
    """Lazily build the mistune AST parser. Called only when the daemon
    actually parses a `## Verification` section (in-process, where mistune
    is always present). Shell-bullet `python3 -c` invocations that import
    `ap2.verify` transitively never hit this path."""
    global _MD
    if _MD is None:
        import mistune
        _MD = mistune.create_markdown(renderer="ast")
    return _MD


@dataclass
class VerifyBullet:
    kind: str  # "shell" | "prose" | "malformed"
    text: str  # full bullet text (for prose)
    command: str | None = None  # extracted shell command, if shell
    # TB-219: when the classifier detects an unrecoverable bullet shape (e.g.
    # the TB-207 "literal backtick inside a single-backtick codespan that
    # markdown can't represent" trap), `kind="malformed"` and
    # `command_error` carries the human-readable explanation. The dispatcher
    # in `verify_task` emits a failed `CriterionResult` with these notes
    # rather than silently truncating to a half-command and exec'ing it.
    command_error: str | None = None


# TB-219: Judge-indicator phrases that flip a codespan-leading bullet from
# "shell" to "prose". Conservative, observed-in-the-wild list — these are
# the phrases operators and ideation routinely write to mean "this bullet
# is judge-routed, even if it leads with a backtick-fenced file path or
# symbol name." Without this fallback the TB-209-shape trap fires: an
# ideation-authored prose bullet whose grammatical subject is a backtick-
# fenced filename gets mis-classified as shell, the verifier executes the
# bare file path → `Permission denied`, exit 126 → retry-exhausts → Frozen
# (the n=4 incident across TB-204/TB-206/TB-207/TB-209). Adding to this
# tuple is cheap and forward-compatible; removing a phrase that's already
# in operator-authored briefings risks reopening the trap, so prefer
# extension over rewording.
JUDGE_INDICATOR_PHRASES: tuple[str, ...] = (
    "Judge confirms",
    "(judged by",
    "judge confirms",
    "the SDK against the diff",
    "judged via",
)


#: TB-219: hard-override prefix that forces a bullet to `prose` classification
#: regardless of whether the first inline child is a codespan. The convention
#: has organically appeared in operator-authored briefings (TB-206/207/209
#: operator-fix briefings all use it); codifying it here closes the upstream
#: design hole — operators get an unambiguous "do not exec this bullet"
#: signal, and ideation will pick it up via `ap2/howto.md`'s authoring
#: guidance. Case-sensitive (matches the operator-authored shape exactly);
#: the single colon and optional trailing whitespace are part of the literal
#: token.
PROSE_PREFIX: str = "Prose:"


@dataclass
class CriterionResult:
    bullet: str
    kind: str
    status: str  # "pass" | "fail" | "unverified"
    notes: str = ""


@dataclass
class VerifyVerdict:
    overall: str  # "pass" | "fail" | "partial"
    criteria: list[CriterionResult] = field(default_factory=list)
    duration_s: float = 0.0


def _heading_text(node: dict) -> str:
    """Concat all text/codespan content under a heading node into a string."""
    parts: list[str] = []
    for ch in node.get("children", []) or []:
        if ch.get("type") == "text":
            parts.append(ch.get("raw", ""))
        elif ch.get("type") == "codespan":
            parts.append(ch.get("raw", ""))
        elif ch.get("children"):
            parts.append(_heading_text(ch))
    return "".join(parts).strip()


def _is_verification_heading(node: dict) -> bool:
    """True iff `node` is a level-2 heading whose text starts with the word
    "Verification" (with a word-boundary follow). Tolerates parentheticals
    and trailing disambiguators: `## Verification`,
    `## Verification (launch-task)`, `## Verification — output`, etc.
    Rejects look-alikes like `## Verifications` (plural) or
    `## VerificationTable`.
    """
    if node.get("type") != "heading":
        return False
    if (node.get("attrs") or {}).get("level") != 2:
        return False
    text = _heading_text(node)
    if not text.startswith("Verification"):
        return False
    after = text[len("Verification"):]
    # Word boundary: next char must NOT be alphanumeric/underscore.
    if after and (after[0].isalnum() or after[0] == "_"):
        return False
    return True


def _list_item_text(item: dict) -> str:
    """Reconstruct a list_item's full text content (codespans rendered with
    their backticks, regular text rendered as-is). Used for the bullet's
    free-form `text` field on `VerifyBullet`."""
    parts: list[str] = []
    for ch in item.get("children", []) or []:
        if ch.get("type") in ("block_text", "paragraph"):
            for inner in ch.get("children", []) or []:
                if inner.get("type") == "codespan":
                    parts.append(f"`{inner.get('raw', '')}`")
                else:
                    parts.append(inner.get("raw", ""))
        elif ch.get("type") == "codespan":
            parts.append(f"`{ch.get('raw', '')}`")
        elif ch.get("type") == "text":
            parts.append(ch.get("raw", ""))
    return "".join(parts).strip()


def _bullet_inlines(item: dict) -> list[dict]:
    """Return the bullet's inline children (the contents of its
    `block_text` / `paragraph` wrapper), or `[]` if there are none.

    Factored out of `_list_item_leading_codespan` so the new TB-219
    classifier helpers (`_has_prose_prefix`, `_has_unbalanced_backtick`)
    can walk the same inlines without duplicating the wrapper unwrap.
    """
    for ch in item.get("children", []) or []:
        if ch.get("type") in ("block_text", "paragraph"):
            return ch.get("children", []) or []
    return []


def _list_item_leading_codespan(item: dict) -> str | None:
    """If the list_item's first inline child is a codespan, return its raw
    content with surrounding backticks stripped (handles both
    `` `cmd` `` → "cmd" and `` ` `cmd` ` `` → "`cmd`" → "cmd"). Else None.
    """
    inlines = _bullet_inlines(item)
    if not inlines:
        return None
    first = inlines[0]
    if first.get("type") != "codespan":
        return None
    raw = first.get("raw", "").strip()
    # Mistune's `raw` for a single-backtick `` `cmd` `` codespan is just
    # "cmd" (no backticks). For the double-backtick form `` ` `cmd` ` ``,
    # mistune nests the inner backticks INTO the raw content as
    # "`cmd`" — strip those.
    if raw.startswith("`") and raw.endswith("`") and len(raw) >= 2:
        raw = raw[1:-1].strip()
    return raw or None


def _has_prose_prefix(item: dict) -> bool:
    """TB-219: True iff the bullet has the `Prose:` hard-override prefix.

    The check is "after the first inline child" so a bullet of the shape
    `` `path/to/file.py` Prose: this is judge-routed`` qualifies (the
    leading codespan is the bullet's grammatical subject; the operator
    wants the rest evaluated as prose). Leading whitespace between the
    codespan and the `Prose:` token is tolerated (mistune inserts a
    space-prefixed text node between adjacent inline children); leading
    punctuation (em-dashes, colons, hyphens) is NOT — operators write
    `` `foo` Prose: ...`` or just `Prose: ...` at bullet start.

    For bullets whose first inline child is already a text node starting
    with `Prose:` (no leading codespan), this returns False — the
    codespan-leading classifier already routes them to prose via the
    "no leading codespan → prose" branch, so an extra match here would be
    redundant.
    """
    inlines = _bullet_inlines(item)
    if len(inlines) < 2:
        return False
    parts: list[str] = []
    for ch in inlines[1:]:
        if ch.get("type") == "codespan":
            parts.append(f"`{ch.get('raw', '')}`")
        else:
            parts.append(ch.get("raw", ""))
    rest = "".join(parts).lstrip()
    return rest.startswith(PROSE_PREFIX)


def _has_judge_indicator(item: dict) -> bool:
    """TB-219: True iff the bullet's reconstructed text contains any of the
    judge-indicator phrases. Heuristic fallback for codespan-leading
    bullets that lack the explicit `Prose:` prefix — catches the
    TB-209-shape "ideation-authored prose bullet with a backtick-fenced
    filename lead" trap without forcing every existing operator briefing
    to be rewritten.
    """
    text = _list_item_text(item)
    return any(phrase in text for phrase in JUDGE_INDICATOR_PHRASES)


def _has_unbalanced_backtick(item: dict) -> bool:
    """TB-219: True iff a non-codespan inline child contains a literal
    backtick character. Strong signal that the bullet author wrote a shell
    command with a literal-backtick inside a single-backtick codespan,
    which markdown cannot represent — mistune truncates the codespan at
    the inner backtick and the remaining command leaks into the bullet's
    prose body as a sequence of text nodes (one of which is just `` ` ``).
    Detecting it lets the classifier emit a `kind="malformed"` bullet
    rather than silently exec'ing the truncated half-command (the TB-207
    trap).

    Mistune only emits a backtick-containing text node when the source
    had an unbalanced backtick — paired codespans always go into
    `codespan` nodes. So this detection is precise: it doesn't false-
    positive on bullets with multiple paired codespans (`` `cmd one`
    plus `cmd two```).
    """
    for ch in _bullet_inlines(item):
        if ch.get("type") == "text" and "`" in ch.get("raw", ""):
            return True
    return False


def parse_verification_section(briefing_text: str) -> list[VerifyBullet] | None:
    """Return parsed bullets, or None if there's no `## Verification` section.

    An empty section (header present but no bullets) returns `[]`, which
    callers can treat as "skip" (no concrete criteria to check).

    Picks the LAST `## Verification` section in the briefing — TB-91. TB-86
    pipeline-launch briefings include an inline `validation_briefing`
    sub-document (the briefing for the auto-created post-pipeline validation
    task), which has its own `## Verification` containing output-artifact
    checks. That sub-document is placed near Scope/Approach where the
    `pipeline_task_start` call is described, so the launch task's own
    `## Verification` always comes after it. Picking the last match correctly
    targets the launch task's verification. For single-Verification briefings
    (the common case) last == first, so behavior is unchanged.

    TB-102: parses via `mistune` AST instead of ad-hoc regex. The AST walks
    handle parenthetical headings, double-backtick code spans, indented
    bullets, and any other markdown rendering quirk natively — eliminating
    the regex-brittleness class that produced TB-91 and TB-146.

    TB-219 (classifier-tightening): the earlier "first inline child is a
    codespan → shell, else prose" rule was too aggressive. It mis-classified
    ideation-authored prose bullets that opened with a backtick-fenced
    filename (the TB-209-shape) → the verifier exec'd the bare path →
    `Permission denied` → retry-exhaust → Frozen (n=4 incident across
    TB-204/TB-206/TB-207/TB-209 in the 2026-05-12 → 2026-05-13 window).
    The current rule layers three signals on top of the leading-codespan
    check:

      1. **`Prose:` hard override** — if the bullet's text after the first
         inline child begins with the literal `Prose:` prefix, classify as
         `prose` unconditionally. This codifies the operator-authored
         convention that emerged in TB-206/207/209 fix briefings.
      2. **TB-207 malformed detection** — for codespan-leading bullets
         WITHOUT a `Prose:` prefix, look for a stray backtick character
         in any non-codespan inline child. Markdown's single-backtick
         codespan cannot represent a literal backtick, so a stray one
         means the codespan got truncated mid-command; emit
         `kind="malformed"` with an explanatory `command_error` rather
         than exec'ing the truncated half-command.
      3. **Judge-indicator heuristic fallback** — for codespan-leading
         bullets WITHOUT a `Prose:` prefix AND no malformed-backtick
         signal, scan the bullet text for any of the
         `JUDGE_INDICATOR_PHRASES` (`"Judge confirms"`, `"(judged by"`,
         etc.). If any match, classify as `prose`. Catches the TB-209
         shape without forcing a `Prose:` prefix on every legacy
         briefing.

    Well-formed bullets — those that lead with a real shell command in a
    single codespan and contain neither `Prose:` nor a judge-indicator
    phrase nor a stray backtick — continue to be classified as `shell`
    with their codespan extracted as `command`. Behavior is unchanged for
    the common case.
    """
    nodes = _get_md()(briefing_text) or []
    # Find the LAST verification heading; capture all sibling list nodes
    # following it until the next heading (or EOF).
    last_idx: int | None = None
    for i, node in enumerate(nodes):
        if _is_verification_heading(node):
            last_idx = i
    if last_idx is None:
        return None
    bullets: list[VerifyBullet] = []
    for node in nodes[last_idx + 1:]:
        if node.get("type") == "heading":
            break
        if node.get("type") != "list":
            continue
        for item in node.get("children", []) or []:
            if item.get("type") != "list_item":
                continue
            text = _list_item_text(item)
            if not text:
                continue
            # TB-219 step 1: `Prose:` hard override beats every other
            # signal. Operators reach for it precisely when the bullet
            # would otherwise be mis-classified.
            if _has_prose_prefix(item):
                bullets.append(VerifyBullet(kind="prose", text=text))
                continue
            cmd = _list_item_leading_codespan(item)
            if cmd:
                # TB-219 step 2: TB-207-shape malformed-backtick trap.
                # Emit a typed-fail bullet rather than exec'ing the
                # truncated codespan.
                if _has_unbalanced_backtick(item):
                    bullets.append(VerifyBullet(
                        kind="malformed",
                        text=text,
                        command_error=(
                            "TB-207-shape malformed bullet: a literal "
                            "backtick inside a single-backtick codespan "
                            "truncated the shell command. Rewrite the "
                            "bullet to either (a) use a double-backtick "
                            "wrapping (`` `..\\`..` ``) so markdown "
                            "preserves the inner backtick, or (b) "
                            "replace the literal backtick with the "
                            "regex any-char `.` if a regex pattern is "
                            "what you intended."
                        ),
                    ))
                    continue
                # TB-219 step 3: judge-indicator heuristic fallback for
                # codespan-leading prose bullets (TB-209 shape).
                if _has_judge_indicator(item):
                    bullets.append(VerifyBullet(kind="prose", text=text))
                    continue
                bullets.append(VerifyBullet(kind="shell", text=text, command=cmd))
            else:
                bullets.append(VerifyBullet(kind="prose", text=text))
    return bullets


def _run_shell_bullet(
    bullet: VerifyBullet,
    *,
    project_root: Path,
    timeout_s: int,
) -> CriterionResult:
    """Run a shell bullet via subprocess; exit 0 → pass, anything else → fail."""
    assert bullet.kind == "shell"
    assert bullet.command is not None
    try:
        # TB-147: pin the shell to /bin/bash. `subprocess.run(shell=True)` on
        # POSIX defaults to /bin/sh — bash-in-POSIX-mode on macOS (no process
        # substitution), dash on Debian-family (no `[[ ]]`, no arrays, no
        # process substitution). Bullet authors — humans and LLMs alike —
        # invariably write bash: `<(python3 -c ...)` for "compare against a
        # script's output", `[[ -f path ]]` for file checks, `set -o
        # pipefail`, etc. We've burned multiple briefings (TB-142..146) on
        # bash-only bullets that were correct under any developer shell but
        # tripped sh's parser before reaching the command. Aligning the
        # verifier with bash eliminates that whole class of self-inflicted
        # retry-exhaustion. macOS and every common Linux ship /bin/bash by
        # default; CI environments without it would already be broken on
        # operator scripts and aren't a target. Do NOT revert to /bin/sh
        # for "more portable" without re-reading TB-147's rationale.
        proc = subprocess.run(
            bullet.command,
            shell=True,
            executable="/bin/bash",
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return CriterionResult(
            bullet=bullet.text, kind="shell", status="fail",
            notes=f"timeout after {timeout_s}s",
        )
    if proc.returncode == 0:
        return CriterionResult(bullet=bullet.text, kind="shell", status="pass")
    return CriterionResult(
        bullet=bullet.text, kind="shell", status="fail",
        notes=f"exit={proc.returncode} stderr={proc.stderr[-300:]!r}",
    )


#: Read-only repo-inspection tools the prose-bullet judge may use to verify
#: claims directly against the working tree at HEAD. Belt-and-suspenders
#: companion to the cumulative diff (TB-136): the diff catches the common
#: case of a multi-retry implementation; direct repo reads catch the edge
#: cases (file moved, symbol renamed, test split across files, the working
#: tree drifted from the diff's last hunk because of state commits).
JUDGE_REPO_READ_TOOLS = ["Read", "Glob", "Grep"]


async def _judge_prose_bullet(
    bullet: VerifyBullet,
    *,
    project_root: Path,
    sdk,
    diff_text: str,
    events_file: Path | None = None,
    task_id: str | None = None,
    bullet_idx: int | None = None,
) -> CriterionResult:
    """Ask the SDK whether `bullet.text` is satisfied by `diff_text` plus the
    working tree at HEAD.

    The judge gets two evidence sources:

      1. ``diff_text`` — the cumulative diff across all task-id commits
         (TB-136). Reasoning over a diff is fast and catches most cases.
      2. ``Read``/``Glob``/``Grep`` tools scoped to ``project_root`` — the
         judge can confirm a test/symbol actually exists in HEAD before
         declaring it missing. This is the authoritative check when the
         diff is ambiguous (file moved, symbol renamed, or the diff was
         truncated). TB-136.

    Asks for a structured one-line JSON response; falls back to ``unverified``
    on parse failure rather than failing the whole verification (the prose
    judge is best-effort).

    TB-157: when ``events_file`` is provided, emits a ``judge_call`` event
    on each judge SDK call carrying usage / model / cost / verdict so
    cost-tradeoff experiments can aggregate per-judge token spend without
    routing through the daemon's `_log_message` (the judge has its own
    SDK loop that bypasses that capture path).
    """
    prompt = (
        "You are evaluating ONE acceptance bullet from a task's verification "
        "section against the agent's CUMULATIVE diff (every code commit "
        "across any retries of this task, with daemon state-file noise "
        "filtered out) AND the project's working tree at HEAD.\n\n"
        # TB-236: tightened final-message contract. The pre-TB-236 prompt
        # asked for "ONE LINE of JSON" but did not cap rationale length,
        # did not forbid markdown code fences, and did not show an
        # explicit example. Observed failure (TB-228 bullet 7) was a
        # 1100-token response with a long rationale containing unescaped
        # JSON-breaking characters; bullet 6 from the same task succeeded
        # at ~510 tokens with a short rationale. The shorter the
        # rationale, the smaller the surface area for JSON-escape bugs.
        # The constraint applies ONLY to the FINAL message — intermediate
        # Read/Grep tool calls (legal via JUDGE_REPO_READ_TOOLS) are
        # unconstrained.
        "OUTPUT CONTRACT — your FINAL message must be a JSON object only:\n"
        '  {"status": "pass", "rationale": "X exists per L42"}\n'
        "Rules for the FINAL message:\n"
        "  - It is a JSON object only. No markdown code fences (no ```json"
        " or ``` wrapping). No leading prose (no 'Here is the verdict:'"
        " preamble). No trailing commentary after the closing brace.\n"
        "  - `status` is exactly `\"pass\"` or `\"fail\"` (lowercase).\n"
        "  - `rationale` is a single short sentence, MAXIMUM 200 characters."
        " Cite a file:line or symbol name when possible; do NOT quote long"
        " code blocks or paste diff hunks into the rationale.\n"
        "  - If the rationale would naturally exceed 200 characters,"
        " summarize: cite the strongest single piece of evidence and"
        " stop.\n"
        "  - Intermediate tool calls (Read, Glob, Grep) during reasoning"
        " are unconstrained — only the FINAL message must satisfy this"
        " contract.\n\n"
        "Evidence priority — when the diff and the working tree disagree, "
        "the working tree at HEAD is AUTHORITATIVE. The diff can be "
        "truncated, span renames, or simply not show what's actually on "
        "disk after a multi-retry sequence. You have Read, Glob, and Grep "
        "tools scoped to the project root; before declaring a test or "
        "symbol or file missing, USE Grep/Glob to confirm it isn't present "
        "in HEAD under a different name or path. If you can find the "
        "asserted test/symbol/file in the working tree (Read it to verify "
        "shape if needed), the bullet PASSES regardless of whether the "
        "diff makes that obvious.\n\n"
        f"Bullet:\n  {bullet.text}\n\n"
        # TB-156: diff cap lowered from 100KB → 30KB. Most cumulative
        # diffs land in the 5-30KB range; the prior 100KB worst-case-
        # defensive cap was paying ~70KB of judge tokens per bullet for
        # padding. The judge has Read/Glob/Grep (TB-136) and the prompt
        # tells it the working tree at HEAD is authoritative — when the
        # truncated tail matters, it can pull what it needs directly. So
        # the cap is now a soft hint rather than a hard wall, traded
        # against ~50% judge-token savings on average. Operators wanting
        # a different cap can edit the source.
        f"Cumulative diff:\n```\n{diff_text[:30_000]}\n```\n"
    )
    try:
        # TB-156: per-call-site effort knob. The judge's job — read a diff,
        # optionally Grep/Read for confirmation, emit a one-line JSON
        # verdict — doesn't need the multi-step reasoning budget that
        # `xhigh` is sized for. Default to `high` here so the judge runs
        # cheaper than task agents (which stay on the global default,
        # `xhigh`); operators can still pin a specific value via
        # `AP2_VERIFY_JUDGE_EFFORT`, or globally via `AP2_AGENT_EFFORT`.
        # Precedence: per-site env > global env > per-site default.
        effort = os.environ.get(
            "AP2_VERIFY_JUDGE_EFFORT",
            os.environ.get("AP2_AGENT_EFFORT", "high"),
        )
        # The judge can take a few tool roundtrips (Grep → Read) before
        # emitting its final verdict, so allow a handful of turns. The
        # tools are read-only and scoped to project_root via cwd.
        options = sdk.ClaudeAgentOptions(
            cwd=str(project_root),
            allowed_tools=list(JUDGE_REPO_READ_TOOLS),
            permission_mode="bypassPermissions",
            max_turns=int(os.environ.get("AP2_VERIFY_JUDGE_MAX_TURNS", 20)),
            setting_sources=["project"],
            model=os.environ.get("AP2_AGENT_MODEL", "claude-opus-4-7"),
            extra_args={"effort": effort},
        )
        text = ""
        # TB-157: capture usage / cost / model / num_turns from the
        # ResultMessage(s) so the per-judge call can be costed independently
        # of the daemon's `_log_message` path (which `_judge_prose_bullet`
        # bypasses).
        result_meta: dict = {}
        t0 = time.monotonic()
        async for msg in sdk.query(prompt=prompt, options=options):
            content = getattr(msg, "content", None)
            if isinstance(content, list):
                for part in content:
                    t = getattr(part, "text", None)
                    if isinstance(t, str) and t.strip():
                        text = t.strip()
            else:
                t = getattr(msg, "result", None)
                if isinstance(t, str) and t.strip():
                    text = t.strip()
            # ResultMessage carries the usage / cost / model fields. We
            # accept ANY message that has them so a transport that buries
            # the totals in a non-standard envelope doesn't lose data.
            for k in ("model", "num_turns", "total_cost_usd", "stop_reason"):
                v = getattr(msg, k, None)
                if v is not None:
                    result_meta[k] = v
            for k in ("usage", "model_usage"):
                v = getattr(msg, k, None)
                if isinstance(v, dict) and v:
                    result_meta[k] = v
        duration_s = time.monotonic() - t0
    except Exception as e:  # noqa: BLE001
        return CriterionResult(
            bullet=bullet.text, kind="prose", status="unverified",
            notes=f"judge error: {type(e).__name__}: {e}",
        )

    outcome = _parse_judge_response(bullet.text, text)
    verdict = outcome.verdict

    # TB-236: when the response can't be parsed into a verdict, dump the
    # FULL raw last-assistant-text to a per-bullet debug file so the
    # operator can diagnose WHY without being limited to the 200-char
    # truncated preview the verifier carries in `notes`. Categorization
    # (`parse_error`) + length metrics (`response_length` /
    # `rationale_length`) ride on the `judge_call` event so events.jsonl
    # alone is enough to pattern-detect across many failures without
    # opening dumps. Dumps are written ONLY on parse failure — successful
    # judge calls leave no trace on disk beyond the existing event.
    dump_path: Path | None = None
    if outcome.parse_error is not None and events_file is not None:
        try:
            import datetime as _dt
            debug_dir = events_file.parent / "debug"
            debug_dir.mkdir(parents=True, exist_ok=True)
            ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            bullet_label = (
                bullet_idx if bullet_idx is not None else -1
            )
            task_label = task_id or "unknown"
            dump_path = (
                debug_dir
                / f"{ts}-{task_label}-judge-bullet{bullet_label}-response.txt"
            )
            dump_path.write_text(text or "")
        except Exception:  # noqa: BLE001
            # Diagnostic write must never break verification. If the
            # write fails, drop the path (event won't carry it either).
            dump_path = None

    # TB-157: emit `judge_call` so events.jsonl is the canonical aggregation
    # surface for prose-judge cost. Composes with `events.tail`, the web
    # events table, and the diagnose report — same envelope shape as
    # `task_complete`, `verification_failed`, etc. Best-effort: a write
    # failure here must not flip the judge's verdict.
    # TB-236: extended with `response_length` (always), `rationale_length`
    # (on successful parse), `parse_error` (on parse failure), and
    # `judge_response_dump` (path to the per-bullet dump file, when the
    # dump fired). The length fields are present on every call so an
    # operator can track whether the prompt-tightening prevention is
    # actually shortening rationales over time.
    if events_file is not None:
        try:
            from . import events as _events
            payload = {
                "task": task_id or "",
                "bullet_idx": bullet_idx if bullet_idx is not None else -1,
                "bullet_kind": bullet.kind,
                "verdict": verdict.status,
                "duration_s": round(duration_s, 3),
                "response_length": len(text or ""),
            }
            if outcome.rationale_length is not None:
                payload["rationale_length"] = outcome.rationale_length
            if outcome.parse_error is not None:
                payload["parse_error"] = outcome.parse_error
            if dump_path is not None:
                payload["judge_response_dump"] = str(dump_path)
            for k in ("model", "num_turns", "total_cost_usd",
                      "stop_reason", "usage", "model_usage"):
                if k in result_meta:
                    payload[k] = result_meta[k]
            _events.append(events_file, "judge_call", **payload)
        except Exception:  # noqa: BLE001
            # Instrumentation must never break verification. Swallow.
            pass

    return verdict


#: TB-236: parse-failure categorization labels. When the judge's final
#: response can't be turned into a `{"status": ..., "rationale": ...}`
#: verdict, the failure is classified into one of these labels so the
#: operator can pattern-detect across dump files without reading every
#: one. Ordered roughly from "structural" (no JSON at all) to "subtle"
#: (JSON content bug):
#:   - `no_json_object`         — response is empty or missing `{` / `}`
#:   - `trailing_prose_after_json` — `{...}` parses cleanly but non-
#:     whitespace follows the closing brace (judge added commentary)
#:   - `unescaped_in_string`    — `JSONDecodeError.msg` matched
#:     "Expecting" / "Invalid" / similar; usually an unescaped `"` or
#:     `\\` inside a string value
#:   - `json_truncated`         — `JSONDecodeError.msg` matched
#:     "Unterminated string"; response cut off mid-value
#:   - `parse_error_other`      — catch-all so the enum is closed
PARSE_ERROR_CATEGORIES: tuple[str, ...] = (
    "no_json_object",
    "trailing_prose_after_json",
    "unescaped_in_string",
    "json_truncated",
    "parse_error_other",
)


def _categorize_parse_error(response: str) -> str:
    """TB-236: classify why ``_parse_judge_response`` couldn't extract a
    verdict from ``response``. Returns one of ``PARSE_ERROR_CATEGORIES``.

    Heuristic-only — no SDK, no IO. The categorization is meant to be
    coarse enough that human operators (or a future LLM pattern detector)
    can answer "is this a prompt-tightening bug or a parser bug?" at a
    glance. The full raw response is dumped separately by the caller for
    cases where the category alone isn't enough.

    TB-261: extraction boundary-finding is centralized in
    ``ap2.json_extract.extract_rightmost_json_object``. This function
    inherits the rightmost-balanced-object semantics, then probes
    failure modes with ``json.JSONDecoder().raw_decode`` from each
    ``{`` position to reach the operator-actionable category
    (truncation vs. unescaped-string vs. bare-no-JSON-at-all).
    """
    if not response:
        return "no_json_object"
    # Fast path: did the centralized extractor find a clean JSON object?
    # If yes, the only remaining failure mode the categorizer needs to
    # name is "trailing prose after the verdict" (the JSON parsed
    # fine, but the judge appended commentary the call site flags as
    # a prompt-contract drift). Other "success" landings here are
    # unexpected — the catch-all parse_error_other carries them.
    extracted = extract_rightmost_json_object(response)
    if extracted is not None:
        _, _, end_offset = extracted
        if response[end_offset:].strip():
            return "trailing_prose_after_json"
        return "parse_error_other"
    # No parseable JSON object. Fall back to per-`{` probing to
    # categorize WHY each candidate failed — pick the most specific
    # surfaced JSONDecodeError message across all candidates.
    if "{" not in response:
        return "no_json_object"
    decoder = json.JSONDecoder()
    msgs: list[str] = []
    # Walk every `{` position in `response`. Using enumerate (not
    # `str.find` / `str.rfind`) keeps the categorizer free of the
    # pre-TB-261 boundary-finding pattern that the verification grep
    # forbids — the extractor centralizes that, and the categorizer
    # only needs candidate positions to probe error messages.
    for pos, ch in enumerate(response):
        if ch != "{":
            continue
        try:
            decoder.raw_decode(response, pos)
        except json.JSONDecodeError as e:
            msgs.append((e.msg or "").lower())
    # `json` raises "Unterminated string starting at" when a `"`-
    # opened string value has no matching close inside the candidate.
    # This is the truncation signature: model ran out of budget mid-
    # rationale.
    if any("unterminated string" in m for m in msgs):
        return "json_truncated"
    if "}" not in response:
        # Opening brace(s) present but no closing brace anywhere.
        # `raw_decode` failures with "Expecting" (property name / value /
        # delimiter) on a closing-brace-less response also point at
        # truncation since the JSON is structurally incomplete. Only
        # collapse to `no_json_object` if probes returned nothing
        # informative.
        if any("expecting" in m for m in msgs):
            return "json_truncated"
        return "no_json_object"
    # Both braces present, but no candidate `{` parses cleanly. Most
    # other failures surface as "Expecting ',' delimiter" or
    # "Expecting value" or "Invalid \\escape" — all consistent with
    # an unescaped quote / backslash inside the rationale.
    if any("expecting" in m or "invalid" in m for m in msgs):
        return "unescaped_in_string"
    return "parse_error_other"


@dataclass
class _ParseOutcome:
    """TB-236: result + diagnostics from ``_parse_judge_response``.

    Carries the criterion verdict plus optional metrics so
    ``_judge_prose_bullet`` can decide whether to dump the response and
    which length / categorization fields to add to the `judge_call`
    event. None values mean "not applicable for this outcome".
    """
    verdict: CriterionResult
    parse_error: str | None = None  # PARSE_ERROR_CATEGORIES value
    rationale_length: int | None = None  # len(rationale) on successful parse


def _parse_judge_response(bullet_text: str, response: str) -> _ParseOutcome:
    """Extract the JSON verdict from the judge's reply.

    The response should be `{"status": ..., "rationale": ...}` on one line.
    Tolerates prose preamble by extracting the **rightmost top-level**
    balanced ``{...}`` substring via
    ``ap2.json_extract.extract_rightmost_json_object`` (TB-261). The
    judge's prompt pins the verdict to the end of the response, so
    rightmost-wins matches the prompt contract even when the preamble
    holds literal braces (set notation, code examples) that would have
    shadowed the verdict under the pre-TB-261 first-``{`` / last-``}``
    boundary-finding.

    TB-236: returns a ``_ParseOutcome`` carrying the verdict plus
    diagnostic fields. ``parse_error`` is set on every failure path
    where the response could not be turned into a structurally-valid
    JSON object (empty / no JSON / malformed JSON), and ``None`` on
    successful parse OR on the "unknown status" path (JSON parsed
    fine, the value of ``status`` just wasn't `pass` / `fail`).
    ``rationale_length`` is populated whenever a rationale field was
    extracted (including the "unknown status" path), so the always-on
    length signal on `judge_call` events covers as many calls as
    possible.
    """
    if not response:
        return _ParseOutcome(
            verdict=CriterionResult(
                bullet=bullet_text, kind="prose", status="unverified",
                notes="empty judge response",
            ),
            parse_error=_categorize_parse_error(response),
        )
    # TB-261: rightmost-balanced extraction. Tolerates preamble braces
    # (the TB-89 trigger) and trailing prose (the TB-236 distinction).
    extracted = extract_rightmost_json_object(response)
    if extracted is None:
        return _ParseOutcome(
            verdict=CriterionResult(
                bullet=bullet_text, kind="prose", status="unverified",
                notes=f"no JSON object in response: {response[:200]!r}",
            ),
            parse_error=_categorize_parse_error(response),
        )
    data, json_start, json_end = extracted
    status = str(data.get("status", "")).lower().strip()
    rationale = str(data.get("rationale", "")).strip()
    if status not in ("pass", "fail"):
        return _ParseOutcome(
            verdict=CriterionResult(
                bullet=bullet_text, kind="prose", status="unverified",
                notes=f"unknown status {status!r}; rationale: {rationale[:200]}",
            ),
            # JSON parsed fine — this isn't a parse failure, just an
            # invalid `status` value. Leave parse_error as None so the
            # dump file isn't written for this case (per Scope §2, dumps
            # are for parse failures, not all unverifieds).
            parse_error=None,
            rationale_length=len(rationale),
        )
    # TB-236: trailing prose after a valid JSON object is recoverable
    # (we still extract a verdict), but it's a prompt-contract violation
    # worth tracking — surface it as `parse_error="trailing_prose_after_json"`
    # so the operator can see prompt-drift creeping in without flipping
    # the verdict to unverified.
    trailing_parse_error: str | None = None
    if response[json_end:].strip():
        trailing_parse_error = "trailing_prose_after_json"
    return _ParseOutcome(
        verdict=CriterionResult(
            bullet=bullet_text, kind="prose", status=status,
            notes=rationale,
        ),
        parse_error=trailing_parse_error,
        rationale_length=len(rationale),
    )


def _aggregate(results: list[CriterionResult]) -> str:
    if any(r.status == "fail" for r in results):
        return "fail"
    if any(r.status == "unverified" for r in results):
        return "partial"
    return "pass"


def _git_show_head(project_root: Path) -> str:
    if not (project_root / ".git").exists():
        return ""
    r = subprocess.run(
        ["git", "-C", str(project_root), "show", "HEAD"],
        capture_output=True, text=True,
    )
    return r.stdout if r.returncode == 0 else ""


#: Pathspec excludes folded into every cumulative-diff invocation. Keeps the
#: prose judge from drowning in the daemon's bookkeeping diffs (TASKS.md
#: state moves, retry counters, Attempts blocks appended to briefings,
#: events.jsonl appends). Listed here rather than inlined so the test suite
#: can pin the exact set.
CUMULATIVE_DIFF_EXCLUDES = (":!.cc-autopilot/",)


def _find_first_task_commit(project_root: Path, task_id: str) -> str | None:
    """Return the SHA of the OLDEST commit reachable from HEAD whose subject
    begins with ``<task_id>:`` (the convention pinned by the task-agent
    prompt — see ``daemon._infer_result_from_head``).

    Why the OLDEST and not the most recent (TB-136): when a task retries
    multiple times, the FIRST ``<task_id>:`` commit usually carries the
    bulk of the implementation; subsequent retry commits are incremental
    fixes. The TB-127 helper picked the most recent, so the prose judge
    only saw the small follow-up diff and falsely failed bullets whose
    evidence lived in the original commit (TB-135 case). Anchoring at
    the oldest task-id commit and diffing forward to HEAD gives the judge
    EVERY code change across the retry chain.

    Returns ``None`` if the project isn't a git repo, the log call fails,
    or no commit subject matches the convention.
    """
    if not (project_root / ".git").exists():
        return None
    # Bound the walk: 200 commits is far more than any retry chain produces.
    log = subprocess.run(
        [
            "git", "-C", str(project_root),
            "log", "-200", "--format=%H%x00%s", "HEAD",
        ],
        capture_output=True, text=True,
    )
    if log.returncode != 0:
        return None
    import re as _re
    oldest: str | None = None
    for line in log.stdout.splitlines():
        if "\x00" not in line:
            continue
        sha, subject = line.split("\x00", 1)
        # Match `daemon._infer_result_from_head` exactly: the first
        # colon-or-whitespace-delimited token must equal `task_id`. This
        # avoids substring false positives (e.g. "TB-12" in a subject for
        # "TB-127: ...").
        first_token = _re.split(r"[:\s]", subject, maxsplit=1)[0]
        if first_token == task_id:
            # `git log` orders newest-first; keep overwriting so the LAST
            # match (= oldest commit) wins.
            oldest = sha
    return oldest


#: Git's universal SHA for the empty tree. Used as the synthetic base when
#: the first task-id commit is the repo's root commit (no parent to take
#: ``^`` of). Stable across all git versions:
#:   $ git hash-object -t tree /dev/null
#:   4b825dc642cb6eb9a060e54bf8d69288fbee4904
_EMPTY_TREE_SHA = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def _cumulative_task_diff(project_root: Path, task_id: str | None) -> str:
    """Return the cumulative code diff across every retry of this task.

    Implementation: anchor at the OLDEST commit whose subject starts with
    ``<task_id>:`` (call it ``<first>``), then run::

        git diff <first>^..HEAD -- :!.cc-autopilot/

    so the prose judge sees every code change from ``<first>`` forward,
    minus daemon state-file noise (TASKS.md state moves, retry_state.json,
    events.jsonl appends, attempt blocks the daemon writes back into the
    briefing). TB-136.

    Edge cases:

      - ``task_id`` is ``None`` (legacy callers, smoke tests): fall back
        to ``git show HEAD``.
      - No ``<task_id>:`` commit reachable yet (agent didn't commit on
        first attempt; or this is the very first run pre-commit): fall
        back to ``git show HEAD`` so the judge still has SOMETHING to
        evaluate against.
      - ``<first>`` is the repo's root commit (no parent): use the empty
        tree as the synthetic base, so the diff is ``<empty>..HEAD``.
      - Project isn't a git repo: return ``""`` (matches the contract of
        ``_git_show_head``).
    """
    if not task_id:
        return _git_show_head(project_root)
    if not (project_root / ".git").exists():
        return ""
    sha = _find_first_task_commit(project_root, task_id)
    if not sha:
        return _git_show_head(project_root)

    # Resolve <first>^. If <first> is the root commit, rev-parse fails and
    # we substitute git's empty-tree SHA so the diff is still well-formed.
    parent = subprocess.run(
        ["git", "-C", str(project_root), "rev-parse", "--verify", f"{sha}^"],
        capture_output=True, text=True,
    )
    base = parent.stdout.strip() if parent.returncode == 0 else _EMPTY_TREE_SHA

    diff = subprocess.run(
        [
            "git", "-C", str(project_root),
            "diff", f"{base}..HEAD", "--",
            *CUMULATIVE_DIFF_EXCLUDES,
        ],
        capture_output=True, text=True,
    )
    if diff.returncode != 0:
        # Surface git's stderr in the fallback path rather than leaving the
        # judge with an empty string.
        return _git_show_head(project_root)
    return diff.stdout


async def verify_task(
    *,
    briefing_text: str | None,
    project_root: Path,
    timeout_s: int = 300,
    sdk=None,
    task_id: str | None = None,
    events_file: Path | None = None,
) -> VerifyVerdict | None:
    """Run the per-task verifier. Returns None when there's nothing to check.

    Skip cases that map to None:
      - `briefing_text` is None (legacy task, no briefing).
      - The briefing has no `## Verification` section.
      - The section is present but has no bullets.

    The skip cases let pre-TB-69 tasks proceed unchanged through the daemon.

    `task_id` (TB-127, refined in TB-136) is used to resolve the diff
    handed to the prose-bullet judge. When provided, we anchor at the
    OLDEST commit whose subject starts with `<task_id>:` and diff
    forward to HEAD, with `.cc-autopilot/` paths excluded — the
    cumulative diff covers every retry's code change without daemon
    state-file noise. Without `task_id` we fall back to `git show HEAD`.
    """
    if briefing_text is None:
        return None
    bullets = parse_verification_section(briefing_text)
    if bullets is None:
        return None
    if not bullets:
        return None

    t0 = time.monotonic()
    results: list[CriterionResult] = []
    diff_text: str | None = None
    for idx, b in enumerate(bullets):
        if b.kind == "shell":
            results.append(_run_shell_bullet(
                b, project_root=project_root, timeout_s=timeout_s,
            ))
        elif b.kind == "malformed":
            # TB-219: typed-fail produced by the classifier when the
            # bullet shape can't be safely exec'd or judged (the TB-207
            # literal-backtick trap is the only producer today). Surface
            # the classifier's explanation as the criterion's `notes`
            # so the operator sees a precise rewrite suggestion in the
            # `verification_failed` event rather than a downstream
            # exec-failure shell exit code. The execution paths
            # themselves (`_run_shell_bullet`, `_judge_prose_bullet`)
            # stay unchanged — this is a dispatcher-only branch.
            results.append(CriterionResult(
                bullet=b.text, kind=b.kind, status="fail",
                notes=b.command_error or "malformed bullet",
            ))
        else:
            if sdk is None:
                results.append(CriterionResult(
                    bullet=b.text, kind="prose", status="unverified",
                    notes="no SDK provided to judge prose bullet",
                ))
                continue
            if diff_text is None:
                diff_text = _cumulative_task_diff(project_root, task_id)
            results.append(await _judge_prose_bullet(
                b, project_root=project_root, sdk=sdk, diff_text=diff_text,
                events_file=events_file, task_id=task_id, bullet_idx=idx,
            ))

    return VerifyVerdict(
        overall=_aggregate(results),
        criteria=results,
        duration_s=time.monotonic() - t0,
    )

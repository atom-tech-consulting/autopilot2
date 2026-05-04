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
    kind: str  # "shell" | "prose"
    text: str  # full bullet text (for prose)
    command: str | None = None  # extracted shell command, if shell


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


def _list_item_leading_codespan(item: dict) -> str | None:
    """If the list_item's first inline child is a codespan, return its raw
    content with surrounding backticks stripped (handles both
    `` `cmd` `` → "cmd" and `` ` `cmd` ` `` → "`cmd`" → "cmd"). Else None.
    """
    inlines: list[dict] = []
    for ch in item.get("children", []) or []:
        if ch.get("type") in ("block_text", "paragraph"):
            inlines = ch.get("children", []) or []
            break
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
            cmd = _list_item_leading_codespan(item)
            if cmd:
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
    """
    prompt = (
        "You are evaluating ONE acceptance bullet from a task's verification "
        "section against the agent's CUMULATIVE diff (every code commit "
        "across any retries of this task, with daemon state-file noise "
        "filtered out) AND the project's working tree at HEAD. Answer with "
        "ONE LINE of JSON: "
        '{"status": "pass" | "fail", "rationale": "<one sentence>"}. '
        "Do not include any other text outside that JSON line.\n\n"
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
        f"Cumulative diff:\n```\n{diff_text[:100_000]}\n```\n"
    )
    try:
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
            extra_args={"effort": os.environ.get("AP2_AGENT_EFFORT", "xhigh")},
        )
        text = ""
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
    except Exception as e:  # noqa: BLE001
        return CriterionResult(
            bullet=bullet.text, kind="prose", status="unverified",
            notes=f"judge error: {type(e).__name__}: {e}",
        )

    return _parse_judge_response(bullet.text, text)


def _parse_judge_response(bullet_text: str, response: str) -> CriterionResult:
    """Extract the JSON verdict from the judge's reply.

    The response should be `{"status": ..., "rationale": ...}` on one line.
    Tolerates extra text by extracting the first balanced `{...}` substring.
    """
    if not response:
        return CriterionResult(
            bullet=bullet_text, kind="prose", status="unverified",
            notes="empty judge response",
        )
    # Find the first JSON object in the response.
    start = response.find("{")
    end = response.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return CriterionResult(
            bullet=bullet_text, kind="prose", status="unverified",
            notes=f"no JSON object in response: {response[:200]!r}",
        )
    try:
        data = json.loads(response[start:end + 1])
    except json.JSONDecodeError:
        return CriterionResult(
            bullet=bullet_text, kind="prose", status="unverified",
            notes=f"malformed JSON: {response[start:end + 1][:200]!r}",
        )
    status = str(data.get("status", "")).lower().strip()
    rationale = str(data.get("rationale", "")).strip()
    if status not in ("pass", "fail"):
        return CriterionResult(
            bullet=bullet_text, kind="prose", status="unverified",
            notes=f"unknown status {status!r}; rationale: {rationale[:200]}",
        )
    return CriterionResult(
        bullet=bullet_text, kind="prose", status=status,
        notes=rationale,
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
    for b in bullets:
        if b.kind == "shell":
            results.append(_run_shell_bullet(
                b, project_root=project_root, timeout_s=timeout_s,
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
            ))

    return VerifyVerdict(
        overall=_aggregate(results),
        criteria=results,
        duration_s=time.monotonic() - t0,
    )

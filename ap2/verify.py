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

import mistune


# Mistune AST parser — used for `## Verification` section detection and bullet
# classification (TB-102). Replaces the regex tower (`_VERIFICATION_HEADER_RE`,
# `_BULLET_RE`, `_SHELL_LEAD_RE`, `_SHELL_DOUBLE_RE`) that successively bit us:
#   - TB-91: `\s*$` rejected `## Verification (launch-task — ...)` headings
#   - TB-146 round 1: same regex variant
#   - TB-146 round 2: shell-extraction regex didn't handle double-backtick
#     code spans (`` ` `cmd` ` ``)
# Markdown is rich enough that ad-hoc regex against it is a losing
# arms race. mistune's AST handles all of these natively.
_MD = mistune.create_markdown(renderer="ast")


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
    nodes = _MD(briefing_text) or []
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
        proc = subprocess.run(
            bullet.command,
            shell=True,
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


async def _judge_prose_bullet(
    bullet: VerifyBullet,
    *,
    project_root: Path,
    sdk,
    diff_text: str,
) -> CriterionResult:
    """Ask the SDK whether `bullet.text` is satisfied by `diff_text`.

    Asks for a structured one-line JSON response; falls back to `unverified`
    on parse failure rather than failing the whole verification (the prose
    judge is best-effort).
    """
    prompt = (
        "You are evaluating ONE acceptance bullet from a task's verification "
        "section against the agent's diff. Answer with ONE LINE of JSON: "
        '{"status": "pass" | "fail", "rationale": "<one sentence>"}. '
        "Do not include any other text.\n\n"
        f"Bullet:\n  {bullet.text}\n\n"
        f"Diff:\n```\n{diff_text[:8000]}\n```\n"
    )
    try:
        # Build the same kind of options used elsewhere; allowed_tools=[] —
        # the judge doesn't need to take action, just evaluate.
        options = sdk.ClaudeAgentOptions(
            cwd=str(project_root),
            allowed_tools=[],
            permission_mode="bypassPermissions",
            max_turns=1,
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


async def verify_task(
    *,
    briefing_text: str | None,
    project_root: Path,
    timeout_s: int = 300,
    sdk=None,
) -> VerifyVerdict | None:
    """Run the per-task verifier. Returns None when there's nothing to check.

    Skip cases that map to None:
      - `briefing_text` is None (legacy task, no briefing).
      - The briefing has no `## Verification` section.
      - The section is present but has no bullets.

    The skip cases let pre-TB-69 tasks proceed unchanged through the daemon.
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
                diff_text = _git_show_head(project_root)
            results.append(await _judge_prose_bullet(
                b, project_root=project_root, sdk=sdk, diff_text=diff_text,
            ))

    return VerifyVerdict(
        overall=_aggregate(results),
        criteria=results,
        duration_s=time.monotonic() - t0,
    )

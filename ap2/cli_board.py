"""Board-mutation CLI handlers (TB-264 split from `ap2/cli.py`).

Owns the verbs that mutate TASKS.md (or queue mutations through the
operator queue for the daemon's drain to apply):

  - `cmd_add`        — create a new task from a briefing markdown file
                       (or $EDITOR fallback, TB-135).
  - `cmd_update`     — in-place edit of an existing task's title / tags /
                       description / @blocked codespan and/or briefing
                       (TB-153).
  - `cmd_backlog`    — move any task into Backlog (TB-77 — the verb that
                       replaces the older `cmd_skip`).
  - `cmd_unfreeze`   — move a Frozen task to Backlog + reset retry counter.
  - `cmd_delete`     — permanently remove a task; refuses Active/Ready
                       without --force; emits `task_deleted` for audit.
  - `cmd_reject`     — reject an ideation-proposed task with captured
                       reason → operator_log.md (TB-152).
  - `cmd_approve`    — strip `@blocked:review` so an ideation-proposed
                       task auto-promotes out of Backlog (TB-121).
  - `cmd_classify`   — record an operator's retrospective impact verdict
                       on a shipped proposal (TB-189).

Every verb routes through the operator queue (`tools.do_operator_queue_append`)
so mutations land at a tick boundary, never mid-task-run (TB-131's anti-race
contract).

Briefing-parsing helpers (`_compose_briefing_via_editor`,
`_parse_briefing_metadata`, `_read_briefing_file`) live here too because
they're used only by the board-mutation surface (cmd_add / cmd_update). The
`_read_briefing_file` helper is also imported by `cli_review.cmd_update_goal`
— the file-or-stdin shape is the same as the briefing path arg.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from .board import _norm_tag
from .config import Config
from . import tools


_BRIEFING_TEMPLATE_HINT = (
    "ap2 add: --briefing-file is required (or set $EDITOR for the\n"
    "  git-commit-style editor flow).\n"
    "  Author a briefing markdown file first (H1 sets the title, an\n"
    "  optional `Tags: #foo #bar` line sets tags), then re-run as:\n"
    "      ap2 add                                    # opens $EDITOR with the template\n"
    "      ap2 add --briefing-file <path>             # from a file\n"
    "      ap2 add --briefing-file -                  # from stdin\n"
    "  See ap2/init.py:BRIEFING_TEMPLATE for the canonical shape; the\n"
    "  daemon's per-task verifier (TB-69) reads `## Verification` from\n"
    "  this file to score the task — a missing briefing means no\n"
    "  scope-specific verification (TB-135)."
)


# TB-135: editor-driven authoring (git-commit-style). When `ap2 add` is
# invoked without `--briefing-file`, fall back to opening $EDITOR against
# this template and use the saved buffer as the briefing. Aborting the
# editor (empty save, unchanged template, or non-zero exit) makes
# `ap2 add` exit non-zero without mutating TASKS.md. The template is
# intentionally distinct from `ap2.init.BRIEFING_TEMPLATE`: that one is
# rendered post-add (TB-N is known) and used by the daemon-prep flow,
# whereas this one is pre-add (TB-N is allocated *after* the briefing
# parses) so the H1 has a placeholder rather than `{task_id}`.
_EDITOR_TEMPLATE = (
    "# (your title here — single line; no `TB-N` prefix, the daemon allocates the id)\n\n"
    "Tags: #area #kind\n\n"
    "## Goal\n\n"
    "(one paragraph — what success looks like, why this matters)\n\n"
    "## Scope\n\n"
    "- (file / module to change)\n\n"
    "## Design\n\n"
    "(how this will be built — surface, data flow, edge cases)\n\n"
    "## Verification\n\n"
    "Concrete acceptance criteria the daemon's per-task verifier (TB-69)\n"
    "runs after the agent's commit. Shell-command bullets (backtick-fenced\n"
    "at the start of the bullet) are run automatically; prose bullets are\n"
    "judged by an SDK call against the diff.\n\n"
    "- `uv run pytest -q` — full suite passes\n"
    "- (one or more concrete shell or prose bullets the verifier can score)\n\n"
    "## Out of scope\n\n"
    "- (what's explicitly NOT in this task)\n"
)


def _compose_briefing_via_editor() -> str | None:
    """Open $EDITOR against `_EDITOR_TEMPLATE`; return the edited buffer.

    Mirrors `git commit`'s editor-driven message authoring. Returns the
    edited text on a clean save, or `None` if the operator aborted (any
    of: $EDITOR unset, editor exited non-zero, saved buffer was empty,
    or the buffer was unchanged from the template). The caller treats
    `None` as a user abort and exits non-zero without mutating the
    board — same contract as `git commit` aborting on an empty commit
    message.

    The temp file lives under tempfile.gettempdir() (so the operator's
    swap files don't pollute the project tree) and is removed regardless
    of editor exit. The full path is passed to the editor as a single
    argv element so paths with spaces survive.
    """
    editor = os.environ.get("EDITOR", "").strip()
    if not editor:
        return None
    import shlex
    import tempfile

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", delete=False, prefix="ap2-briefing-",
    ) as tf:
        tf.write(_EDITOR_TEMPLATE)
        tmp_path = tf.name
    try:
        # `$EDITOR` is canonically a shell-tokenized command (e.g.
        # `vim -p` or `code --wait`), so split it the way git does.
        cmd = shlex.split(editor) + [tmp_path]
        try:
            rc = subprocess.call(cmd)
        except (FileNotFoundError, OSError):
            return None
        if rc != 0:
            return None
        text = Path(tmp_path).read_text()
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    if not text.strip():
        return None
    if text == _EDITOR_TEMPLATE:
        # Operator saved without changes — treat as an abort, same as
        # `git commit` refusing an empty/unchanged commit message.
        return None
    return text


# TB-135: convention parse — title from H1, tags from a `Tags:` line
# (case-insensitive). Any leading `TB-N — ` on the H1 is stripped because
# TB-N is allocated AFTER add — what's on disk pre-add is the bare title.
# `Tags:` accepts either `#a #b` or `a, b` shapes; both round-trip onto
# the rendered task line. YAML frontmatter (TB-133's job) takes precedence
# if it eventually lands; this fallback handles the no-frontmatter case
# the briefing's "Out of scope" section calls out.
_TITLE_TBN_RE = re.compile(r"^TB-\d+\s*[—\-:]\s*", re.IGNORECASE)
_TAG_TOKEN_RE = re.compile(r"#?([A-Za-z0-9][A-Za-z0-9_\-]*)")


def _parse_briefing_metadata(text: str) -> tuple[str, list[str]]:
    """Pull (title, tags) out of a briefing markdown buffer.

    Title: first non-empty line beginning with `# ` (H1). Strip a leading
    `TB-N — ` if present so a re-add or daemon-prepped briefing doesn't
    bake the prior id into the new task line. Empty/missing → "" so the
    caller can surface a clear error.

    Tags: first line matching `^Tags:` (case-insensitive). Tokens are
    `#`-prefixed words OR comma/whitespace-separated words; each tag is
    normalized to `#<word>` so the rendered task line is uniform.
    """
    title = ""
    tags: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not title and line.startswith("# "):
            title = _TITLE_TBN_RE.sub("", line[2:].strip())
        if not tags and line.lower().startswith("tags:"):
            payload = line.split(":", 1)[1]
            tags = [
                f"#{m.group(1).lower()}"
                for m in _TAG_TOKEN_RE.finditer(payload)
            ]
        if title and tags:
            break
    return title, tags


def _read_briefing_file(arg: str) -> str:
    """`--briefing-file -` reads stdin; otherwise reads the path."""
    if arg == "-":
        return sys.stdin.read()
    return Path(arg).read_text()


def cmd_add(cfg: Config, args: argparse.Namespace) -> int:
    section = args.section.capitalize()
    section_map = {
        "Ready": "add_ready",
        "Backlog": "add_backlog",
        "Frozen": "add_frozen",
    }
    op = section_map.get(section)
    if not op:
        print(f"unknown section: {args.section}", file=sys.stderr)
        return 2
    # TB-135: briefing authoring is now required. Resolution order:
    #   1. `--briefing-file <path>` (or `-` for stdin) — explicit caller
    #      contract; what scripts and the ap2-task skill use.
    #   2. `$EDITOR` — git-commit-style fallback when neither is set:
    #      open the template in $EDITOR, use the saved buffer. Aborting
    #      the editor (empty save, unchanged template, non-zero exit)
    #      drops through to the usage hint and exits non-zero without
    #      mutating TASKS.md.
    #   3. Neither — print the usage hint, exit 1.
    # The auto-fill skeleton path (which produced briefings whose
    # `## Verification` had only a placeholder bullet) is gone — without
    # a real Verification section the per-task verifier has nothing
    # scope-specific to score against.
    if args.briefing_file:
        try:
            briefing = _read_briefing_file(args.briefing_file)
        except OSError as e:
            print(f"ap2 add: {e}", file=sys.stderr)
            return 1
    else:
        briefing = _compose_briefing_via_editor() or ""
        if not briefing:
            print(_BRIEFING_TEMPLATE_HINT, file=sys.stderr)
            return 1
    if not briefing.strip():
        print(
            "ap2 add: briefing is empty — refusing.\n"
            "  An empty briefing means no scope-specific verification "
            "(TB-135).",
            file=sys.stderr,
        )
        return 1
    parsed_title, parsed_tags = _parse_briefing_metadata(briefing)
    if not parsed_title:
        print(
            "ap2 add: briefing has no `# Title` H1 — refusing.\n"
            "  The first H1 sets the task title on TASKS.md (TB-135).",
            file=sys.stderr,
        )
        return 1

    title = parsed_title
    # Tags: union of briefing-derived (`Tags:` line) and `--tags` flag.
    # The flag is preserved as a convenience override; if both are
    # provided, the briefing's tags win and the flag's tokens are
    # appended (deduped) so neither side is silently dropped.
    tags = list(parsed_tags)
    for t in args.tags or []:
        if t not in tags:
            tags.append(t)
    # TB-134: still validate single-line for tags — a tag with embedded
    # newlines breaks TASK_LINE_RE even when the title parses cleanly.
    for tag in tags:
        err = tools._validate_single_line("tag", tag)
        if err:
            print(f"ap2 add: {err}", file=sys.stderr)
            return 1
    # --no-verify becomes a `#no-verify` tag on the task line. The daemon
    # checks for this tag in `_run_verify` to skip the project-wide gate
    # for tasks the operator already knows can't be meaningfully verified
    # by AP2_VERIFY_CMD (docs, infra, etc.). Tags survive the round-trip
    # through TASK_LINE_RE so the marker persists across daemon restarts.
    if getattr(args, "no_verify", False) and "#no-verify" not in tags:
        tags.append("#no-verify")
    # TB-132: --blocked CSV → `@blocked:<csv>` codespan on the rendered
    # task line. Validate single-line so a stray newline in the operator's
    # input doesn't smuggle TASK_LINE_RE-busting bytes onto the board.
    blocked = (getattr(args, "blocked", None) or "").strip()
    err = tools._validate_single_line("blocked", blocked)
    if err:
        print(f"ap2 add: {err}", file=sys.stderr)
        return 1
    # TB-131: stage through the operator queue rather than mutate TASKS.md
    # directly. The TB-N is pre-allocated synchronously (so we can print
    # it immediately), the briefing file is pre-written, and only the
    # TASKS.md insertion is deferred until the daemon's next tick.
    # TB-170: forward the operator-CLI `--skip-goal-alignment` opt-in onto
    # the queue payload so the queue-append-time validator (and the
    # drain-side audit line) sees the bypass intent.
    res = tools.do_operator_queue_append(
        cfg,
        {
            "op": op,
            "title": title,
            "tags": tags,
            "description": "",
            "blocked_on": blocked,
            "briefing": briefing,
            "skip_goal_alignment": bool(
                getattr(args, "skip_goal_alignment", False)
            ),
        },
    )
    if res.get("isError"):
        print(res["content"][0]["text"], file=sys.stderr)
        return 1
    msg = json.loads(res["content"][0]["text"])
    print(f"{msg.get('task_id')} (queued; will land at next tick)")
    return 0


def cmd_update(cfg: Config, args: argparse.Namespace) -> int:
    """In-place edit of an existing task (TB-153).

    Mirrors `cmd_add`'s briefing-resolution flow: `--briefing-file
    <path|->` reads the new briefing from a path or stdin (the
    file is overwritten in place — slug-stable so the briefing's
    git history stays contiguous). Other flags map onto the task's
    board-line fields:

      --title <str>          replace the task title
      --tags <csv>           replace tags (comma-separated; e.g.
                             "#foo,#bar" or "foo,bar"). Existing tags
                             are dropped — use the explicit add path
                             if you want to append.
      --blocked <csv>        replace the `@blocked:<csv>` codespan
                             (TB-N or scheme:value tokens; same
                             vocabulary as `ap2 add --blocked`).
      --description <str>    replace the description prose.
      --clear-tags           explicit clear of all tags. Distinct
                             from `--tags ""` which is ambiguous and
                             rejected (typo vs intent).
      --clear-blocked        explicit clear of the `@blocked:`
                             codespan.
      --force                allow board-line field updates on a
                             task in Active or Pipeline Pending. Has
                             no effect on briefing-content edits —
                             those are hard-refused on a running task
                             regardless, since the agent may re-read
                             its briefing mid-run.

    Omitted flag = field unchanged. At least one field must be set.

    Routes through the operator queue (`do_operator_queue_append`)
    so the mutation lands at a tick boundary, never mid-task-run —
    same anti-race rationale as `add_*` / `delete` / `unfreeze` /
    `approve`.
    """
    payload: dict[str, Any] = {"op": "update", "task_id": args.task_id}

    # --briefing-file / -. Briefing edits are optional for update
    # (unlike `cmd_add` where it's mandatory) — only read the file if
    # the flag was supplied.
    briefing: str | None = None
    if args.briefing_file:
        try:
            briefing = _read_briefing_file(args.briefing_file)
        except OSError as e:
            print(f"ap2 update: {e}", file=sys.stderr)
            return 1
        if not briefing.strip():
            print(
                "ap2 update: --briefing-file is empty — refusing.\n"
                "  Pass a non-empty briefing or omit the flag.",
                file=sys.stderr,
            )
            return 1
        payload["briefing"] = briefing

    if args.title is not None:
        payload["title"] = args.title
    if args.description is not None:
        payload["description"] = args.description

    # Tags. `--clear-tags` is the explicit-intent path; `--tags ""`
    # would be ambiguous (typo vs intentional clear), so mutually
    # exclude them at the argparse layer.
    if args.clear_tags:
        payload["clear_tags"] = True
    elif args.tags is not None:
        tags_csv = args.tags.strip()
        if not tags_csv:
            print(
                "ap2 update: --tags must be non-empty. Use --clear-tags "
                "to remove all tags.",
                file=sys.stderr,
            )
            return 1
        payload["tags"] = [
            _norm_tag(t) for t in tags_csv.split(",") if t.strip()
        ]

    # Blocked. Same explicit-clear shape as tags.
    if args.clear_blocked:
        payload["clear_blocked"] = True
    elif args.blocked is not None:
        blocked = args.blocked.strip()
        if not blocked:
            print(
                "ap2 update: --blocked must be non-empty. Use "
                "--clear-blocked to remove the @blocked: codespan.",
                file=sys.stderr,
            )
            return 1
        payload["blocked"] = blocked

    payload["force"] = bool(getattr(args, "force", False))
    # TB-170: operator-CLI bypass of TB-161 + TB-164 on briefing-content
    # edits. Only meaningful when `--briefing-file` is also set; harmless
    # otherwise (the validator only fires on briefing edits, but the
    # audit-line suffix still lands so operator intent is preserved).
    if getattr(args, "skip_goal_alignment", False):
        payload["skip_goal_alignment"] = True

    res = tools.do_operator_queue_append(cfg, payload)
    if res.get("isError"):
        print(res["content"][0]["text"], file=sys.stderr)
        return 1
    body = json.loads(res["content"][0]["text"])
    print(
        f"queued update {body.get('task_id', args.task_id)} "
        f"(will land at next tick)"
    )
    return 0


def cmd_backlog(cfg: Config, args: argparse.Namespace) -> int:
    """Move a task to Backlog from any section.

    Replaces the older `cmd_skip` (TB-77) — same code path, name now matches
    the underlying `move_to_backlog` action instead of the historical
    "skip in queue" use case.

    TB-131: routes through the operator queue. Snapshot validation runs at
    queue-append time (rejects unknown TB-N immediately); the actual
    move lands on the daemon's next tick.
    """
    res = tools.do_operator_queue_append(
        cfg, {"op": "move_to_backlog", "task_id": args.task_id}
    )
    if res.get("isError"):
        print(res["content"][0]["text"], file=sys.stderr)
        return 1
    print(f"queued move {args.task_id} → Backlog (will land at next tick)")
    return 0


def cmd_unfreeze(cfg: Config, args: argparse.Namespace) -> int:
    """Move a Frozen task back to Backlog and clear its retry counter.

    TB-131: routes through the operator queue. Snapshot validation runs at
    queue-append time so an unfreeze on a non-Frozen task is rejected
    immediately — exactly as before. The retry-counter reset + the
    `task_unfrozen` event are emitted by the daemon's drain step.
    """
    res = tools.do_operator_queue_append(
        cfg, {"op": "unfreeze", "task_id": args.task_id}
    )
    if res.get("isError"):
        print(res["content"][0]["text"], file=sys.stderr)
        return 1
    print(
        f"queued unfreeze {args.task_id} → Backlog "
        f"(retry counter will reset on drain)"
    )
    return 0


def cmd_delete(cfg: Config, args: argparse.Namespace) -> int:
    """Permanently remove a task from the board.

    Refuses to delete from Active (in-flight) or Ready (about to dispatch)
    by default — the daemon's orphan-recovery and dispatch invariants
    assume those sections aren't out from under it. Use `ap2 backlog
    <TB-N>` first to move the task somewhere safe, OR pass `--force` if
    you really mean it.

    TB-131: routes through the operator queue. The Active/Ready/Pipeline
    Pending refusal happens at queue-append time so the operator gets
    immediate feedback. The `task_deleted` event is emitted by the
    daemon's drain step (after the briefing under `.cc-autopilot/tasks/`
    is preserved on disk — git history preserves the briefing if it was
    committed; the ghost file is harmless).
    """
    res = tools.do_operator_queue_append(
        cfg,
        {
            "op": "delete",
            "task_id": args.task_id,
            "force": bool(args.force),
        },
    )
    if res.get("isError"):
        print(res["content"][0]["text"], file=sys.stderr)
        return 1
    print(f"queued delete {args.task_id} (will land at next tick)")
    return 0


def cmd_reject(cfg: Config, args: argparse.Namespace) -> int:
    """Reject an ideation-proposed task with a captured reason (TB-152).

    Mirrors `cmd_delete`'s removal semantics — the drain handler drops
    the row, removes the briefing file, and emits `task_deleted` — but
    the audit line is richer: `<ts> — rejected ideation proposal →
    TB-N (<title>): <reason>` lands in `.cc-autopilot/operator_log.md`
    so ideation Step 0 has a per-cycle signal to avoid re-proposing
    the same idea. The standard `applied operator-queued reject → TB-N`
    line is also written so the verb-vs-`delete` distinction shows up
    in the audit trail.

    Pre-validation: the verb is reserved for Backlog tasks still gated
    by `@blocked:review` (i.e. unapproved ideation proposals). For
    anything else — Active runs, already-approved Backlog tasks, Frozen
    failures, etc. — the queue-append handler refuses with a message
    pointing the operator at `ap2 delete`. Both checks live on the
    queue-append side (`do_operator_queue_append`) so the chat surface
    in `prompts.py` benefits from the same gate.

    `--reason` is optional (operator may want to reject quickly); when
    omitted the placeholder `(no reason given)` is recorded — itself a
    signal ideation can spot.
    """
    payload: dict = {"op": "reject", "task_id": args.task_id}
    if args.reason is not None:
        payload["reason"] = args.reason
    res = tools.do_operator_queue_append(cfg, payload)
    if res.get("isError"):
        print(res["content"][0]["text"], file=sys.stderr)
        return 1
    print(
        f"queued reject {args.task_id} (will land at next tick; "
        f"reason written to operator_log.md)"
    )
    return 0


def cmd_classify(cfg: Config, args: argparse.Namespace) -> int:
    """Record an operator's retrospective impact verdict on a shipped
    proposal (TB-189).

    Routes through the operator queue rather than mutating
    operator_log.md / per-proposal records directly because the daemon
    drains the queue under `board_file_lock` between tick stages — the
    same channel the other operator-authored verbs (reject / approve /
    delete / update_goal) use, so the audit trail and conflict semantics
    stay uniform. The drain-side handler:

      - Writes `<ts> — classified TB-N impact=<verdict>: <reason>` to
        operator_log.md (the standalone authoritative trail; ideation
        Step 0 reads this).
      - Appends an `impact` block to
        `.cc-autopilot/ideation_proposals/<TB-N>.json` (the structured
        signal feeding ideation's later track-record block — TB-188).
        Tolerates missing record file (legacy / non-ideation tasks).
      - Emits a `task_classified` event so events.jsonl carries the
        structured audit trail; `ap2 status` reads recent events to
        count classifications in the last 30 days.

    `--impact` is required and must be one of `IMPACT_VERDICTS` (the CLI
    exits non-zero before queueing on any other value). `--reason` is
    optional but encouraged (the verdict by itself is signal; a reason
    converts it into a learnable signal).

    Operator authority by design: there is no LLM auto-classification
    path. The operator IS the source of truth for the impact verdict —
    that's the whole point of the surface (goal.md L61-76).
    """
    if args.impact not in tools.IMPACT_VERDICTS:
        print(
            f"ap2 classify: --impact must be one of "
            f"{list(tools.IMPACT_VERDICTS)}; got {args.impact!r}",
            file=sys.stderr,
        )
        return 1
    payload: dict = {
        "op": "classify",
        "task_id": args.task_id,
        "verdict": args.impact,
    }
    if args.reason is not None:
        payload["reason"] = args.reason
    res = tools.do_operator_queue_append(cfg, payload)
    if res.get("isError"):
        print(res["content"][0]["text"], file=sys.stderr)
        return 1
    print(
        f"queued classify {args.task_id} impact={args.impact} "
        f"(will land at next tick; verdict written to operator_log.md "
        f"and the per-proposal record)"
    )
    return 0


def cmd_approve(cfg: Config, args: argparse.Namespace) -> int:
    """Strip the `(blocked on: review)` review-gate clause from a task
    (TB-121).

    Operator surface for promoting an ideation-proposed task out of its
    `@blocked:review` codespan so it auto-dispatches on the next tick.
    Routes through the operator queue so the mutation (a) lands at a
    tick boundary instead of mid-task-run (TB-142 anti-race), and (b)
    shares the drain-side `_approve_review_token` helper with both the
    Mattermost handler's `operator_queue_append({"op":"approve",...})`
    chat surface and `do_board_edit({"action":"approve",...})`.

    Snapshot validation runs at queue-append time — an unknown TB-N is
    rejected immediately. The actual codespan strip + `ideation_approved`
    audit event happen on the daemon's next tick.
    """
    res = tools.do_operator_queue_append(
        cfg, {"op": "approve", "task_id": args.task_id}
    )
    if res.get("isError"):
        print(res["content"][0]["text"], file=sys.stderr)
        return 1
    print(
        f"queued approve {args.task_id} "
        f"(review gate strip will land at next tick)"
    )
    return 0

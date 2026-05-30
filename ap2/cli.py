"""`autopilot` CLI — argparse dispatcher (TB-264 mechanical split).

Intended to be run as `python -m ap2` or via the console_scripts entrypoint.

This module owns the argparse builder + `main()` entrypoint only — every
`cmd_<verb>` handler lives in a sibling module grouped by command surface:

  - `ap2/cli_daemon.py`     — daemon lifecycle (`start`, `stop`, `status`,
                              `pause`, `resume`, `web`) + helpers
                              `_is_running`, `_require_oauth_token`,
                              `_version_string`, `_resolve_web_url`.
  - `ap2/cli_board.py`      — board mutation (`add`, `update`, `backlog`,
                              `unfreeze`, `delete`, `reject`, `approve`,
                              `classify`) + briefing parsers.
  - `ap2/cli_review.py`     — review surfaces (`audit`, `ack`, `rollback`,
                              `ideate`, `update-goal`, `backfill-proposals`)
                              + `_active_task_id`.
  - `ap2/cli_diagnostic.py` — diagnostic + cron (`doctor`, `check`, `logs`,
                              `cron list`, `cron edit`, `init`).

Each handler keeps its `(cfg: Config, args: argparse.Namespace) -> int`
signature unchanged; this module imports them by name and binds via
`parser.set_defaults(func=...)`. Tests that `from ap2.cli import cmd_X`
continue to resolve through the re-exports below — the split is API-
preserving.
"""
from __future__ import annotations

import argparse

from .config import Config
from . import sandbox, tools

# Re-export handlers + helpers so existing test imports
# (`from ap2.cli import cmd_<verb>` / `_require_oauth_token` /
# `_compose_briefing_via_editor` / `_EDITOR_TEMPLATE` / `_version_string`)
# continue to resolve. The split is API-preserving — see TB-264's briefing.
from .cli_daemon import (  # noqa: F401
    _is_running,
    _require_oauth_token,
    _resolve_web_url,
    _version_string,
    cmd_pause,
    cmd_resume,
    cmd_start,
    cmd_status,
    cmd_stop,
    cmd_web,
)
from .cli_board import (  # noqa: F401
    _BRIEFING_TEMPLATE_HINT,
    _EDITOR_TEMPLATE,
    _compose_briefing_via_editor,
    _parse_briefing_metadata,
    _read_briefing_file,
    cmd_add,
    cmd_approve,
    cmd_backlog,
    cmd_classify,
    cmd_delete,
    cmd_reject,
    cmd_unfreeze,
    cmd_update,
)
from .cli_review import (  # noqa: F401
    _active_task_id,
    _cursor_label,
    _prompt_audit_action,
    _prompt_impact_verdict,
    _queue_audit_run_cursor,
    cmd_ack,
    cmd_audit,
    cmd_backfill_proposals,
    cmd_ideate,
    cmd_rollback,
    cmd_update_goal,
)
from .cli_diagnostic import (  # noqa: F401
    _format_verification_failed_row,
    cmd_check,
    cmd_cron_edit,
    cmd_cron_list,
    cmd_doctor,
    cmd_init,
    cmd_logs,
)
from .cli_config import (  # noqa: F401
    cmd_config_get,
    cmd_config_list,
    cmd_config_set,
    cmd_config_validate,
)


def _add_mm_url_token_args(p: argparse.ArgumentParser) -> None:
    """Shared --mm-* flags for user-setup / install-mm.

    Precedence (resolved in sandbox._resolve_mm_url_token): explicit --mm-url/
    --mm-token, then --mm-url-env/--mm-token-env env-var names, then the
    caller's own MATTERMOST_URL/MATTERMOST_TOKEN from the environment.
    """
    p.add_argument("--mm-url", metavar="URL",
                   help="MATTERMOST_URL to install into ~user/.zshenv")
    p.add_argument("--mm-token", metavar="TOKEN",
                   help="MATTERMOST_TOKEN to install (prefer --mm-token-env)")
    p.add_argument("--mm-url-env", metavar="VAR",
                   help="read MATTERMOST_URL from this env var instead")
    p.add_argument("--mm-token-env", metavar="VAR",
                   help="read MATTERMOST_TOKEN from this env var instead")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="autopilot", description="Autopilot v2 CLI.")
    p.add_argument(
        "--version",
        action="version",
        version=f"ap2 {_version_string()}",
    )
    p.add_argument("--project", default=None, help="project root (default: cwd)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("start", help="start the daemon (backgrounded)")
    s.add_argument("--foreground", action="store_true", help="run in foreground")
    s.set_defaults(func=cmd_start)

    s = sub.add_parser("_run", help=argparse.SUPPRESS)
    s.set_defaults(func=lambda cfg, a: (__import__("ap2.daemon", fromlist=["run"]).run(str(cfg.project_root)) or 0))

    s = sub.add_parser("stop", help="stop the daemon")
    s.add_argument("-f", "--force", action="store_true")
    s.set_defaults(func=cmd_stop)

    s = sub.add_parser("status", help="show daemon + board status")
    s.add_argument("--json", action="store_true")
    # TB-326 sidecar: mirror the top-level `--project` on the subparser
    # so `ap2 status --project <path>` (operator-conventional, also the
    # shape briefings cite as a sanity check) works alongside the
    # documented `ap2 --project <path> status`. Matches the pattern the
    # `config` subverbs already use (cli.py L628 / L641 / L659 / L673).
    s.add_argument("--project", default=None, help="project root (default: cwd)")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser(
        "add",
        help="add a task — `--briefing-file` is required (TB-135). The "
             "title and tags are parsed from the briefing's H1 and an "
             "optional `Tags:` line; pass `-` to read the briefing from "
             "stdin.",
    )
    # Not argparse-required so cmd_add can emit a hint that points at
    # the canonical template instead of argparse's terse
    # "the following arguments are required" line (TB-135).
    s.add_argument(
        "--briefing-file",
        default=None,
        help="path to the briefing markdown file (or `-` for stdin). "
             "Required since TB-135 — the daemon's per-task verifier "
             "needs a real `## Verification` section.",
    )
    s.add_argument(
        "-s", "--section", default="Backlog",
        help="Ready|Backlog|Frozen (default: Backlog — operator-filed "
             "tasks land in triage alongside ideation proposals; the "
             "daemon auto-promotes Backlog → Ready when capacity opens. "
             "Pass `-s Ready` for prior fast-track behavior; "
             "`--blocked review` only surfaces from Backlog so leaving "
             "the default keeps review-pending tasks visible to "
             "`ap2 status` — TB-167.)",
    )
    s.add_argument(
        "-t", "--tags", nargs="*",
        help="extra tags appended to those parsed from the briefing's "
             "`Tags:` line (deduped).",
    )
    s.add_argument(
        "--no-verify",
        action="store_true",
        help="skip the AP2_VERIFY_CMD project-wide test gate for this task "
             "(adds `#no-verify` to its tags)",
    )
    # TB-132: blockers live in a `@blocked:<csv>` codespan on the task line
    # (parallel to `#tags`), not in the description prose. Comma-separated
    # tokens; each is either a TB-N task id or a `<scheme>:<value>` blocker
    # token.
    s.add_argument(
        "--blocked",
        default=None,
        metavar="CSV",
        help="comma-separated blocker tokens (TB-N or scheme:value); written "
             "as a `@blocked:<csv>` codespan on the task line so the parser "
             "never has to regex the description prose (TB-132).",
    )
    # TB-170: operator-CLI escape hatch from the TB-161 goal-cite + TB-164
    # Why-now checks. Use for legitimately-meta operator-filed work
    # (dependency bumps, doc fixes, infra maintenance) where the
    # validators were designed for ideation's human-out-of-the-loop case
    # and shouldn't fire on a one-line typo fix. ALL OTHER validations
    # (canonical Goal/Scope/Design/Verification/Out-of-scope, parseable +
    # non-empty Verification, single-line title/tags/description) keep
    # firing.
    s.add_argument(
        "--skip-goal-alignment",
        action="store_true",
        help="bypass the TB-161 goal-cite + TB-164 Why-now checks for "
             "this operator-filed task (TB-170). Use for legitimately-"
             "meta work (dependency bumps, doc fixes, infra "
             "maintenance) where manufacturing goal-alignment prose "
             "would be ceremony for its own sake. Other validations "
             "still apply; the operator_log.md audit line is decorated "
             "with `(goal-alignment check skipped)` so ideation Step 0 "
             "can spot bypassed tasks.",
    )
    s.set_defaults(func=cmd_add)

    s = sub.add_parser("init", help="scaffold gitignores + .cc-autopilot/tasks/ (idempotent)")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("doctor", help="check ap2 readiness (project skeleton + sandbox)")
    s.add_argument("--user", default=None, help="sandbox user (default: claude-agent)")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser(
        "check",
        help="check on-disk state-file integrity: TASKS.md shape, "
             "briefing-link resolution, cron.yaml schema, JSON state "
             "parseability, insights front matter (TB-108). Exits 1 on "
             "errors; warnings don't fail.",
    )
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.set_defaults(func=cmd_check)

    s = sub.add_parser("logs", help="show recent events")
    s.add_argument("-n", type=int, default=40)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_logs)

    s = sub.add_parser("backlog", help="move a task to Backlog from any section")
    s.add_argument("task_id")
    s.set_defaults(func=cmd_backlog)

    s = sub.add_parser(
        "unfreeze",
        help="move a Frozen task to Backlog + clear its retry counter "
             "(refuses if the task isn't currently in Frozen)",
    )
    s.add_argument("task_id")
    s.set_defaults(func=cmd_unfreeze)

    s = sub.add_parser(
        "delete",
        help="permanently remove a task from the board (refuses Active/"
             "Ready without --force; emits task_deleted event for audit)",
    )
    s.add_argument("task_id")
    s.add_argument("-f", "--force", action="store_true",
                   help="allow deletion from Active or Ready (use with care)")
    s.set_defaults(func=cmd_delete)

    s = sub.add_parser(
        "update",
        help="in-place edit a task's title / tags / description / "
             "@blocked codespan and/or its briefing file (TB-153). "
             "Routes through the operator queue so the mutation lands "
             "at a tick boundary, never mid-task-run. Omitted flag = "
             "field unchanged.",
    )
    s.add_argument("task_id", help="TB-N to update")
    s.add_argument("--title", default=None, help="replace task title")
    s.add_argument(
        "--tags",
        default=None,
        metavar="CSV",
        help="replace tags (comma-separated, e.g. `#foo,#bar` or "
             "`foo,bar`). Use --clear-tags to remove all tags.",
    )
    s.add_argument(
        "--blocked",
        default=None,
        metavar="CSV",
        help="replace the `@blocked:<csv>` codespan (TB-N or "
             "scheme:value tokens). Use --clear-blocked to remove the "
             "codespan entirely.",
    )
    s.add_argument(
        "--description",
        default=None,
        help="replace description prose on the task line",
    )
    s.add_argument(
        "--clear-tags",
        action="store_true",
        help="explicit clear of all tags (vs. ambiguous --tags '')",
    )
    s.add_argument(
        "--clear-blocked",
        action="store_true",
        help="explicit clear of the @blocked: codespan",
    )
    s.add_argument(
        "--briefing-file",
        default=None,
        metavar="PATH",
        help="path to the new briefing markdown (or `-` for stdin). "
             "The existing briefing file is overwritten in place "
             "(slug-stable so git history of the briefing stays "
             "contiguous).",
    )
    s.add_argument(
        "--force",
        action="store_true",
        help="allow board-line field updates on a task in Active or "
             "Pipeline Pending. Has no effect on briefing-content "
             "edits — those are hard-refused on a running task "
             "regardless.",
    )
    # TB-170: same operator-CLI escape hatch as `ap2 add`. Only meaningful
    # when the update carries a `--briefing-file` edit (the validator
    # only fires on briefing-content changes); for board-line-only
    # updates (title / tags / blocked / description) the flag is a
    # no-op but the audit-line suffix still lands so the operator's
    # intent is preserved in the log.
    s.add_argument(
        "--skip-goal-alignment",
        action="store_true",
        help="bypass the TB-161 goal-cite + TB-164 Why-now checks on "
             "the briefing-content edit for this update (TB-170). "
             "Operator-CLI-only escape hatch; mirrors `ap2 add "
             "--skip-goal-alignment`.",
    )
    s.set_defaults(func=cmd_update)

    s = sub.add_parser(
        "approve",
        help="approve an ideation-proposed task (TB-121): strips the "
             "`@blocked:review` codespan so the task auto-promotes out "
             "of Backlog on the next tick. Refuses if the task isn't on "
             "the board.",
    )
    s.add_argument("task_id")
    s.set_defaults(func=cmd_approve)

    s = sub.add_parser(
        "reject",
        help="reject an ideation-proposed task (TB-152): drops the row "
             "and briefing file (same removal as `delete`) AND writes "
             "`rejected ideation proposal → TB-N (<title>): <reason>` to "
             "operator_log.md so ideation Step 0 learns to avoid "
             "re-proposing it. Reserved for Backlog tasks still gated "
             "by `@blocked:review`; for anything else use `ap2 delete`.",
    )
    s.add_argument("task_id")
    s.add_argument(
        "--reason",
        default=None,
        help="single-line reason captured in operator_log.md. Omit for "
             "a quick reject — `(no reason given)` is recorded as a "
             "placeholder, itself a signal to ideation.",
    )
    s.set_defaults(func=cmd_reject)

    s = sub.add_parser(
        "classify",
        help="record an operator's retrospective impact verdict on a "
             "shipped proposal (TB-189): writes "
             "`<ts> — classified TB-N impact=<verdict>: <reason>` to "
             "operator_log.md AND appends an `impact` block to the "
             "per-proposal record from TB-188. The operator-authored "
             "signal stream goal.md L61-76 anchors signal collection "
             "to. Routed through the operator queue.",
    )
    s.add_argument("task_id")
    s.add_argument(
        "--impact",
        required=True,
        choices=list(tools.IMPACT_VERDICTS),
        help="operator's verdict on the proposal's impact (one of: "
             f"{', '.join(tools.IMPACT_VERDICTS)}). `advanced-goal` = "
             "the proposal substantively moved the goal forward; "
             "`pro-forma` = it satisfied validators but did not move "
             "the goal forward (the failure mode goal.md L66-76 names) "
             "— no-impact + no-harm; `negative` = the proposal actively "
             "regressed something or made the codebase worse (a "
             "regression slipped through, test coverage was inadvertently "
             "weakened, a refactor increased complexity beyond the "
             "briefing's intent) — no-impact + harm (TB-251); `unclear` "
             "= impact not yet legible.",
    )
    s.add_argument(
        "--reason",
        default=None,
        help="single-line reason captured in operator_log.md and the "
             "per-proposal record's `impact.reason`. Optional but "
             "encouraged — the verdict by itself is signal; a reason "
             "converts it into a learnable signal.",
    )
    s.set_defaults(func=cmd_classify)

    s = sub.add_parser(
        "audit",
        help="retrospective review of unreviewed Complete + Frozen "
             "tasks since the last `ap2 audit` cursor (TB-248). Default "
             "prints a table; `--interactive` walks the list one task "
             "at a time with [c]lassify / [s]kip / [n]ext / [q]uit "
             "prompts. State derives from `operator_log.md` grep "
             "(`classified TB-N` / `audit-skipped TB-N` / `rejected "
             "TB-N` reviewed-set + `ran audit (...)` cursor); no new "
             "state file. Closes the retrospective review surface gap "
             "the auto-approve path opens — under `AP2_AUTO_APPROVE=1` "
             "this is the operator's ONLY judgment surface, so pair "
             "with `--auto-approved-only` for the after-walk-away "
             "review workflow.",
    )
    s.add_argument(
        "--interactive",
        action="store_true",
        help="walk through each unreviewed task one at a time with a "
             "[c]lassify / [s]kip / [n]ext / [q]uit prompt. `c` "
             "queues `ap2 classify` (asks for verdict + reason); `s` "
             "queues an `audit_skip` op (asks for an optional reason); "
             "`n` advances without recording; `q` exits and records a "
             "`ran audit (reviewed M, skipped K, deferred L)` cursor "
             "line. Rollback as an in-walk action is deliberately "
             "out-of-scope this iteration — use `ap2 rollback` "
             "outside the walk if needed.",
    )
    s.add_argument(
        "--json",
        action="store_true",
        help="emit the unreviewed list as JSON (for scripting / "
             "external dashboards consuming `ap2 audit --json`). "
             "Mirrors the table columns plus a `cursor` + `filter` "
             "context block.",
    )
    s.add_argument(
        "--since",
        default=None,
        metavar="ISO-DATE",
        help="override the natural audit cursor — useful for "
             "re-reviewing a window (e.g. `--since 2026-04-01T00:00:00Z` "
             "to revisit last month). When omitted, the most recent "
             "`<ts> — ran audit (...)` line in operator_log.md is "
             "used; when no such line exists, all shipped tasks are "
             "listed.",
    )
    grp = s.add_mutually_exclusive_group()
    grp.add_argument(
        "--frozen-only",
        action="store_true",
        help="restrict the list to Frozen tasks (operator triaging the "
             "freeze pile — Frozen tasks are the highest-signal review "
             "candidates because they've already cost agent attempts).",
    )
    grp.add_argument(
        "--auto-approved-only",
        action="store_true",
        help="restrict the list to tasks the daemon auto-promoted "
             "via the `AP2_AUTO_APPROVE` path (identified by an "
             "`auto_approved` event in events.jsonl). The natural "
             "filter for the after-walk-away review workflow: shows "
             "what shipped without operator-in-the-loop review at "
             "dispatch time.",
    )
    s.set_defaults(func=cmd_audit)

    s = sub.add_parser(
        "ideate",
        help="manually trigger an ideation pass (TB-159): bypasses the "
             "natural empty-board / cooldown / `AP2_IDEATION_DISABLED` "
             "gates. Routed through the operator queue; the daemon "
             "runs ideation on its next tick (≤30s). TB-194: queues "
             "regardless of board state — the prior Active-task "
             "refusal was guarding a race the loop topology already "
             "prevents (drain runs before task dispatch, with Active "
             "cleared by the previous tick's synchronous `run_task`). "
             "The natural cooldown clock still bumps after the forced "
             "run, so back-to-back `ap2 ideate` calls don't lap the "
             "next cron-driven fire.",
    )
    s.add_argument(
        "--force",
        action="store_true",
        help="TB-194: no-op for the routing decision (kept on the "
             "queue payload as audit metadata only). Pre-TB-194 this "
             "overrode an at-append-time Active-task refusal that "
             "has since been removed; the flag is preserved for one "
             "release so callers passing it don't break.",
    )
    s.set_defaults(func=cmd_ideate)

    s = sub.add_parser(
        "update-goal",
        help="refresh `goal.md` via the operator queue (TB-193): "
             "queues a full-file replacement for the daemon to apply "
             "at the next tick (≤30s) under `board_file_lock`. "
             "Symmetric to `ap2 add --briefing-file` — pass --file "
             "<path> (or `-` for stdin) to read the new goal content. "
             "Operator-CLI-only by design; the MM handler has no path "
             "to mutate goal.md.",
    )
    s.add_argument(
        "--file",
        required=True,
        metavar="PATH",
        help="path to the new goal.md content (or `-` to read from "
             "stdin). Empty / whitespace-only payloads are rejected.",
    )
    s.add_argument(
        "--reason",
        default=None,
        help="single-line reason captured in operator_log.md as "
             "`<ts> — operator updated goal.md (<reason>)`. Future "
             "ideation cycles read this as a goal-drift signal.",
    )
    s.set_defaults(func=cmd_update_goal)

    s = sub.add_parser(
        "rollback",
        help="linear rollback (TB-111): walk back from HEAD by N tasks "
             "(or to a specific TB-N / sha) and `git reset --hard`. "
             "Restores TASKS.md + every committed state file coherently. "
             "Refuses dirty working tree by default.",
    )
    grp = s.add_mutually_exclusive_group()
    grp.add_argument("-n", type=int, default=None,
                     help="roll back the last N task-completions (default: 1)")
    grp.add_argument("--task", metavar="TB-N",
                     help="roll back to before TB-N (linear: undoes everything "
                          "between HEAD and TB-N too)")
    grp.add_argument("--to", metavar="SHA",
                     help="reset to an explicit ancestor sha")
    s.add_argument("-y", "--yes", action="store_true",
                   help="skip the interactive confirm prompt")
    s.add_argument("--force", action="store_true",
                   help="proceed even with a dirty working tree (will discard)")
    s.set_defaults(func=cmd_rollback)

    s = sub.add_parser(
        "backfill-proposals",
        help="backfill historical ideation proposal records (TB-195): "
             "scans operator_log.md + briefing files + events.jsonl and "
             "writes per-proposal records for every ideation-authored "
             "TB-N that lacks one. Idempotent; safe to re-run. "
             "Operator-driven one-off — not exposed via the operator "
             "queue or daemon ticks.",
    )
    s.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would be written without touching disk",
    )
    s.set_defaults(func=cmd_backfill_proposals)

    s = sub.add_parser("pause", help="pause the daemon (sets a flag)")
    s.add_argument("--reason", default="")
    s.set_defaults(func=cmd_pause)

    s = sub.add_parser("resume", help="clear the pause flag")
    s.set_defaults(func=cmd_resume)

    s = sub.add_parser(
        "ack",
        help="record an operator-decision in .cc-autopilot/operator_log.md "
             "(TB-106) so ideation stops re-proposing actions whose effects "
             "aren't filesystem-visible",
    )
    s.add_argument("note", help="the decision or action to record (one sentence)")
    s.add_argument("-t", "--task", default=None,
                   help="optional TB-N this ack relates to")
    s.set_defaults(func=cmd_ack)

    s = sub.add_parser(
        "web",
        help="start a local read-only web UI for status + events "
             "(127.0.0.1 by default; no auth — local-only)",
    )
    s.add_argument("--host", default="127.0.0.1",
                   help="bind address (default: 127.0.0.1)")
    s.add_argument("--port", type=int, default=7820,
                   help="bind port (default: 7820); enumeration start — "
                        "if busy, walks forward up to 10 ports (TB-155)")
    s.set_defaults(func=cmd_web)

    s = sub.add_parser(
        "config",
        help="introspect / mutate `.cc-autopilot/config.toml` "
             "(TB-324). Subverbs: `list` (enumerate every key with "
             "value + source — file / env-override / default), `get "
             "<path>` (single-key lookup), `set <path> <value>` "
             "(operator-queue-routed write; lands at the next daemon "
             "tick), `validate` (dry-run schema check).",
    )
    sub_config = s.add_subparsers(dest="config_cmd", required=True)
    sc = sub_config.add_parser(
        "list",
        help="enumerate every config key + current value + source "
             "(file / env-override / default)",
    )
    sc.add_argument("--project", default=None,
                    help="project root (default: cwd). Mirrors the "
                         "top-level `--project` so operators can use "
                         "the conventional `ap2 config list --project "
                         "<path>` invocation order.")
    sc.add_argument("--json", action="store_true",
                    help="emit JSON instead of a text table")
    sc.set_defaults(func=cmd_config_list)
    sc = sub_config.add_parser(
        "get",
        help="print the current value at <path> "
             "(e.g. components.janitor.disabled)",
    )
    sc.add_argument("--project", default=None, help="project root (default: cwd)")
    sc.add_argument("path",
                    help="dotted config path "
                         "(core.<field> | components.<name>.<key>)")
    sc.add_argument("--strict", action="store_true",
                    help="exit non-zero when <path> is unknown "
                         "(default: exit 0 with the error message + "
                         "did-you-mean on stderr; the bad path is "
                         "always named verbatim so an operator who "
                         "pasted a typo can correlate). Use --strict "
                         "for shell pipelines that want fail-fast on a "
                         "typo'd path.")
    sc.set_defaults(func=cmd_config_get)
    sc = sub_config.add_parser(
        "set",
        help="queue a `config_set` op for the daemon to apply at the "
             "next tick — writes <value> to <path> in config.toml",
    )
    sc.add_argument("--project", default=None, help="project root (default: cwd)")
    sc.add_argument("path",
                    help="dotted config path "
                         "(core.<field> | components.<name>.<key>)")
    sc.add_argument("value",
                    help="new value (parsed against the schema's "
                         "declared type — bool: 1/0/true/false/yes/no, "
                         "int / float: numeric literal, str: verbatim)")
    sc.set_defaults(func=cmd_config_set)
    sc = sub_config.add_parser(
        "validate",
        help="dry-run schema check on the current config.toml + env "
             "overlay (same check the daemon runs at startup)",
    )
    sc.add_argument("--project", default=None, help="project root (default: cwd)")
    sc.set_defaults(func=cmd_config_validate)

    s = sub.add_parser("cron", help="cron utilities")
    sub_cron = s.add_subparsers(dest="cron_cmd", required=True)
    sc = sub_cron.add_parser("list", help="list cron jobs")
    sc.set_defaults(func=cmd_cron_list)
    # TB-146 + TB-202: operator-CLI-only cron registry mutation; agents
    # never have `cron_edit` in their toolset. The TB-202 refuse-if-active
    # gate lives in `cmd_cron_edit` so a mid-task invocation doesn't race
    # the fenced cron.yaml write against the task agent's snapshot window.
    sc = sub_cron.add_parser(
        "edit",
        help="add / remove / update a cron job (operator-CLI-only; "
             "TB-146 retired the agent-side cron_edit tool)",
    )
    sc.add_argument("action", choices=["add", "remove", "update"])
    sc.add_argument("name", help="cron job name")
    sc.add_argument("--interval", default=None,
                    help="interval string (e.g. '1h', '30m', '1d')")
    sc.add_argument("--prompt", default=None, help="prompt body")
    sc.add_argument("--active-when", dest="active_when", default=None,
                    help="optional active_when condition")
    sc.add_argument("--max-turns", dest="max_turns", default=None,
                    help="optional max-turns cap (default 15)")
    sc.set_defaults(func=cmd_cron_edit)

    s = sub.add_parser("sandbox", help="OS-level sandbox user + project helpers")
    s.set_defaults(func=lambda cfg, a: (s.print_help() or 0))
    sub_sbx = s.add_subparsers(dest="sbx_cmd")

    sc = sub_sbx.add_parser("user-audit", help="verify sandbox user has no creds")
    sc.add_argument("user", nargs="?", default=sandbox.DEFAULT_USER)
    sc.set_defaults(func=sandbox.cmd_user_audit)

    sc = sub_sbx.add_parser("user-setup", help="create sandbox user (prompts before running sudo)")
    sc.add_argument("user", nargs="?", default=sandbox.DEFAULT_USER)
    sc.add_argument("-y", "--yes", action="store_true", help="skip confirmation prompt")
    sc.add_argument("--skip-token", action="store_true",
                    help="don't prompt for CLAUDE_CODE_OAUTH_TOKEN post-creation")
    sc.add_argument("--skip-statusline", action="store_true",
                    help="don't install the project's statusline into ~user/.claude/")
    _add_mm_url_token_args(sc)
    sc.set_defaults(func=sandbox.cmd_user_setup)

    sc = sub_sbx.add_parser(
        "install-token",
        help="install CLAUDE_CODE_OAUTH_TOKEN into ~<user>/.zshenv "
             "(obtain via `claude setup-token`)",
    )
    sc.add_argument("user", nargs="?", default=sandbox.DEFAULT_USER)
    sc.add_argument("--token-env", metavar="VAR",
                    help="read token from this env var instead of prompting")
    sc.set_defaults(func=sandbox.cmd_install_token)

    sc = sub_sbx.add_parser(
        "install-statusline",
        help="copy hooks/statusline-command.sh into ~<user>/.claude/ + "
             "wire it into ~<user>/.claude/settings.json",
    )
    sc.add_argument("user", nargs="?", default=sandbox.DEFAULT_USER)
    sc.set_defaults(func=sandbox.cmd_install_statusline)

    sc = sub_sbx.add_parser(
        "install-mm",
        help="install MATTERMOST_URL + MATTERMOST_TOKEN into ~<user>/.zshenv",
    )
    sc.add_argument("user", nargs="?", default=sandbox.DEFAULT_USER)
    _add_mm_url_token_args(sc)
    sc.set_defaults(func=sandbox.cmd_install_mm)

    sc = sub_sbx.add_parser("project-setup", help="clone <source> into ~<user>/repos/")
    sc.add_argument("source", help="path to the source repo (human's clone)")
    sc.add_argument("--user", default=sandbox.DEFAULT_USER)
    sc.add_argument("-y", "--yes", action="store_true", help="skip confirmation prompt")
    sc.add_argument("--mm-channel", metavar="NAME",
                    help="resolve #NAME via MATTERMOST_URL/TOKEN in current env and "
                         "write AP2_MM_CHANNELS=<id> into <project>/.cc-autopilot/env")
    sc.add_argument("--git-name", default=sandbox.DEFAULT_GIT_NAME,
                    help=f"repo-local git user.name (default: {sandbox.DEFAULT_GIT_NAME!r})")
    sc.add_argument("--git-email", default=sandbox.DEFAULT_GIT_EMAIL,
                    help=f"repo-local git user.email (default: {sandbox.DEFAULT_GIT_EMAIL!r})")
    sc.set_defaults(func=sandbox.cmd_project_setup)

    sc = sub_sbx.add_parser(
        "install-channel",
        help="resolve a MM channel name to an ID and write "
             "AP2_MM_CHANNELS into <project>/.cc-autopilot/env",
    )
    sc.add_argument("project", help="path to an existing ap2 project clone")
    sc.add_argument("channel", help="channel name (with or without leading #)")
    sc.add_argument("--user", default=sandbox.DEFAULT_USER)
    sc.set_defaults(func=sandbox.cmd_install_channel)

    sc = sub_sbx.add_parser("project-audit", help="verify isolated project clone")
    sc.add_argument("path")
    sc.add_argument("--user", default=sandbox.DEFAULT_USER)
    sc.set_defaults(func=sandbox.cmd_project_audit)

    sc = sub_sbx.add_parser(
        "sync-assets",
        help="deploy BOTH <repo>/skills/* AND ap2/howto.md into a target "
             "~/.claude/ in one invocation (TB-276; default dry-run, "
             "--apply to copy). Default: sudo into <user>'s home; "
             "--sbuser: write to current user's $HOME, no sudo",
    )
    sc.add_argument("user", nargs="?", default=sandbox.DEFAULT_USER,
                    help="target sandbox user (ignored when --sbuser is "
                         "passed; mutually exclusive with --sbuser)")
    sc.add_argument("--sbuser", action="store_true",
                    help="write to the CURRENT user's $HOME/.claude/ "
                         "without sudo (for a sandbox-user Claude session "
                         "that lacks sudoer privileges)")
    sc.add_argument("--apply", action="store_true",
                    help="copy assets onto their deployed targets "
                         "(default: dry-run drift summary)")
    sc.add_argument("--dest", metavar="DIR",
                    help="override the .claude/ root entirely "
                         "(default: target user's ~/.claude)")
    sc.set_defaults(func=sandbox.cmd_sync_assets)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = Config.load(args.project)
    return args.func(cfg, args) or 0


if __name__ == "__main__":
    raise SystemExit(main())

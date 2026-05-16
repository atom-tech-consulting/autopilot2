"""`autopilot` CLI — start/stop/status/add/logs/skip.

Intended to be run as `python -m ap2` or via the console_scripts entrypoint.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import doctor, events, rollback, sandbox
from ap2._shared import read_pid, short
from .board import Board, _norm_tag, board_file_lock
from .config import Config
from .cron import load_jobs, load_state
from .init import init_project
from . import tools


def _is_running(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _require_oauth_token() -> int:
    """Refuse to start the daemon when CLAUDE_CODE_OAUTH_TOKEN isn't in env (TB-79).

    Without the token the SDK control protocol times out on handshake and the
    daemon idles through `Control request timeout: initialize` events — the
    failure mode is silent because `claude` exits before printing anything to
    stderr. Returns 1 + prints remediation; the source-of-truth for env
    delivery is operator policy (login shell, sudoers env_keep, project env
    file), so ap2 stays out of guessing.
    """
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip():
        return 0
    print(
        "ap2: refusing to start — CLAUDE_CODE_OAUTH_TOKEN is not in the env.\n"
        "Without it the SDK control protocol will silently time out at\n"
        "initialize. Pick one:\n"
        "  - launch via login shell:  sudo -u <user> -i ap2 start\n"
        "  - install token first:     ap2 sandbox install-token <user>\n"
        "                             (then re-launch via -i, or set\n"
        "                             CLAUDE_CODE_OAUTH_TOKEN explicitly)\n"
        "  - one-off env pass:        sudo --preserve-env=CLAUDE_CODE_OAUTH_TOKEN \\\n"
        "                                 -u <user> ap2 start",
        file=sys.stderr,
    )
    return 1


def cmd_start(cfg: Config, args: argparse.Namespace) -> int:
    pid = read_pid(cfg)
    if _is_running(pid):
        print(f"already running (pid {pid})")
        return 0
    # stale pid file
    if cfg.pid_file.exists():
        cfg.pid_file.unlink()
    rc = _require_oauth_token()
    if rc != 0:
        return rc
    if args.foreground:
        from .daemon import run

        run(str(cfg.project_root))
        return 0
    # Fork into background via `python -m ap2 _run`.
    log = cfg.project_root / ".cc-autopilot" / "daemon.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "ap2", "--project", str(cfg.project_root), "_run"]
    with log.open("a") as f:
        proc = subprocess.Popen(
            cmd, stdout=f, stderr=f, stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    time.sleep(0.5)
    print(f"started (pid {proc.pid}), logs: {log}")
    return 0


def cmd_stop(cfg: Config, args: argparse.Namespace) -> int:
    pid = read_pid(cfg)
    if not pid or not _is_running(pid):
        print("not running")
        if cfg.pid_file.exists():
            cfg.pid_file.unlink()
        return 0
    sig = signal.SIGKILL if args.force else signal.SIGTERM
    os.kill(pid, sig)
    print(f"sent {sig.name} to pid {pid}")
    return 0


def cmd_status(cfg: Config, args: argparse.Namespace) -> int:
    pid = read_pid(cfg)
    running = _is_running(pid)
    board = Board.load(cfg.tasks_file)
    counts = {s: len(board.sections.get(s, [])) for s in
              ["Active", "Ready", "Backlog", "Pipeline Pending", "Complete", "Frozen"]}
    jobs = load_jobs(cfg.cron_file)
    state = load_state(cfg.cron_state_file)
    paused = cfg.pause_flag.exists()
    # TB-131: pending operator-queued ops (CLI / MM-handler appends that
    # haven't been drained by the daemon's tick yet). Visible here so an
    # operator can spot a stalled queue (depth > 0 with the daemon down
    # ⇒ ops will sit until the daemon comes back up).
    queue_pending = tools.operator_queue_pending_count(cfg)
    # TB-121: count Backlog tasks gated on the human-review clause so
    # `ap2 status` distinguishes "Backlog has 5 workable items" from
    # "Backlog has 5 ideation proposals waiting for an operator nod."
    # Cheap inline scan (the same board we already loaded above); avoids
    # importing diagnose.py for one number.
    # TB-151: keep the TB-Ns (not just the count) so the text branch can
    # name them and the JSON branch can carry a `pending_review_ids`
    # list — operators were having to grep TASKS.md to find the IDs.
    # TB-187: `any(...)` (was `all(...)`) so mixed-blocker tasks
    # (e.g. `@blocked:review,TB-5`) still surface as pending review —
    # the operator's approval is meaningful even when other blockers
    # remain, and `_is_dispatchable` continues to gate the actual
    # auto-promotion. Mirrors `web._is_pending_review` and
    # `status_report._pending_review_ids`.
    pending_review_ids = [
        t.id for t in board.iter_tasks("Backlog")
        if t.blocked_on and any(b.lower() == "review" for b in t.blocked_on)
    ]
    pending_review = len(pending_review_ids)
    # TB-173 / TB-191: surface the ideator's `## Decisions needed from
    # operator` section from `.cc-autopilot/ideation_state.md` so
    # actionable decisions surfaced at ideation time reach the operator
    # without manual file-reading. JSON carries the full helper output
    # (capped at 7 by `parse_operator_decisions`); the text-mode
    # rendering below truncates to the first 5 with a "(+M more)"
    # suffix to keep the status block compact, mirroring TB-151's
    # pending-review pattern. The agent-internal `## Cycle observations`
    # section (TB-191) is structurally excluded by the parser's
    # heading-match — it never reaches this surface.
    from .ideation import parse_operator_decisions

    operator_decisions = parse_operator_decisions(
        cfg.project_root / ".cc-autopilot" / "ideation_state.md"
    )
    # TB-177: surface the count of recent `janitor_finding` events so an
    # operator returning to the project sees stranded git state without
    # running `ap2 logs` first. The verdict-aware counter walks the
    # events tail and only counts findings inside
    # `RECENT_FINDING_WINDOW_S` — stale findings from a day-old run
    # don't accumulate (the next janitor cron will re-emit them if
    # still relevant). Surfaced alongside pending-review and
    # queue-pending so the three operator-attention signals share one
    # cluster.
    # TB-178: split the counter by LLM-judge verdict — only
    # `real_strand` drives the urgency tone of the `janitor:` line;
    # `operator_draft` findings get a softer summary; `ambiguous`
    # findings (judge couldn't decide) bucket together for operator
    # eyes-on without flagging as urgent.
    from .janitor import (
        recent_finding_counts_by_verdict as _recent_finding_counts,
    )

    janitor_counts = _recent_finding_counts(cfg)
    janitor_findings = sum(janitor_counts.values())
    # TB-189: count operator-authored impact verdicts (`task_classified`
    # events) in the last 30 days, broken down by verdict. Operators
    # learn faster when "we kept calling these proposals pro-forma" is
    # visible at-a-glance — same surfacing pattern as
    # `pending_review` / `janitor_findings` (counters operators glance
    # at on every status check). Empty status (no classifications in
    # the window) renders as zeros across all three keys in JSON; the
    # text branch omits the line entirely so a fresh project doesn't
    # grow zero-noise.
    classifications_30d = tools.classifications_last_30d_by_verdict(cfg)
    classifications_30d_total = sum(classifications_30d.values())
    # TB-227: auto-approve / auto-unfreeze loop state (axes 1-3 of the
    # end-to-end automation focus). Always computed (the helper handles
    # missing events file / unset knobs cleanly); the text rendering
    # below omits the line entirely when the knob is off AND no 24h
    # activity has accumulated, so fresh projects don't grow a zero-line.
    # JSON consumers always see the full `auto_approve` key for parser
    # stability.
    from . import automation_status

    auto_approve_state = automation_status.collect_auto_approve_state(cfg)
    # TB-242: axis-4 focus-rotation surface — read the focus list +
    # active focus + halt state once so both the text and JSON branches
    # can render them. Pure read-layer composition over TB-226's
    # `goal.read_focus_list` + `goal.active_focus` + `goal.load_pointer`
    # + `goal.roadmap_exhausted`; no new state files, no daemon-side
    # mutation. The render-symmetry mirrors TB-227's auto-approve
    # surface — fresh / pre-pivot projects (goal.md absent OR no
    # `## Current focus:` headings) get `active_focus: null` in JSON
    # and zero text-render lines.
    from . import goal as _goal

    _foci = _goal.read_focus_list(cfg)
    if _foci:
        _focus_pointer = _goal.load_pointer(cfg)
        _focus_item = _goal.active_focus(cfg, _foci)
        _focus_roadmap_complete = _goal.roadmap_exhausted(cfg, _foci)
    else:
        _focus_pointer = None
        _focus_item = None
        _focus_roadmap_complete = False
    # TB-130: when the daemon is up and the web UI wasn't disabled, surface
    # the URL so operators don't have to remember to run `ap2 web`
    # separately. Resolution mirrors the daemon's own — same env vars, same
    # default — so what we print is the URL the daemon is actually serving.
    web_url = _resolve_web_url(cfg) if running else None
    # TB-139: surface the running CLI's full version (base + git suffix on
    # editable installs) so an operator can confirm freshness alongside
    # daemon liveness without a second `ap2 --version` call. Same string
    # the daemon emits on its `daemon_start` event, so the post-mortem
    # reader can correlate `ap2 status` output with state on disk.
    version = _version_string()

    if args.json:
        out = {
            "running": running,
            "pid": pid,
            "paused": paused,
            "version": version,
            "tick_interval_s": cfg.tick_interval_s,
            "board": counts,
            "cron_jobs": [j.name for j in jobs],
            "cron_last_run": state,
            "tasks_file": str(cfg.tasks_file),
            "events_file": str(cfg.events_file),
            "web_url": web_url,
            "operator_queue_pending": queue_pending,
            "pending_review": pending_review,
            # TB-151: full TB-N list for machine consumers (web UI,
            # external monitors). The `pending_review` count is kept
            # for backward compat with anything that already parsed it.
            "pending_review_ids": pending_review_ids,
            # TB-173 / TB-191: the ideator's "decisions needed from
            # operator" list, untruncated. Empty list when the file or
            # section is absent — that's the steady-state happy path
            # for fresh projects. Renamed from `open_questions`
            # alongside the parser rename so the JSON key matches the
            # operator-facing label and the underlying schema section.
            "operator_decisions": operator_decisions,
            # TB-177: count of recent `janitor_finding` events (within
            # `RECENT_FINDING_WINDOW_S`). 0 on healthy projects /
            # missing events file — machine consumers always see the
            # key for parseability.
            "janitor_findings": janitor_findings,
            # TB-178: per-verdict breakdown so machine consumers (web
            # UI, external monitors) can render strands vs drafts vs
            # ambiguous independently. Always all three keys, defaulting
            # to 0.
            "janitor_findings_by_verdict": janitor_counts,
            # TB-189: operator-authored impact verdicts in the last 30
            # days (sourced from `task_classified` events). Always all
            # three keys (`advanced-goal` / `pro-forma` / `unclear`),
            # defaulting to 0 — so machine consumers always see the
            # full shape regardless of activity. The text branch below
            # omits the line entirely when total is 0.
            "classifications_last_30d_by_verdict": classifications_30d,
            # TB-227: auto-approve / auto-unfreeze loop state. Keys are
            # always present regardless of knob-state (machine consumers
            # get a stable shape); see `automation_status.collect_auto_approve_state`
            # for the contract.
            "auto_approve": auto_approve_state,
            # TB-242: axis-4 focus-rotation state. Renders as `null`
            # when goal.md is missing or has zero `## Current focus:`
            # headings (fresh / pre-pivot projects) so machine
            # consumers can distinguish "no roadmap" from "roadmap
            # exhausted" — the `roadmap_complete` boolean field
            # disambiguates the latter on populated roadmaps. `index`
            # is 0-based (matches `focus_pointer.json`'s
            # `active_index`); the text branch displays it as `idx+1`
            # for human readability. `title` is the active focus title
            # when one is in flight; it falls back to "" when the
            # pointer is past the last focus (halt state) — the
            # `roadmap_complete: true` flag carries the meaning there.
            "active_focus": (
                None
                if not _foci
                else {
                    "title": _focus_item.title if _focus_item else "",
                    "index": _focus_pointer["active_index"],
                    "total": len(_foci),
                    "roadmap_complete": _focus_roadmap_complete,
                }
            ),
        }
        print(json.dumps(out, indent=2))
        return 0

    print(f"daemon:   {'running' if running else 'stopped'} (pid {pid or '-'}){' [paused]' if paused else ''}")
    print(f"version:  ap2 {version}")
    print(f"tick:     {cfg.tick_interval_s}s")
    # TB-242: surface axis-4 focus-rotation state near the top of the
    # report so an operator returning after walk-away can answer
    # "what focus am I on, and how many remain?" without grepping
    # events.jsonl or reading `focus_pointer.json` by hand. Three
    # render shapes (mirrors the JSON branch):
    #   - halt state (pointer past last focus, no operator ack since
    #     the most recent `roadmap_complete` event) → the halt-state
    #     line with the `ap2 ack roadmap_complete` resume nudge, same
    #     shape as TB-227's auto-approve PAUSED line.
    #   - multi-focus → `focus: <title> (<idx+1> of <total>)`.
    #   - single-focus → `focus: <title>` (no `(1 of 1)` suffix —
    #     single-focus projects don't need a position counter).
    # Omitted entirely when goal.md is missing or has zero
    # `## Current focus:` headings (fresh / pre-pivot projects) so
    # the default-off output stays byte-identical to pre-TB-242.
    if _foci:
        if _focus_roadmap_complete:
            print(
                "focus:    ROADMAP_COMPLETE — "
                "`ap2 ack roadmap_complete` to resume"
            )
        elif len(_foci) == 1:
            _title = _focus_item.title if _focus_item else ""
            print(f"focus:    {_title}")
        elif _focus_item is not None:
            _idx_display = _focus_pointer["active_index"] + 1
            print(
                f"focus:    {_focus_item.title} "
                f"({_idx_display} of {len(_foci)})"
            )
    print(
        f"board:    {counts['Active']}A / {counts['Ready']}R / "
        f"{counts['Backlog']}B / {counts['Pipeline Pending']}P / "
        f"{counts['Complete']}C / {counts['Frozen']}F"
    )
    print(f"cron:     {len(jobs)} jobs ({', '.join(j.name for j in jobs) or '-'})")
    print(f"tasks:    {cfg.tasks_file}")
    print(f"events:   {cfg.events_file}")
    if web_url:
        print(f"web:      {web_url}")
    if queue_pending:
        print(
            f"pending:  {queue_pending} operator op"
            f"{'s' if queue_pending != 1 else ''}"
        )
    if pending_review_ids:
        # TB-121: shown only when N>0 so a clean board doesn't grow a
        # zero-line. Mention `ap2 approve` so the action is one
        # readable nudge away.
        # TB-151: name the actual TB-Ns (truncated to 5 with a
        # "(+N more)" suffix via the shared helper) so the operator
        # doesn't have to grep TASKS.md to find the IDs to approve.
        from .status_report import _format_pending_review_line
        ids_line = _format_pending_review_line(pending_review_ids)
        print(
            f"review:   {pending_review} pending — {ids_line}\n"
            f"          (`ap2 approve TB-N`)"
        )
    if classifications_30d_total:
        # TB-189: render the impact-verdict counts as a single compact
        # line so the operator sees the trend at-a-glance. Format
        # mirrors the briefing's pin: `classifications last 30d:
        # advanced-goal=<n>, pro-forma=<m>, unclear=<k>`. Only emitted
        # when at least one verdict exists in the window — fresh
        # projects don't grow a zero-line.
        c = classifications_30d
        print(
            f"classifications last 30d: "
            f"advanced-goal={c['advanced-goal']}, "
            f"pro-forma={c['pro-forma']}, "
            f"unclear={c['unclear']}"
        )
    if janitor_findings:
        # TB-177 + TB-178: surface stranded git state without making the
        # operator run `ap2 logs` first. Render strands / drafts /
        # ambiguous separately so a `draft_*.md` working notebook
        # doesn't read as urgent — only `real_strand` carries the
        # operator-attention urgency. Per-finding detail (subkind,
        # paths, hint, reasoning) lives in events.jsonl.
        n_strand = janitor_counts["real_strand"]
        n_draft = janitor_counts["operator_draft"]
        n_ambig = janitor_counts["ambiguous"]
        parts: list[str] = []
        if n_strand:
            parts.append(
                f"{n_strand} strand{'s' if n_strand != 1 else ''}"
            )
        if n_draft:
            parts.append(
                f"{n_draft} draft{'s' if n_draft != 1 else ''}"
            )
        if n_ambig:
            parts.append(
                f"{n_ambig} ambiguous"
            )
        print(
            f"janitor:  {', '.join(parts)} — "
            "`ap2 logs` (filter type=janitor_finding) to inspect"
        )
    if operator_decisions:
        # TB-173 / TB-191: surface ideator-surfaced operator decisions
        # from `ideation_state.md` so escalation reaches the CLI
        # without a manual file read. Truncate per-bullet to ~80 chars
        # with an ellipsis; cap at the first 5 bullets with a
        # "(+M more)" tail so the status block stays compact (mirrors
        # TB-151's pending-review-line shape). Label changed from
        # "open questions for operator" to "decisions needed" alongside
        # the schema rename so the surfacing label matches the
        # actionable-decision shape required by the schema.
        _OPERATOR_DECISIONS_RENDER_CAP = 5
        _OPERATOR_DECISIONS_BULLET_MAX_CHARS = 80
        rendered: list[str] = []
        for bullet in operator_decisions[:_OPERATOR_DECISIONS_RENDER_CAP]:
            if len(bullet) > _OPERATOR_DECISIONS_BULLET_MAX_CHARS:
                rendered.append(
                    bullet[: _OPERATOR_DECISIONS_BULLET_MAX_CHARS - 3] + "..."
                )
            else:
                rendered.append(bullet)
        if len(operator_decisions) > _OPERATOR_DECISIONS_RENDER_CAP:
            rendered.append(
                f"(+{len(operator_decisions) - _OPERATOR_DECISIONS_RENDER_CAP} more)"
            )
        print(
            f"decisions needed ({len(operator_decisions)}): "
            + "; ".join(rendered)
        )
    # TB-227: surface the auto-approve / auto-unfreeze loop state. Two
    # rendering shapes — healthy (knob on, no halt) vs. paused (halt
    # active, ack verb shown so the action is one readable nudge away,
    # mirroring TB-151's pending-review line shape). Omitted entirely
    # when the knob is off AND all 24h counters are zero so fresh /
    # pre-opt-in projects don't grow a perpetual zero-line (same shape
    # as TB-189's classifications line).
    a = auto_approve_state
    # TB-241: dry-run 24h activity also counts toward the render-block
    # decision so an operator who flipped `AP2_AUTO_APPROVE_DRY_RUN=1` /
    # `AP2_AUTO_UNFREEZE_DRY_RUN=1` against an otherwise quiet board
    # still sees the readiness signal here (the dry-run on-ramp's
    # whole purpose is to observe the loop's decisions on-demand
    # without flipping live dispatch). Pre-TB-241 the bucket counted
    # only real-mode activity, so dry-run-only state fell through and
    # the operator saw nothing changed after the knob flip.
    _has_24h_activity = (
        a["auto_approved_count_24h"]
        + a["auto_unfreeze_applied_count_24h"]
        + a["auto_unfreeze_skipped_count_24h"]
        + a["would_auto_approve_count_24h"]
        + a["would_auto_unfreeze_count_24h"]
    ) > 0
    if a["auto_approve_enabled"] or _has_24h_activity:
        if a["auto_approve_paused"]:
            print(
                f"auto-approve: PAUSED (reason={a['pause_reason']}; "
                f"{a['consecutive_freezes']} consecutive freezes / "
                f"threshold {a['freeze_threshold']}) — "
                f"`ap2 ack auto_approve_window_resume`"
            )
        else:
            print(
                f"auto-approve: enabled (24h: "
                f"{a['auto_approved_count_24h']} approved, "
                f"{a['auto_unfreeze_applied_count_24h']} auto-unfrozen)"
            )
        # TB-241: surface the dry-run readiness signal (sibling of the
        # TB-238 status-report digest `*Dry-run window:*` sub-block) on
        # the on-demand `ap2 status` surface. Rendered immediately
        # below the existing `auto-approve:` line so an operator
        # reading the two side-by-side sees the real-mode summary
        # first and the dry-run readiness count on the next row.
        # Omitted entirely when both dry-run knobs are off so the
        # default-off output stays byte-identical to TB-227.
        if a["dry_run_enabled"] or a["auto_unfreeze_dry_run_enabled"]:
            print(
                f"dry-run: would-approve "
                f"{a['would_auto_approve_count_24h']} (24h) | "
                f"would-unfreeze "
                f"{a['would_auto_unfreeze_count_24h']} (24h)"
            )
    nxt = board.next_ready()
    if nxt:
        print(f"next:     {nxt.id} {nxt.title}")
    return 0


def _resolve_web_url(cfg: Config) -> str | None:
    """The URL the daemon-spawned web UI is serving on, or `None` when off.

    Returns `None` when `AP2_WEB_DISABLED` is set (the operator opted out
    of the bundled UI for this daemon process).

    TB-155: prefers the most recent `web_start` event in `events.jsonl`
    over recomputing from env, so the URL we print reflects the
    auto-enumerated port (e.g. 8730 when 8729 was busy at daemon start).
    Falls back to the env-based default when no `web_start` event has
    been written yet — covers the brief window between `ap2 start` and
    the daemon's first bind, and any older events.jsonl that predates
    the daemon's web lifecycle wiring.
    """
    from . import events as _events
    from . import web as _web

    if _web.is_web_disabled():
        return None

    # Walk events.jsonl backward looking for the most recent web lifecycle
    # signal. A `web_stop` newer than the last `web_start` means the web
    # UI shut down (orderly cancel or post-error fall-through); we still
    # print the env-derived URL because the daemon being `running`
    # implies it's about to re-bind on the next loop iteration. A
    # `web_start` newer than (or with no) `web_stop` is canonical.
    if cfg.events_file.exists():
        # 200 events is a comfortable window — `web_start`/`web_stop` fire
        # at most twice per daemon lifecycle, so anything older is safely
        # superseded by current state.
        recent = _events.tail(cfg.events_file, n=200)
        last_start: dict | None = None
        last_stop_ts: str | None = None
        for evt in recent:
            t = evt.get("type")
            if t == "web_start":
                last_start = evt
            elif t == "web_stop":
                last_stop_ts = evt.get("ts") or last_stop_ts
        if last_start is not None and (
            last_stop_ts is None
            or (last_start.get("ts") or "") >= last_stop_ts
        ):
            url = last_start.get("url")
            if url:
                return url
            # Older events without a pre-built URL — synthesize from host/port.
            host = last_start.get("host") or "127.0.0.1"
            port = last_start.get("port")
            if port:
                return f"http://{host}:{port}/"

    port = _web.daemon_web_port()
    return f"http://127.0.0.1:{port}/"


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


def cmd_init(cfg: Config, args: argparse.Namespace) -> int:
    """Idempotent project scaffolding: gitignore entries + tasks dir.

    `cfg.project_root` is already resolved by Config.load() — we don't take a
    DIR argument because every other ap2 subcommand operates on the same root.
    """
    report = init_project(cfg.project_root)
    print(f"ap2 init: {report.project_root}")
    report.print()
    return 0


def cmd_check(cfg: Config, args: argparse.Namespace) -> int:
    """One-shot integrity check on TASKS.md, cron.yaml, JSON state files,
    insights front matter, and briefing-link resolution (TB-108).

    Sibling of `ap2 doctor` (which checks the environment — sandbox user,
    OAuth token, project clone, CLI presence). `check` checks the data on
    disk. Exit nonzero on any error; warnings don't fail.
    """
    from . import check

    report = check.check_project(cfg)
    print(check.render_json(report) if args.json else check.render_text(report))
    return 0 if report.ok else 1


def cmd_doctor(cfg: Config, args: argparse.Namespace) -> int:
    """One-shot environment-readiness check (project skeleton + sandbox + CLI)."""
    user = args.user or sandbox.DEFAULT_USER
    rep = doctor.diagnose(cfg.project_root, user)
    rep.print()
    return 0 if rep.ok else 1


def cmd_logs(cfg: Config, args: argparse.Namespace) -> int:
    n = args.n
    evts = events.tail(cfg.events_file, n=n)
    if args.json:
        for e in evts:
            print(json.dumps(e))
        return 0
    for e in evts:
        ts = e.get("ts", "")
        typ = e.get("type", "?")
        # TB-158: dedicated rendering for `verification_failed` rows so the
        # operator sees N/M passed + the failing bullet headlines without
        # opening events.jsonl in an editor or expanding raw json.
        if typ == "verification_failed":
            print(_format_verification_failed_row(ts, e))
            continue
        # TB-180: compact one-line rendering for the three usage-carrying
        # event types (`judge_call`, `task_run_usage`, `control_run_usage`)
        # — the verbose `usage` / `model_usage` / `server_tool_use` blobs
        # otherwise wrap the row across several lines and drown the
        # at-a-glance signal. Same shared helper as TB-179's web rendering
        # so the CLI and `/events` page stay symmetric. Operators wanting
        # the raw payload pass `--json` (regression-pinned).
        if typ in ("judge_call", "task_run_usage", "control_run_usage"):
            compact = events.summarize_usage_event(e)
            if compact:
                print(f"{ts} {typ:16s} {compact}")
                continue
        extras = {k: v for k, v in e.items() if k not in ("ts", "type")}
        extra = " ".join(f"{k}={short(v, 120)}" for k, v in extras.items())
        print(f"{ts} {typ:16s} {extra}")
    return 0


def _format_verification_failed_row(ts: str, e: dict) -> str:
    """TB-158: pretty-print a `verification_failed` event for `ap2 logs`.

    Shape:
        <ts>  verification_failed  <task>  <pass>/<total> passed, <f> failed, <u> unverified
          ✗ [<kind>]  <bullet, truncated to ~120>
                     ↳ <judge note, truncated to ~200>

    Passing / unverified bullets are NOT individually rendered (they live
    in the counter only) — the briefing's `## Out of scope` calls this out
    explicitly to keep the noise/signal ratio in the operator's favor.
    Operators wanting the raw payload pass `--json` (regression-pinned).
    """
    summary = events.summarize_verification_failed(
        e, max_bullet=120, max_note=200,
    )
    task = str(e.get("task") or "").strip() or "?"
    lines = [
        f"{ts} verification_failed {task}  {summary['summary_line']}"
    ]
    for fb in summary["failed_bullets"]:
        kind = fb.get("kind") or "?"
        bullet = fb.get("bullet") or ""
        notes = fb.get("notes") or ""
        lines.append(f"  ✗ [{kind}]  {bullet}")
        if notes:
            lines.append(f"            ↳ {notes}")
    return "\n".join(lines)


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


def cmd_ideate(cfg: Config, args: argparse.Namespace) -> int:
    """Manually trigger an ideation pass on the daemon's next tick (TB-159).

    Bypasses the natural empty-board / cooldown / `AP2_IDEATION_DISABLED`
    gates that govern `ideation._maybe_ideate`. Routed through the
    operator queue rather than spinning up the SDK from the CLI process
    so the daemon stays the single owner of the control-agent SDK slot
    (same pattern as `ap2 add` / `approve` / `reject` / `unfreeze` /
    `delete` / `update`).

    TB-194: the queue-append handler no longer rejects this op when a
    task happens to be Active. By the loop-topology invariant the
    drain runs as `_tick`'s first stage with Active already cleared
    by the previous tick's synchronous `run_task`, so the previously-
    feared concurrent-SDK interleaving is unreachable. `--force` is
    accepted as a no-op for the routing decision (audit-only metadata
    on the queue payload); the flag is preserved for one release so
    callers passing it don't break.

    The CLI is non-blocking: it returns immediately after the queue
    append; the daemon picks up the signal in the next tick (≤30s by
    default).
    """
    res = tools.do_operator_queue_append(
        cfg, {"op": "ideate", "force": bool(args.force)}
    )
    if res.get("isError"):
        print(res["content"][0]["text"], file=sys.stderr)
        return 1
    print("queued ideate (will run at next tick — ≤30s)")
    return 0


def cmd_update_goal(cfg: Config, args: argparse.Namespace) -> int:
    """Refresh `goal.md` via the operator queue (TB-193).

    Routes through the operator queue rather than mutating goal.md in
    place because ideation reads the file mid-cycle (anchors injected
    into the prompt; `_goal_md_anchors` consulted at queue-append time
    for TB-161) and the per-task verifier (TB-69) reads it as part of
    the rollback-cohesion state surface — an in-place edit racing a
    snapshot-window write would tear against any of those readers. The
    queue-routed write lands at a tick boundary, under
    `board_file_lock`, in the same `state: drained N operator op(s)`
    commit as any co-staged ops.

    Symmetric to `ap2 add --briefing-file`: pass `--file <path>` to
    read the new goal content from a path, or `--file -` to read from
    stdin. `--reason` is optional and feeds the operator-log audit
    line `<ts> — operator updated goal.md (<reason>)` future ideation
    cycles read as a goal-drift signal.
    """
    try:
        content = _read_briefing_file(args.file)
    except OSError as e:
        print(f"ap2 update-goal: {e}", file=sys.stderr)
        return 1
    if not content.strip():
        print(
            "ap2 update-goal: --file is empty — refusing.\n"
            "  Pass a non-empty goal.md payload (whitespace-only is "
            "rejected).",
            file=sys.stderr,
        )
        return 1
    # Soft client-side cap so a runaway file doesn't get queued. The
    # daemon will accept whatever lands, but a goal.md > 100KB is
    # almost certainly a path-vs-content mistake; bail early.
    if len(content) > 100_000:
        print(
            f"ap2 update-goal: --file is {len(content)} bytes — "
            f"refusing (cap 100000). goal.md is meant to be a short "
            f"focus document; double-check you passed the goal "
            f"content, not a log/dump.",
            file=sys.stderr,
        )
        return 1
    payload: dict = {"op": "update_goal", "goal_content": content}
    if args.reason is not None:
        payload["reason"] = args.reason
    res = tools.do_operator_queue_append(cfg, payload)
    if res.get("isError"):
        print(res["content"][0]["text"], file=sys.stderr)
        return 1
    print("queued update_goal (lands at next tick)")
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


def cmd_rollback(cfg: Config, args: argparse.Namespace) -> int:
    """Linear rollback (TB-111).

    Walk back along first-parent history to a boundary commit and
    `git reset --hard` to it. Atomic via `locked_board()`. Mid-history
    rollback (revert TB-X while keeping TB-Y after) is explicitly out of
    scope — operators do that by hand with `git revert`.
    """
    if not (cfg.project_root / ".git").exists():
        print("ap2 rollback: project is not a git repo — nothing to roll back",
              file=sys.stderr)
        return 1

    # Pre-flight: refuse a dirty working tree. Rollback isn't a stash.
    porcelain = subprocess.run(
        ["git", "-C", str(cfg.project_root), "status", "--porcelain"],
        capture_output=True, text=True,
    )
    if porcelain.returncode != 0:
        print(f"ap2 rollback: `git status --porcelain` failed: "
              f"{porcelain.stderr.strip()}", file=sys.stderr)
        return 1
    if porcelain.stdout.strip() and not args.force:
        print(
            "ap2 rollback: working tree is dirty — refusing.\n"
            "  Commit, stash, or `git checkout -- .` your changes first,\n"
            "  or pass --force to bypass (the dirt will be discarded).",
            file=sys.stderr,
        )
        return 1

    # Resolve boundary from -n / --task / --to (mutually exclusive; default -n 1).
    boundary: str | None = None
    if args.to:
        # Explicit ancestor sha. Refuse if not an ancestor (no rebases mid-rollback).
        if not rollback.is_ancestor(cfg, args.to):
            print(f"ap2 rollback: {args.to} is not an ancestor of HEAD — refusing",
                  file=sys.stderr)
            return 1
        # Resolve to a full SHA so the print is unambiguous.
        rp = subprocess.run(
            ["git", "-C", str(cfg.project_root), "rev-parse", args.to],
            capture_output=True, text=True,
        )
        boundary = rp.stdout.strip() if rp.returncode == 0 else args.to
    elif args.task:
        boundary = rollback.resolve_boundary_by_task(cfg, args.task)
        if boundary is None:
            print(
                f"ap2 rollback: {args.task} not found in HEAD's first-parent "
                f"history.\n  Try `git log --grep={args.task} --oneline` — "
                f"the task may be too far back, or it shipped on a side branch.",
                file=sys.stderr,
            )
            return 1
    else:
        n = args.n if args.n is not None else 1
        if n <= 0:
            print("ap2 rollback: -n must be ≥ 1", file=sys.stderr)
            return 2
        boundary = rollback.resolve_boundary_by_n(cfg, n)
        if boundary is None:
            print(f"ap2 rollback: history doesn't have {n} task-completions "
                  f"to roll back", file=sys.stderr)
            return 1

    affected = rollback.list_affected_commits(cfg, boundary)
    if not affected:
        print(f"ap2 rollback: nothing to roll back "
              f"(boundary {boundary[:8]} == HEAD)")
        return 0
    affected_tasks = rollback.affected_task_ids(affected)
    pipeline_warnings = rollback.list_alive_pipelines_in_range(cfg, boundary)

    # Print plan.
    print("Rollback plan:")
    print(f"  Boundary: {boundary[:8]}")
    print(f"  Affected commits ({len(affected)}):")
    for sha, subject in affected:
        print(f"    - {sha[:8]} {subject}")
    if affected_tasks:
        print(f"  Affected tasks: {', '.join(affected_tasks)}")
    if pipeline_warnings:
        print("  Pipelines still running (NOT auto-killed):")
        for w in pipeline_warnings:
            print(f"    pid {w['pid']} ({w['name'] or '?'}) "
                  f"— log: {w['log']}")
    if not args.yes:
        try:
            reply = input("Proceed? [y/N] ").strip().lower()
        except EOFError:
            reply = ""
        if reply not in ("y", "yes"):
            print("ap2 rollback: aborted (no changes made)")
            return 0

    # Execute under board lock for atomicity vs. the daemon. We use the
    # save-less variant because `git reset --hard` already wrote the
    # post-reset TASKS.md; locked_board's save-on-exit would clobber it.
    with board_file_lock(cfg.tasks_file):
        try:
            rollback.linear_rollback_to(cfg, boundary)
        except Exception as exc:  # noqa: BLE001
            events.append(
                cfg.events_file, "rollback_error",
                boundary=boundary, error=f"{type(exc).__name__}: {exc}",
            )
            print(f"ap2 rollback: failed: {exc}", file=sys.stderr)
            return 1

    events.append(
        cfg.events_file,
        "task_rollback",
        boundary_sha=boundary,
        reverted_commits=[
            {"sha": sha, "subject": subject} for sha, subject in affected
        ],
        affected_tasks=affected_tasks,
        pipeline_warnings=pipeline_warnings,
    )
    print(f"ap2 rollback: reset to {boundary[:8]} "
          f"({len(affected)} commit(s) reverted, "
          f"{len(affected_tasks)} task(s) affected)")
    if pipeline_warnings:
        print(f"  warning: {len(pipeline_warnings)} pipeline subprocess(es) "
              f"still running — terminate manually if rerunning")
    return 0


def _active_task_id(cfg: Config) -> str | None:
    """TB-202 helper: return the first Active task's ID, or None if
    Active is empty. Centralizes the board read; the literal "a task
    is currently active" refusal phrasing lives at each call site so
    the briefing's `grep -nE "a task is currently active" ap2/cli.py`
    verification (≥2 hits) catches a regression where one of the two
    refuse-if-active gates is dropped or weakened. (See
    `cmd_backfill_proposals` and `cmd_cron_edit`.)

    Why refuse-if-active rather than queue-routing both verbs: both
    are rare operations (`backfill-proposals` is a one-off historical
    seed; `cron edit` is operational tuning during project setup or
    cadence-adjust, not routine). Queue-routing has architectural
    overhead — register the op, add a drain-side handler, design the
    queue payload — worth it for frequent surfaces like `ap2 ack`
    (sibling TB-201) but not for these. The cheap mitigation: read
    the board once, refuse if Active is non-empty, point the
    operator at `ap2 status` and `ap2 pause` (with the caveat that
    pause doesn't abort in-flight tasks; it only stops dispatch of
    new ones).
    """
    board = Board.load(cfg.tasks_file)
    active = list(board.iter_tasks("Active"))
    return active[0].id if active else None


def cmd_backfill_proposals(cfg: Config, args: argparse.Namespace) -> int:
    """Backfill historical ideation proposal records (TB-195).

    Operator-driven one-off: scans operator_log.md, briefing files, and
    events.jsonl to identify every TB-N that came in via an ideation
    `add_backlog` (briefing carries both a goal anchor and a Why-now
    paragraph), then writes
    `.cc-autopilot/ideation_proposals/<TB-N>.json` records for those
    that lack them — reconciling outcomes from the board's Complete
    section, the LAST `task_complete` event, and the operator log's
    reject / delete / approve lines.

    Idempotent: a TB-N whose record already exists is skipped, so the
    daemon-driven prospective writes (TB-188) and operator-driven
    backfill don't fight each other. Re-running after the daemon has
    accumulated more prospective records is safe — the second pass
    reports zero new records.

    `--dry-run` prints what WOULD be written without touching disk;
    operators can preview the impact before committing.

    TB-202: pre-flight refuse-if-active gate — if a task agent is
    running, refuse rather than racing the fenced-path write against
    the agent's TB-110 snapshot window and triggering a
    false-positive rollback. `.cc-autopilot/ideation_proposals/` is
    fenced (TB-188) and NOT exempt from the snapshot check; the
    refusal is cheaper than queue-routing the (rare) backfill verb.
    """
    active_id = _active_task_id(cfg)
    if active_id is not None:
        print(
            f"ap2 backfill-proposals: a task is currently active "
            f"({active_id}) — refusing.\n"
            f"  backfill-proposals writes to fenced "
            f"`.cc-autopilot/ideation_proposals/` and racing the active "
            f"task would trigger a state_violation rollback.\n"
            f"  Wait for the task to complete (see `ap2 status`) or pause "
            f"the daemon, then retry. Note: `ap2 pause` halts dispatch of "
            f"new tasks but does NOT abort the in-flight one; pause helps "
            f"only for the NEXT slot.",
            file=sys.stderr,
        )
        return 1

    from . import backfill

    report = backfill.backfill_proposals(cfg, dry_run=args.dry_run)
    for line in report.summaries:
        print(line)
    label = "would write" if args.dry_run else "wrote"
    print(
        f"backfill: {label}={len(report.written)} "
        f"skipped_existing={len(report.skipped_existing)} "
        f"skipped_non_ideation={len(report.skipped_non_ideation)} "
        f"skipped_no_briefing={len(report.skipped_no_briefing)}"
    )
    return 0


def cmd_pause(cfg: Config, args: argparse.Namespace) -> int:
    cfg.pause_flag.parent.mkdir(parents=True, exist_ok=True)
    cfg.pause_flag.write_text((args.reason or "") + "\n")
    events.append(cfg.events_file, "daemon_pause", reason=args.reason or "")
    print("paused (flag written)")
    return 0


def cmd_resume(cfg: Config, args: argparse.Namespace) -> int:
    if cfg.pause_flag.exists():
        cfg.pause_flag.unlink()
    events.append(cfg.events_file, "daemon_resume")
    print("resumed")
    return 0


def cmd_cron_list(cfg: Config, args: argparse.Namespace) -> int:
    jobs = load_jobs(cfg.cron_file)
    state = load_state(cfg.cron_state_file)
    for j in jobs:
        last = state.get(j.name, 0)
        last_str = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(last)) if last else "never"
        print(f"{j.name:30s} every {j.interval_s}s  last={last_str}  cond={j.active_when or '-'}")
    return 0


def cmd_cron_edit(cfg: Config, args: argparse.Namespace) -> int:
    """Operator CLI for mutating `.cc-autopilot/cron.yaml` (TB-146 +
    TB-202).

    TB-146 retired `cron_edit` from every agent toolset — cron schedule
    mutation is operator-CLI-only. The handler under the hood is
    `tools.do_cron_edit`; this command is the operator-facing wrapper
    invoked as `ap2 cron edit <action> <name> [...flags]`.

    TB-202: pre-flight refuse-if-active gate — cron.yaml is fenced
    and not exempt from the TB-110 post-hoc snapshot check, so a
    mid-task `ap2 cron edit` would trigger a false-positive rollback.
    The refuse-if-active gate is the cheap mitigation; queue-routing
    is overkill for an operation that fires during project setup or
    cadence-tuning, not routinely.
    """
    active_id = _active_task_id(cfg)
    if active_id is not None:
        print(
            f"ap2 cron edit: a task is currently active "
            f"({active_id}) — refusing.\n"
            f"  cron edit writes to fenced `.cc-autopilot/cron.yaml` and "
            f"racing the active task would trigger a state_violation "
            f"rollback.\n"
            f"  Wait for the task to complete (see `ap2 status`) or pause "
            f"the daemon, then retry. Note: `ap2 pause` halts dispatch of "
            f"new tasks but does NOT abort the in-flight one; pause helps "
            f"only for the NEXT slot.",
            file=sys.stderr,
        )
        return 1

    payload: dict = {"action": args.action, "name": args.name}
    if args.interval is not None:
        payload["interval"] = args.interval
    if args.prompt is not None:
        payload["prompt"] = args.prompt
    if args.active_when is not None:
        payload["active_when"] = args.active_when
    if args.max_turns is not None:
        payload["max_turns"] = args.max_turns
    res = tools.do_cron_edit(cfg, payload)
    if res.get("isError"):
        print(res["content"][0]["text"], file=sys.stderr)
        return 1
    print(res["content"][0]["text"])
    return 0


def cmd_ack(cfg: Config, args: argparse.Namespace) -> int:
    """Queue an operator-decision line for the daemon to append to
    `.cc-autopilot/operator_log.md` at the next tick (TB-106, TB-201).

    Used to communicate "I did X" / "I decided Y" back to ap2 so
    ideation stops re-proposing actions whose effects aren't visible
    on the filesystem (e.g. "considered FRAGILE plist retention,
    decided to keep them"). Optional `-t TB-N` ties the ack to a task.

    TB-201: routed through the operator queue rather than mutating
    operator_log.md synchronously. The pre-TB-201 in-place write
    raced with running task agents — operator_log.md is fenced and
    NOT exempt from the TB-110 post-hoc snapshot check, so a mid-task
    `ap2 ack` tripped a false-positive state violation and rolled
    back the task's legitimate work (cost ~$12.55 on post-train at
    2026-05-12T06:40-07:14Z; that incident is the proximate motivator
    for this retrofit). The drain-side `_apply_operator_ack` performs
    the actual operator_log.md write at tick boundary, under the
    daemon's board lock — never inside a task agent's snapshot
    window. Slight UX change from the immediate "appended to
    operator_log.md" of yesteryear; consistent with the rest of the
    queue-routed CLI verbs (`approve` / `reject` / `classify` /
    `update-goal` / `add` / etc.).
    """
    res = tools.enqueue_operator_ack(
        cfg,
        {"note": args.note, "task_id": args.task or ""},
    )
    if res.get("isError"):
        print(res["content"][0]["text"], file=sys.stderr)
        return 1
    print("queued ack (will land at next tick)")
    return 0


def cmd_web(cfg: Config, args: argparse.Namespace) -> int:
    """Start the local read-only web UI for daemon state and event log.

    Defaults to 127.0.0.1 so the (no-auth) page can't leak full event
    payloads — briefings, prompt-dump paths, Mattermost message bodies —
    off the box. Override with --host at your own risk.

    TB-155: `--port` is now an enumeration START — when busy (typically a
    stale `ap2 web` from this or another project), `web.serve` walks
    forward up to `web.DEFAULT_WEB_PORT_MAX_ATTEMPTS` before giving up.
    """
    from . import web

    web.serve(cfg, host=args.host, port=args.port)
    return 0


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


def _version_string() -> str:
    """The full version string printed by `ap2 --version` and `ap2 status`.

    Delegates to `ap2.get_version()` (TB-139), which combines the installed
    base version (pyproject.toml, via `importlib.metadata`) with a PEP 440
    local-version suffix `+<short-sha>.<commit-ts>` derived from the
    package's own git checkout. Editable installs — the common case here —
    therefore expose the source revision on every invocation, so an
    operator can `ap2 --version` to confirm freshness against `git log -1`
    instead of debugging through stale source.

    Released wheels (no `.git/` next to the package) get just the base
    version; no behavior change vs. the pre-TB-139 single-call importlib
    lookup.
    """
    from . import get_version

    return get_version()


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
             "the goal forward (the failure mode goal.md L66-76 names); "
             "`unclear` = impact not yet legible.",
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
        "install-howto",
        help="copy ap2/howto.md to ~<user>/.claude/ap2-howto.md so a Claude "
             "session running as the sandbox user can read it for context",
    )
    sc.add_argument("user", nargs="?", default=sandbox.DEFAULT_USER)
    sc.set_defaults(func=sandbox.cmd_install_howto)

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
        "sync-skills",
        help="sync <repo>/skills/* into $HOME/.claude/skills/ "
             "(TB-140; default dry-run, --apply to copy)",
    )
    sc.add_argument("--apply", action="store_true",
                    help="copy each skill onto its deployed copy "
                         "(default: dry-run drift summary)")
    sc.add_argument("--dest", metavar="DIR",
                    help="override destination root "
                         "(default: $HOME/.claude/skills)")
    sc.set_defaults(func=sandbox.cmd_sync_skills)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = Config.load(args.project)
    return args.func(cfg, args) or 0


if __name__ == "__main__":
    raise SystemExit(main())

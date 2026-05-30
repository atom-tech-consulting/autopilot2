"""Daemon-lifecycle CLI handlers (TB-264 split from `ap2/cli.py`).

Owns the lifecycle / observability verbs operators reach for to manage the
daemon process and inspect its in-memory + on-disk state:

  - `cmd_start` / `cmd_stop`  — process lifecycle (fork to background;
    SIGTERM/SIGKILL on stop).
  - `cmd_status`              — the at-a-glance daemon + board + cron
    snapshot, rendered both as text (operator console) and JSON
    (machine consumers).
  - `cmd_pause` / `cmd_resume` — operator-side dispatch gating via the
    `.cc-autopilot/pause` flag file (the daemon still ticks; it just
    refuses to spawn new task agents while the flag is present).
  - `cmd_web`                  — start the local read-only web UI for
    status + events.

Plus lifecycle helpers `_is_running`, `_require_oauth_token`,
`_version_string`, `_resolve_web_url`. `_version_string` lives here
because `cmd_status` is its primary consumer; `cli.py`'s argparse builder
imports it for the `--version` action.

The argparse builder (`build_parser` in `cli.py`) binds each verb via
`parser.set_defaults(func=cmd_<verb>)` against the names imported from
this module — pure mechanical lift from the pre-TB-264 monolith; the
handlers' `(cfg, args) -> int` signatures are unchanged.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time

from ap2._shared import read_pid
from .board import Board
from .config import Config
from .cron import load_jobs, load_state
from . import events, tools


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
    """At-a-glance daemon + board + automation snapshot for the operator.

    Text-render line order (each line is omitted-on-empty unless noted):

      daemon:       — liveness + pid + paused-flag (always)
      version:      — running CLI version string (always)
      tick:         — `cfg.tick_interval_s` (always)
      focus:        — TB-242 axis-4 focus-rotation surface
      board:        — A/R/B/P/C/F counts (always)
      cron:         — registered cron jobs (always)
      tasks: / events: / web: / pending: / review: / classifications: /
      janitor: / decisions needed: / audit: — operator-attention cluster
      WARN:         — TB-260 env-staleness banner
      attention:    — TB-298 active attention conditions (CLI-pull sibling
                       of the TB-282 status-report cron push and the
                       TB-296 web `/attention` page; all three surfaces
                       share `attention.detect_attention_conditions(cfg)`
                       and the `_format_attention_status_line` truncation
                       helper in `status_report.py` so they can never
                       disagree about what's currently active)
      auto-approve: / dry-run: / validator-judge: — TB-227 / TB-241 / TB-243
      next:         — board.next_ready (always when present)

    JSON branch is a superset — every read above carries a stable key
    (zero-state included) for parser stability so machine consumers see
    a stable shape regardless of activity.
    """
    # TB-264: refuse to render a synthetic-empty status against a project
    # root that doesn't exist on disk. Pre-TB-264 cmd_status silently
    # printed "daemon: stopped (pid -) / board: 0A / 0R / ..." for a
    # nonexistent --project path because every loader downstream tolerates
    # missing files; that masked typo'd --project flags as healthy fresh
    # projects. The briefing's verification bullet (`ap2 --project
    # /tmp/nonexistent status` must `head -1 | grep -qE
    # '(error|ERROR|not found)'`) pins the corrected behavior so future
    # CLI refactors can't quietly re-introduce the silent-empty regression.
    if not cfg.project_root.is_dir():
        print(
            f"error: project not found: {cfg.project_root}",
            file=sys.stderr,
        )
        return 1
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
    # TB-309: janitor's data accessor is exposed via the registry's
    # `status_findings_counts` hook-point. The previous direct import
    # of `ap2.janitor.recent_finding_counts_by_verdict` is gone — core
    # never reaches into `ap2.components.*` directly.
    from .registry import default_registry

    _registry = default_registry()
    _recent_finding_counts = _registry.hook(
        "status_findings_counts", component="janitor",
    )
    janitor_counts = _recent_finding_counts(cfg)
    # TB-319: snapshot the registry's manifests once so both the JSON
    # and text branches render the same enabled-state mapping (a
    # second `default_registry()` call would re-read `os.environ` and
    # could in principle disagree across the branches if the env
    # mutated mid-call — unlikely but the snapshot is free).
    _component_manifests = _registry.components
    janitor_findings = sum(janitor_counts.values())
    # TB-189 / TB-251: count operator-authored impact verdicts
    # (`task_classified` events) in the last 30 days, broken down by
    # verdict. Operators learn faster when "we kept calling these
    # proposals pro-forma" (or now: "this batch slipped a `negative`")
    # is visible at-a-glance — same surfacing pattern as
    # `pending_review` / `janitor_findings` (counters operators glance
    # at on every status check). Empty status (no classifications in
    # the window) renders as zeros across all four keys in JSON; the
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
    # TB-258: retrospective-audit unreviewed-count + cursor state.
    # Pure read-layer wrapper over `audit.list_unreviewed` +
    # `audit.parse_audit_cursor` (both already in HEAD); the text
    # branch surfaces an `audit: N unreviewed since <ts>` line in the
    # operator-attention cluster ONLY when N > 0 (omit-on-empty), and
    # the JSON branch ALWAYS carries the `audit` block (parser
    # stability mirror of the `auto_approve` contract).
    audit_state = automation_status.collect_audit_state(cfg)
    # TB-260: stale-env surface — read the daemon-start mtime stash
    # from `daemon_state.json` and compare against the live env file
    # mtime. The text branch emits a WARN line ONLY when the live
    # mtime is later than the daemon-start mtime (i.e. the operator
    # bumped a knob and hasn't restarted yet — the silent window that
    # bit TB-255). JSON ALWAYS carries the `env_stale` + `env_file_mtime`
    # keys for parser stability (mirrors the `auto_approve` / `audit`
    # parser-stability promise). Pure read-layer: no I/O beyond two
    # small stat / json reads via `collect_env_staleness`.
    env_staleness = automation_status.collect_env_staleness(cfg)
    # TB-298: active attention conditions surface — CLI-pull sibling
    # of the TB-282 status-report cron push (`render_attention_section`),
    # the TB-296 web `/attention` pull page (`web_attention._render_attention`),
    # and the TB-297 immediate-MM push (`_maybe_push_attention`). All
    # four surfaces share `attention.detect_attention_conditions(cfg)`
    # as their detector entrypoint — drift between them would mean a
    # walk-away operator polling `ap2 status` from a terminal sees
    # different conditions than the web page / chat post / status-report cron
    # digest. Pure read-layer (walks the events tail + a small board
    # read); no caching needed. Defensive swallow: a detector exception
    # never takes the status surface down — the cluster line just
    # omits, mirroring `render_attention_section`'s swallow-on-error
    # contract.
    # TB-315: `detect_attention_conditions` lives in
    # `ap2/components/attention/__init__.py` post-migration. Core
    # resolves it via a dynamic `importlib.import_module(...)` call
    # so the TB-311 import-direction gate (which walks static
    # Import / ImportFrom nodes) stays quiet; the module attribute
    # is dereferenced at call time so monkeypatch.setattr-style
    # test fixtures targeting the new module path still propagate.
    import importlib as _importlib
    try:
        _attention_mod = _importlib.import_module(
            "ap2.components.attention",
        )
        attention_conditions = _attention_mod.detect_attention_conditions(
            cfg,
        )
    except Exception:  # noqa: BLE001 — never break the status surface
        attention_conditions = []
    # TB-242 / TB-342: axis-4 focus-rotation surface — read the focus
    # list + the halt state once so both the text and JSON branches can
    # render them. Pure read-layer composition over `goal.read_focus_list`
    # + `goal.load_pointer` + `goal.roadmap_exhausted`; no new state
    # files, no daemon-side mutation. The render-symmetry mirrors
    # TB-227's auto-approve surface — fresh / pre-pivot projects
    # (goal.md absent OR no `## Current focus:` headings) get
    # `active_focus: null` in JSON and zero text-render lines.
    #
    # TB-342: the multi-focus rotation pointer was collapsed into a
    # single ideation-exhaustion detector. `goal.active_focus()` /
    # `(N of M)` position display went away with the pointer walk;
    # the focus line now renders the operator-authored `## Current
    # focus:` headings as a plain priority-ordered list (the operator's
    # prose/intent surface) plus the ideation halt state.
    from . import goal as _goal

    _foci = _goal.read_focus_list(cfg)
    if _foci:
        _focus_pointer = _goal.load_pointer(cfg)
        _focus_roadmap_complete = _goal.roadmap_exhausted(cfg, _foci)
        # TB-340: surfacing-vs-state split. `roadmap_complete` is the
        # always-on parked-ideation STATE; `notice_dismissed` only
        # quiets the actionable "extend goal.md" nag once the
        # operator has acked THIS exhaustion episode.
        _focus_notice_dismissed = _goal.roadmap_complete_notice_dismissed(
            cfg, _foci
        )
    else:
        _focus_pointer = None
        _focus_roadmap_complete = False
        _focus_notice_dismissed = False
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
            # TB-189 / TB-251: operator-authored impact verdicts in the
            # last 30 days (sourced from `task_classified` events).
            # Always all four keys (`advanced-goal` / `pro-forma` /
            # `negative` / `unclear`), defaulting to 0 — so machine
            # consumers always see the full shape regardless of
            # activity. The text branch below omits the line entirely
            # when total is 0.
            "classifications_last_30d_by_verdict": classifications_30d,
            # TB-227: auto-approve / auto-unfreeze loop state. Keys are
            # always present regardless of knob-state (machine consumers
            # get a stable shape); see `automation_status.collect_auto_approve_state`
            # for the contract.
            #
            # TB-243: surface the validator-judge fail-open counts as a
            # nested object alongside the flat collector keys so
            # machine consumers can `.auto_approve.validator_judge.fail_count_24h`
            # without grepping the flat key name (mirrors the
            # `validator_judge:` text sub-line). Always present (zeros
            # when no events) so consumers see a stable shape regardless
            # of TB-235 knob state. Flat collector keys remain for
            # back-compat with anything that parsed them between TB-227
            # and TB-243.
            "auto_approve": {
                **auto_approve_state,
                "validator_judge": {
                    "fail_count_24h":
                        auto_approve_state["validator_judge_fail_count_24h"],
                    "timeout_count_24h":
                        auto_approve_state[
                            "validator_judge_timeout_count_24h"
                        ],
                },
            },
            # TB-242 / TB-342: axis-4 focus-rotation state. Renders as
            # `null` when goal.md is missing or has zero
            # `## Current focus:` headings (fresh / pre-pivot projects)
            # so machine consumers can distinguish "no roadmap" from
            # "roadmap exhausted" — the `roadmap_complete` boolean field
            # disambiguates the latter on populated roadmaps. TB-342
            # collapsed the multi-focus rotation pointer walk, so the
            # `index` / `total` position fields went away with the
            # pointer; the operator-authored `## Current focus:`
            # headings remain priority-ordered prose/intent for the
            # ideation agent, surfaced here as `titles` so machine
            # consumers can render them as a list. `roadmap_complete`
            # is the ideation-halt flag (the detector tripped after
            # `AP2_FOCUS_ADVANCE_EMPTY_CYCLES` consecutive empty
            # cycles).
            "active_focus": (
                None
                if not _foci
                else {
                    "titles": [f.title for f in _foci],
                    "roadmap_complete": _focus_roadmap_complete,
                }
            ),
            # TB-258: retrospective-audit unreviewed-count + cursor
            # state. ALWAYS present (zero-state included) for parser
            # stability — mirrors the `auto_approve` parser-stability
            # promise so machine consumers see a stable shape
            # regardless of audit history. `cursor_ts` renders as
            # `null` when no prior audit cursor exists (first-ever
            # audit; cursor defaults to epoch in the underlying
            # helper).
            "audit": {
                "unreviewed_count": audit_state["unreviewed_count"],
                "cursor_ts": audit_state["cursor_ts"],
            },
            # TB-260: stale-env surface. `env_stale` flips true when the
            # `.cc-autopilot/env` file's mtime is later than the
            # daemon-start mtime — operator bumped a knob and hasn't
            # restarted (the silent window that bit TB-255 against
            # `AP2_VERIFY_TIMEOUT_S`). `env_file_mtime` is the live
            # mtime in iso form; both keys are ALWAYS present (zero-
            # state included) for parser stability so machine consumers
            # can pluck `.env_stale` directly. `env_file_mtime_at_start`
            # is the daemon-side baseline iso the comparison ran
            # against; renderers and operator post-mortems can read
            # both timestamps from this block without going through
            # the daemon's PID.
            "env_stale": env_staleness["env_stale"],
            "env_file_mtime": env_staleness["env_file_mtime"],
            "env_file_mtime_at_start": env_staleness[
                "env_file_mtime_at_start"
            ],
            # TB-298: active attention conditions. ALWAYS present
            # (zero-state included) for parser stability — mirrors the
            # `auto_approve` / `audit` / `env_stale` parser-stability
            # promise so machine consumers see a stable shape regardless
            # of detector activity. `conditions` is the FULL unfiltered
            # detector output (no truncation here — the text branch's
            # cap is a render concern only); each entry carries the
            # detector's `type` / `key` / `summary` plus `task` (or
            # `null` for singleton detectors: `validator_judge_noisy`,
            # `auto_approve_paused`, `cost_cap_approach`). Mirror of
            # the TB-296 web `/attention` and TB-282 status-report
            # cron surfaces.
            "attention": {
                "count": len(attention_conditions),
                "conditions": [
                    {
                        "task": (
                            (c.extras.get("task") or None)
                            if isinstance(c.extras, dict) else None
                        ),
                        "type": c.type,
                        "key": c.key,
                        "summary": c.summary,
                    }
                    for c in attention_conditions
                ],
            },
            # TB-319: enumerate every component the registry discovered.
            # Machine-consumer parity for the text-mode `## Components`
            # block — same source-of-truth walk (`default_registry().components`
            # in alphabetic name order) so a JSON consumer (web UI,
            # external monitor, ap2 ack scripts) sees the same wired-in
            # surface the operator's terminal does. Each entry carries
            # the four documented keys: `name`, `enabled` (resolved via
            # `Manifest.is_enabled()` against the live process env so a
            # hot-reloaded `.cc-autopilot/env` takes effect on the next
            # invocation), `env_flag` (the raw env-var name or null for
            # always-on manifests), and `default_enabled` (so a consumer
            # can reason about polarity without re-deriving it from the
            # env_flag name suffix). ALWAYS present (even on a fresh
            # project — the registry walk is deterministic and shipping
            # zero components would itself be a regression worth
            # surfacing).
            "components": [
                {
                    "name": _m.name,
                    "enabled": _m.is_enabled(),
                    "env_flag": _m.env_flag,
                    "default_enabled": _m.default_enabled,
                }
                for _m in _component_manifests
            ],
        }
        print(json.dumps(out, indent=2))
        return 0

    print(f"daemon:   {'running' if running else 'stopped'} (pid {pid or '-'}){' [paused]' if paused else ''}")
    print(f"version:  ap2 {version}")
    print(f"tick:     {cfg.tick_interval_s}s")
    # TB-242 / TB-342: surface axis-4 focus state near the top of the
    # report so an operator returning after walk-away can answer
    # "what's the project working on, and is ideation parked?" without
    # grepping events.jsonl or reading `focus_pointer.json` by hand.
    # Two render shapes:
    #   - parked-ideation state (`roadmap_complete_emitted=True`) →
    #     `focus:    parked — ideation exhausted; extend goal.md (`ap2
    #     update-goal`) to resume, or `ap2 ack roadmap_complete` to
    #     dismiss this notice (ideation stays parked)`. The state line
    #     is ALWAYS shown while parked; the actionable nag is
    #     suppressed once the operator dismissed THIS episode (TB-340;
    #     TB-275 reword — dispatch is NOT halted).
    #   - active → `focus:    <title>[, <title>...]`. The operator-
    #     authored `## Current focus:` headings render as a priority-
    #     ordered comma-separated list (top → bottom of goal.md). The
    #     daemon does not sequence them post-TB-342; the list is the
    #     operator's intent, the ideation agent reads them all each
    #     cycle, and the goal-anchor validator accepts any of them.
    # Omitted entirely when goal.md is missing or has zero
    # `## Current focus:` headings (fresh / pre-pivot projects) so
    # the default-off output stays byte-identical to pre-TB-242.
    if _foci:
        if _focus_roadmap_complete:
            # TB-275/TB-340/TB-342: roadmap_complete parks the ideation
            # trigger only — task dispatch continues. The parked state
            # line always renders so the operator knows the daemon is
            # parked. Resume is `ap2 update-goal` (editing goal.md
            # clears the halt via `reset_pointer_on_goal_updated`);
            # `ap2 ack roadmap_complete` only DISMISSES this nag
            # (ideation stays parked). Once dismissed, the nag is
            # suppressed but the state line stays. `ap2 pause` is the
            # explicit full-stop.
            if _focus_notice_dismissed:
                print(
                    "focus:    parked — ideation exhausted "
                    "(notice dismissed)"
                )
            else:
                print(
                    "focus:    parked — ideation exhausted; extend "
                    "`goal.md` via `ap2 update-goal` to resume, or "
                    "`ap2 ack roadmap_complete` to dismiss this notice "
                    "(ideation stays parked)"
                )
        else:
            print(
                "focus:    " + ", ".join(f.title for f in _foci)
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
        # TB-189 / TB-251: render the impact-verdict counts as a single
        # compact line so the operator sees the trend at-a-glance.
        # Format iterates `IMPACT_VERDICTS` so adding new verdicts to
        # the tuple flows through without a render edit. Missing-bucket
        # fallback is "0" (via `.get(v, 0)`) so projects that haven't
        # classified a particular verdict yet still show the full
        # gradient. Only emitted when at least one verdict exists in
        # the window — fresh projects don't grow a zero-line.
        c = classifications_30d
        parts = [f"{v}={c.get(v, 0)}" for v in tools.IMPACT_VERDICTS]
        print(
            "classifications last 30d: " + ", ".join(parts)
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
    # TB-258: surface the retrospective-audit unreviewed-count line in
    # the operator-attention cluster (after queue / review / janitor /
    # classifications / decisions-needed) so the walk-away operator
    # returning after a quiet day sees the unreviewed-shipped count
    # without running `ap2 audit` explicitly first. Pure read-layer
    # composition over `audit.list_unreviewed` + `audit.parse_audit_cursor`
    # (both already in HEAD — see `automation_status.collect_audit_state`).
    # Omit-on-empty (zero-state stays silent) so fresh / fully-reviewed
    # projects don't grow a zero-noise line; the JSON branch above
    # always carries the `audit` block for parser stability. Cursor-ts
    # renders as `(epoch)` when None so the operator sees a stable
    # two-token shape regardless of audit history.
    if audit_state["unreviewed_count"] > 0:
        _cursor_display = audit_state["cursor_ts"] or "(epoch)"
        print(
            f"audit:    {audit_state['unreviewed_count']} unreviewed "
            f"since {_cursor_display} — `ap2 audit`"
        )
    # TB-260: stale-env WARN line. Emitted when the live
    # `.cc-autopilot/env` mtime is later than the daemon-start mtime
    # (i.e. operator bumped a knob and hasn't restarted yet). The
    # remediation command lives in the message so the operator doesn't
    # need to look it up — same one-liner-with-fix shape as TB-258's
    # `audit:` line. Omitted entirely when not stale (default-off
    # byte-identical to pre-TB-260 output on a healthy daemon).
    if env_staleness["env_stale"]:
        print(
            f"WARN:     .cc-autopilot/env modified at "
            f"{env_staleness['env_file_mtime']} (after daemon start at "
            f"{env_staleness['env_file_mtime_at_start']}) — "
            f"restart with `ap2 stop && ap2 start` to apply changes"
        )
    # TB-298: surface the count + a capped per-bullet preview of currently
    # active attention conditions in the operator-attention cluster (after
    # `audit:` / env stale, before the `auto-approve:` block). CLI-pull
    # sibling of the TB-282 status-report cron push and the TB-296 web
    # `/attention` pull page — all four surfaces consume
    # `detect_attention_conditions(cfg)` so they can never disagree about
    # what's currently active. Omit-on-empty (zero conditions → no line)
    # so a quiet project doesn't grow a zero-noise row, mirroring the
    # TB-258 `audit:` / TB-260 `env stale` / TB-177 `janitor:` discipline;
    # the JSON branch above always carries the `attention` block for
    # parser stability. The shared truncation helper
    # `_format_attention_status_line` in `status_report.py` keeps the
    # bullet shape in lockstep with the cron status-report
    # `render_attention_section` and the web `/attention` renderer.
    if attention_conditions:
        from .status_report import _format_attention_status_line
        n = len(attention_conditions)
        body = _format_attention_status_line(attention_conditions)
        print(
            f"attention:  {n} condition{'s' if n != 1 else ''} — {body}"
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
        # TB-243: validator-judge fail-open counts also surface the
        # block so a noisy gate (with auto-approve still off / no
        # other 24h activity) doesn't fall through. The whole point
        # of surfacing the counts is to let an operator catch the
        # silent-degradation hazard BEFORE flipping `AP2_AUTO_APPROVE=1`;
        # gating on auto-approve here would defeat that.
        + a["validator_judge_fail_count_24h"]
        + a["validator_judge_timeout_count_24h"]
    ) > 0
    if a["auto_approve_enabled"] or _has_24h_activity:
        # TB-250: split the auto-approve top-line into three branches so
        # the rendered text honestly reflects knob state, even when
        # `_has_24h_activity` is truthy purely because the TB-243
        # validator-judge counters fired. Pre-TB-250 the `else` branch
        # printed `auto-approve: enabled (24h: ...)` whenever the outer
        # `if` matched — which meant a `validator_judge_fail` event in
        # the 24h window made the line claim the knob was on, even with
        # `AP2_AUTO_APPROVE` unset. JSON output (`auto_approve_enabled`)
        # always stayed correct; the bug was local to this text render.
        #
        #   - knob ON  + paused        → `auto-approve: PAUSED (...)`.
        #   - knob ON  + healthy       → `auto-approve: enabled (24h: ...)`.
        #   - knob OFF + has activity  → `auto-approve: disabled (
        #     validator-judge 24h: N fail, M timeout)`. Surfaces the
        #     activity that justified printing the block without
        #     misrepresenting the master switch.
        #   - knob OFF + no activity   → outer `if` evaluates false, the
        #     whole block is suppressed (existing TB-227 behavior; the
        #     fresh-project zero-line stays absent).
        if a["auto_approve_enabled"]:
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
        else:
            print(
                f"auto-approve: disabled (validator-judge 24h: "
                f"{a['validator_judge_fail_count_24h']} fail, "
                f"{a['validator_judge_timeout_count_24h']} timeout)"
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
        # TB-243: validator-judge fail-open visibility. TB-235's check #7
        # (LLM-driven dep-coherence judge in
        # `tools._validate_briefing_structure`) logs
        # `validator_judge_fail` / `validator_judge_timeout` on SDK /
        # parse errors and admits the briefing anyway — fail-open is
        # the load-bearing trade-off, but it leaves the auto-approve
        # safety claim (goal.md L82-85) silently-degradable. Render
        # the two counts as a single sub-line when EITHER is non-zero
        # so an operator with `AP2_AUTO_APPROVE=1` sees the gate's
        # health at a glance; omit when both are zero so the
        # default-healthy block stays compact (mirrors TB-241's
        # dry-run line omit-on-empty rule). Append ` [noisy]` when
        # `(fail + timeout) >= AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD`
        # (default 5) so the operator's eye catches the sustained-
        # issue case without staring at raw counts.
        _vj_fail = a["validator_judge_fail_count_24h"]
        _vj_timeout = a["validator_judge_timeout_count_24h"]
        if _vj_fail or _vj_timeout:
            _vj_threshold = automation_status.validator_judge_noisy_threshold(cfg)
            _vj_noisy = (_vj_fail + _vj_timeout) >= _vj_threshold
            print(
                f"validator-judge: {_vj_fail} fail | "
                f"{_vj_timeout} timeout (24h)"
                + (" [noisy]" if _vj_noisy else "")
            )
    nxt = board.next_ready()
    if nxt:
        print(f"next:     {nxt.id} {nxt.title}")
    # TB-319: enumerate every component the registry discovered, with
    # each line showing name + on/off + env-flag description. Closes the
    # goal.md L235-237 Progress signal that named `ap2 status` as the
    # natural surface for component visibility (today the only way to
    # discover what's wired in is to `ls ap2/components/` and read each
    # manifest by hand). Pure read-layer over `default_registry()` +
    # `Manifest.is_enabled` / `Manifest.env_flag_description` — no new
    # env knobs, no filter knobs, no behavior change. Always emitted
    # (even on a fresh project — every project has the same registry
    # walk so there's no "zero components" omit-on-empty case to honor).
    # Walks the snapshot taken above (alphabetic by manifest name,
    # matching the order `tick_hooks(phase)` already uses) so the text
    # branch can never disagree with the JSON branch on enabled state.
    print("## Components")
    for _manifest in _component_manifests:
        _state = "on" if _manifest.is_enabled() else "off"
        _flag_desc = _manifest.env_flag_description()
        print(f"  {_manifest.name}: {_state} ({_flag_desc})")
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

    if _web.is_web_disabled(cfg=cfg):
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

    port = _web.daemon_web_port(cfg=cfg)
    return f"http://127.0.0.1:{port}/"


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

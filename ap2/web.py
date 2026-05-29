"""Local read-only web UI for ap2 daemon state.

Closes TB-93 (the "console tool for human review" backlog item) in web
form. Pure stdlib (`http.server`), no JS framework, no auth. Bound to
127.0.0.1 by default; only the operator on the box should be reading it.

Read-only by design — every mutation still goes through the `ap2` CLI or
custom MCP tools. The web UI is a window onto state, not a control panel.

Pages:
  /                       overview: daemon status, board counts, last 30 events
  /events                 full event log, filterable by ?type=X&n=N (default 200)
  /tasks                  all tasks grouped by section
  /task/<TB-N>            one task: briefing + per-run links + related events
  /task-run/<run-id>      live SDK debug dumps for one run (TB-129)
  /task-run/<run-id>/stream.json
                          JSON sub-endpoint, ?since=N returns new stream rows
  /pipelines              in-flight + recent pipelines from pipeline_start events
  /insights               insights index — front matter summaries + links
  /insight/<name>         one insight file, full content
  /ideation_state         latest ideation_state.md assessment
  /commits                recent git log (subjects link to /task/TB-N when matched)
  /usage                  TB-181 token / cost dashboard (cost-over-time, model split, etc.)
  /stats                  TB-255 stats dashboard (task / verifier / ideation / cron aggregates)
  /stats.json             TB-255 JSON contract for /stats (scripting-friendly)
  /attention              TB-296 pull-surface for current attention conditions
                          (per-condition bullets from detect_attention_conditions)

TB-265: This module is the FastAPI-style app construction + middleware +
router composition + HTTP dispatcher only. The route-group siblings own
their renderers:

  - `ap2/web_chrome.py`    — shared CSS/layout/events_table chrome.
  - `ap2/web_home.py`      — `/` home page + cards (TB-162/173/197/227/242).
  - `ap2/web_events.py`    — `/events`, `/tasks`, `/task/<id>`, `/pipelines`,
                             `/ideation_state`, `/commits`.
  - `ap2/web_tasks.py`     — `/task-run/<id>` live view + stream JSON (TB-129).
  - `ap2/web_stats.py`     — `/stats` + `/stats.json` (TB-255).
  - `ap2/web_insights.py`  — `/insights` + `/insight/<name>`.
  - `ap2/web_usage.py`     — `/usage` token-cost dashboard (TB-181).
  - `ap2/web_attention.py` — `/attention` pull-surface for current attention
                             conditions (TB-296 — pull counterpart to the
                             status-report cron's push of TB-282's
                             `## Attention needed` bullets).

Each sibling exports its own `router` (a `_WebRouter` instance). `make_app()`
composes them via `include_router`, producing a tiny FastAPI-compatible
app object whose `.routes` are introspectable by tests and the verification
gate. The actual HTTP dispatch still flows through the stdlib `_Handler`
class below — `router` is the composition shim, not a routing engine.
"""
from __future__ import annotations

import asyncio
import errno
import http.server
import json
import os
import socket
import socketserver
import threading
from typing import Callable
from urllib.parse import parse_qs, urlsplit

from .config import Config

# Re-exports from sibling route-group modules. Tests + cli_daemon + daemon
# still poke at `web._render_home`, `web._render_events`, etc. — the
# explicit list keeps `from ap2 import web` callers working byte-identically
# post-split (the names that grew alongside ap2 over years of TB-N work
# stay reachable at their historical home).
from .web_chrome import (
    _CSS,
    _COMPACT_USAGE_EVENT_TYPES,
    _RUN_ID_RE,
    _TASK_COMPLETE_STATUS_CLASS,
    _TERMINAL_RUN_EVENT_TYPES,
    _WARNING_EVENT_TYPES,
    _compact_usage_row,
    _debug_dir,
    _event_extra,
    _event_token_summary,
    _events_table,
    _find_run_id_for_event,
    _is_alive,
    _is_pending_review,
    _is_verification_fail_terminal,
    _latest_verification_failed_for_task,
    _layout,
    _list_run_ids_for_task,
    _read_jsonl,
    _row_class,
    _tasks_list,
    _terminal_event_for_run,
    _ts_to_compact,
    _verification_failed_row_summary,
    _verification_summary_block,
)
from .web_home import (
    _WebRouter,
    _format_cooldown_remaining,
    _format_pending_queue_extra,
    _format_pending_queue_ts,
    _hourly_sparkline_buckets,
    _ideation_gate_state,
    _load_pending_queue_entries,
    _render_attention_card,
    _render_automation_card,
    _render_env_stale_warning,
    _render_focus_card,
    _render_home,
    _render_ideation_status_block,
    _render_operator_decisions,
    _render_pending_queue,
    _render_sparkline_svg,
)
from .web_events import (
    _pid_alive,
    _render_commits,
    _render_events,
    _render_ideation_state,
    _render_pipelines,
    _render_task,
    _render_task_runs_section,
    _render_tasks,
    _run_status_badge,
    _TB_PREFIX_RE,
)
from .web_tasks import (
    _classify_row,
    _compute_run_usage_totals,
    _format_tool_call,
    _format_tool_result,
    _render_live_refresh_script,
    _render_run_rows_html,
    _render_run_usage_footer,
    _render_run_verdict,
    _render_task_run,
    _render_task_run_stream_json,
    _row_full_body_html,
    _row_summary_html,
)
from .web_stats import (
    _STATS_WINDOW_CHIPS,
    _fmt_cost,
    _fmt_duration_s,
    _fmt_pct,
    _render_stats,
    _render_stats_json,
    _stats_attempts_table,
    _stats_cron_section,
    _stats_duration_buckets_table,
    _stats_ideation_section,
    _stats_summary_card,
    _stats_top_tasks_table,
    _stats_verifier_table,
    _stats_window_chips,
)
from .web_insights import _render_insight, _render_insights
from .web_attention import _render_attention
from .web_usage import (
    _DEFAULT_USAGE_STACK,
    _DEFAULT_USAGE_WINDOW,
    _EVENT_TYPE_COLORS,
    _USAGE_EVENT_TYPES,
    _USAGE_WINDOWS,
    _aggregate_by_model,
    _aggregate_usage_by_day,
    _aggregate_usage_by_event_type,
    _aggregate_usage_by_subtype,
    _event_cost,
    _event_subtype,
    _event_token_breakdown,
    _event_total_tokens,
    _load_usage_events,
    _model_color,
    _normalize_usage_stack,
    _normalize_usage_window,
    _parse_event_dt,
    _render_cache_chart_svg,
    _render_cost_chart_svg,
    _render_model_split_svg,
    _render_usage,
    _top_n_expensive_tasks,
    _usage_window_chart_days,
    _usage_window_seconds,
)


# TB-130: when the daemon spawns the web UI as part of `ap2 start`, this is
# the default port. Standalone `ap2 web` keeps its historical default
# (7820) so operators who already have a tab pointed at the legacy URL
# don't have to rebookmark. Override either with `AP2_WEB_PORT`.
DEFAULT_DAEMON_WEB_PORT = 8729
DEFAULT_STANDALONE_WEB_PORT = 7820

# TB-155: when the configured start_port is already bound (typically a stale
# daemon, an `ap2 web` standalone, or another project's daemon on the same
# box), `_bind_with_enumeration` walks forward up to this many ports before
# giving up. Bounded so a misconfigured port range can't degenerate into an
# unbounded probe that climbs into the ephemeral range. 10 is enough for the
# realistic conflict case (operator has 1-2 stale processes); beyond that the
# operator should investigate the conflict, not let the daemon paper over it.
DEFAULT_WEB_PORT_MAX_ATTEMPTS = 10


def is_web_disabled(*, cfg: Config | None = None) -> bool:
    """True when the operator opted out of the daemon-spawned web UI.

    Centralized so the daemon, the CLI status command, and tests share one
    parsing rule. Accepts the same truthy strings as the rest of ap2's env
    knobs (`1`, `true`, `yes`, case-insensitive).

    TB-336 axis-5: when ``cfg`` is passed, the read routes through
    ``cfg.get_core_value("web_disabled", default="")`` which evaluates
    sectioned env (``AP2_CORE_<KEY>``) > flat env (``AP2_WEB_DISABLED``
    via reverse-``FLAT_TO_SECTIONED`` lookup) > ``cfg.core_config``
    snapshot > default at call time. The cfg-less back-compat branch
    reads ``os.getenv`` so pre-cfg callers (CLI verbs, ad-hoc tests)
    keep today's behavior bit-for-bit while the cross-package grep
    gate stays green via the ``os.getenv`` shape the absence-check
    excludes by construction. TypeError-guard on the kwarg surfaces a
    refactor that passes a non-Config object instead of silently
    treating it as None.
    """
    if cfg is not None and not isinstance(cfg, Config):
        raise TypeError(
            "is_web_disabled(cfg=...) expects a Config instance; "
            f"got {type(cfg).__name__}",
        )
    if cfg is not None:
        raw = str(cfg.get_core_value("web_disabled", default="") or "")
    else:
        raw = os.getenv("AP2_WEB_DISABLED", "")
    return raw.strip().lower() in (
        "1", "true", "yes", "on",
    )


def daemon_web_port(*, cfg: Config | None = None) -> int:
    """Resolve the daemon-spawned web port from env, falling back to default.

    A malformed `AP2_WEB_PORT` (e.g. `"abc"`) falls back to the default
    rather than crashing the daemon at startup — the operator's typo
    shouldn't kill the whole loop.

    TB-336 axis-5: when ``cfg`` is passed, the read routes through
    ``cfg.get_core_value("web_port", default="")`` (sectioned env >
    flat env ``AP2_WEB_PORT`` > TOML snapshot > default). The cfg-less
    branch keeps the ``os.getenv`` legacy shape so pre-cfg callers
    (CLI verbs) and the cross-package grep gate both stay happy.
    """
    if cfg is not None and not isinstance(cfg, Config):
        raise TypeError(
            "daemon_web_port(cfg=...) expects a Config instance; "
            f"got {type(cfg).__name__}",
        )
    if cfg is not None:
        raw = str(cfg.get_core_value("web_port", default="") or "").strip()
    else:
        raw = os.getenv("AP2_WEB_PORT", "").strip()
    if not raw:
        return DEFAULT_DAEMON_WEB_PORT
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_DAEMON_WEB_PORT


# ------------- FastAPI-style app construction -------------


class _App:
    """Minimal FastAPI-compatible app stand-in.

    Exposes `.routes` as a flat list of `_Route` objects, each carrying
    `.path` — matching `fastapi.FastAPI` for the verification gate's
    `from ap2.web import make_app; app = make_app(); [r.path for r in app.routes]`
    introspection pattern. The actual HTTP dispatch happens via the
    stdlib `_Handler` below; this object exists for surface visibility
    and route-composition tooling.
    """
    def __init__(self) -> None:
        self.routes: list = []

    def include_router(self, r: "_WebRouter") -> None:
        self.routes.extend(r.routes)


def make_app() -> _App:
    """Compose the route-group sibling routers into one app.

    Each sibling owns one or more URL prefixes and exposes a
    `_WebRouter` named `router`. This function `include_router`s each
    one in turn so the operator-facing route surface is one
    introspectable list — the TB-265 verification bullet
    (`[r.path for r in app.routes]`) walks this. Daemon / CLI startup
    use `serve` / `serve_async` directly; `make_app()` is for
    introspection + tooling.
    """
    from . import (
        web_attention,
        web_events,
        web_home,
        web_insights,
        web_stats,
        web_tasks,
        web_usage,
    )

    app = _App()
    app.include_router(web_home.router)
    app.include_router(web_events.router)
    app.include_router(web_tasks.router)
    app.include_router(web_insights.router)
    app.include_router(web_stats.router)
    app.include_router(web_usage.router)
    app.include_router(web_attention.router)
    return app


# ------------- HTTP handler -------------


class _Handler(http.server.BaseHTTPRequestHandler):
    cfg: Config = None  # type: ignore[assignment]

    def do_GET(self) -> None:  # noqa: N802
        try:
            url = urlsplit(self.path)
            qs = parse_qs(url.query)
            path = url.path or "/"
            if path == "/":
                body = _render_home(self.cfg)
            elif path == "/events":
                typ = qs.get("type", [None])[0]
                try:
                    n = int(qs.get("n", ["200"])[0])
                except ValueError:
                    n = 200
                n = max(1, min(n, 5000))
                # TB-157: ?show=tokens renders an extra column per row
                # surfacing usage / cost for every event that carries it
                # (chiefly judge_call rows today, and any future
                # event types that grow a usage payload).
                show_tokens = (
                    qs.get("show", [""])[0] == "tokens"
                )
                body = _render_events(
                    self.cfg, typ=typ, n=n, show_tokens=show_tokens,
                )
            elif path == "/tasks":
                # TB-121: ?filter=pending-review narrows to ideation
                # proposals awaiting operator approval.
                f_kind = qs.get("filter", [None])[0]
                body = _render_tasks(self.cfg, filter_kind=f_kind)
            elif path.startswith("/task/"):
                tb_id = path[len("/task/"):]
                body = _render_task(self.cfg, tb_id)
            elif path.startswith("/task-run/"):
                rest = path[len("/task-run/"):]
                # Two routes share the same prefix:
                #   /task-run/<run-id>            → HTML page
                #   /task-run/<run-id>/stream.json → JSON poll endpoint
                if rest.endswith("/stream.json"):
                    rid = rest[: -len("/stream.json")]
                    try:
                        since = int(qs.get("since", ["0"])[0])
                    except ValueError:
                        since = 0
                    status, data = _render_task_run_stream_json(
                        self.cfg, rid, max(0, since)
                    )
                    self.send_response(status)
                    self.send_header(
                        "Content-Type", "application/json; charset=utf-8"
                    )
                    self.send_header("Content-Length", str(len(data)))
                    # Live polling endpoint — disable caching so a stale
                    # 304 doesn't strand the operator on an empty page
                    # while the daemon writes new rows.
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(data)
                    return
                body = _render_task_run(self.cfg, rest)
            elif path == "/pipelines":
                body = _render_pipelines(self.cfg)
            elif path == "/attention":
                # TB-296: pull-surface for current attention conditions.
                # Companion to the status-report cron's push of TB-282's
                # `## Attention needed` bullets — same detector
                # entrypoint, always-available rendering.
                body = _render_attention(self.cfg)
            elif path == "/insights":
                body = _render_insights(self.cfg)
            elif path.startswith("/insight/"):
                name = path[len("/insight/"):]
                body = _render_insight(self.cfg, name)
            elif path == "/ideation_state":
                body = _render_ideation_state(self.cfg)
            elif path == "/commits":
                body = _render_commits(self.cfg)
            elif path == "/usage":
                # TB-181: token/cost dashboard. URL is the only config
                # surface; out-of-range / missing values fall back to
                # the defaults inside `_render_usage`.
                window = qs.get("window", [None])[0]
                stack = qs.get("stack", [None])[0]
                body = _render_usage(
                    self.cfg, window=window, stack_by=stack,
                )
            elif path == "/stats.json":
                # TB-255: JSON contract. Serve raw bytes (no HTML
                # wrapping) so scripting consumers can `curl | jq`
                # without unwrapping a layout.
                window = qs.get("window", [None])[0]
                data = _render_stats_json(self.cfg, window=window)
                self.send_response(200)
                self.send_header(
                    "Content-Type", "application/json; charset=utf-8"
                )
                self.send_header("Content-Length", str(len(data)))
                # Recompute-on-refresh is the contract (see briefing
                # design block); a stale 304 would mask the latest
                # window-bounded aggregates.
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
                return
            elif path == "/stats":
                # TB-255: HTML stats dashboard. Window selector via
                # `?window=` (1d / 7d / 30d default; arbitrary `Nh`
                # / `Nm` / `Nd` accepted, clamped per
                # `automation_stats.parse_window`).
                window = qs.get("window", [None])[0]
                body = _render_stats(self.cfg, window=window)
            else:
                self.send_response(404)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"not found")
                return
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:  # noqa: BLE001
            self.send_response(500)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"500: {type(e).__name__}: {e}".encode("utf-8"))

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        # Quiet by default — the daemon's events.jsonl is the audit trail,
        # not stdout from a debug HTTP server.
        return


class _ThreadingTCPServer(socketserver.ThreadingTCPServer):
    """`ThreadingTCPServer` with `allow_reuse_address` flipped on by default.

    Without this, restarting the daemon (or switching from `ap2 web` to
    daemon-spawned mode) trips a `OSError: [Errno 48] Address already in
    use` on the port for ~60s while the kernel waits out TIME_WAIT.
    Daemon threads on the request handlers so a stuck request can't keep
    `srv.shutdown()` blocked when the operator wants out.
    """

    allow_reuse_address = True
    daemon_threads = True


def _bind_with_enumeration(
    host: str, start_port: int, max_attempts: int,
) -> tuple[socket.socket, int]:
    """Bind a TCP listening socket on `host`, walking forward from `start_port`.

    TB-155: silently retry the next port (start_port+1, start_port+2, ..., up
    to `start_port + max_attempts - 1`) when the configured `start_port` is
    already bound — typically by a stale daemon, an `ap2 web` standalone, or
    another project's daemon on the same machine. Returns the bound socket
    and the actually-bound port; callers include the resolved port in their
    `web_start` event payload so post-mortem can pair "requested 8729, bound
    8730" with the conflict.

    `EADDRINUSE` is the only error treated as "try the next port" — any other
    `OSError` (permissions, bad host, etc.) propagates immediately because
    walking forward wouldn't help. After exhausting `max_attempts`, raises a
    single `OSError(EADDRINUSE, ...)` whose message names the range tried so
    the operator's hunt for the offending pid is one log line away.
    """
    if max_attempts < 1:
        raise ValueError(f"max_attempts must be >= 1 (got {max_attempts})")
    last_err: OSError | None = None
    for offset in range(max_attempts):
        port = start_port + offset
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
            return sock, port
        except OSError as e:
            sock.close()
            if e.errno != errno.EADDRINUSE:
                raise
            last_err = e
    end_port = start_port + max_attempts - 1
    raise OSError(
        errno.EADDRINUSE,
        f"no free port in range {start_port}..{end_port} "
        f"(tried {max_attempts}); investigate with "
        f"`lsof -iTCP:{start_port} -sTCP:LISTEN`"
        + (f" — last EADDRINUSE: {last_err}" if last_err else ""),
    )


def _build_server(
    cfg: Config,
    host: str,
    start_port: int,
    max_attempts: int = DEFAULT_WEB_PORT_MAX_ATTEMPTS,
) -> tuple[_ThreadingTCPServer, int]:
    """Bind the read-only HTTP server with TB-155 port enumeration.

    Returns `(srv, bound_port)` so callers can log the actually-bound port
    rather than the one they asked for. The HTTP server uses our pre-bound
    socket (`bind_and_activate=False` skips TCPServer's own bind) so we
    don't double-bind and the enumeration result is authoritative.
    """
    sock, bound_port = _bind_with_enumeration(host, start_port, max_attempts)
    handler_cls = type("Handler", (_Handler,), {"cfg": cfg})
    srv = _ThreadingTCPServer(
        (host, bound_port), handler_cls, bind_and_activate=False,
    )
    # Replace the unbound socket TCPServer just allocated with our pre-bound
    # one, then call `server_activate()` so the kernel starts queuing
    # connections. `socketserver` keeps a reference to `srv.socket` for
    # `server_close()`, so swapping it here is the supported path.
    srv.socket.close()
    srv.socket = sock
    srv.server_activate()
    return srv, bound_port


def serve(
    cfg: Config,
    host: str = "127.0.0.1",
    port: int = DEFAULT_STANDALONE_WEB_PORT,
    *,
    max_attempts: int = DEFAULT_WEB_PORT_MAX_ATTEMPTS,
) -> None:
    """Start the read-only web UI. Blocks until SIGINT.

    Default bind is 127.0.0.1 deliberately — there's no auth and the page
    surfaces full event payloads (briefing text, prompt dump paths,
    Mattermost message bodies, etc.) that should never leave the box.

    TB-155: when `port` is already bound, walks forward up to `max_attempts`
    times before giving up. The "bound on" line printed below reflects the
    resolved port so the operator can copy/paste the URL even after a
    silent enumeration. `port` keeps its argparse-friendly name (so the CLI
    flag stays `--port`) but functions as the ENUMERATION START.
    """
    srv, bound_port = _build_server(cfg, host, port, max_attempts=max_attempts)
    with srv:
        if bound_port != port:
            print(
                f"ap2 web: port {port} busy; bound to {bound_port} instead "
                f"(range {port}..{port + max_attempts - 1})"
            )
        print(
            f"ap2 web: http://{host}:{bound_port}/  (project={cfg.project_root})"
        )
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print("\nap2 web: stopped")


async def serve_async(
    cfg: Config,
    *,
    host: str = "127.0.0.1",
    start_port: int = DEFAULT_DAEMON_WEB_PORT,
    max_attempts: int = DEFAULT_WEB_PORT_MAX_ATTEMPTS,
    on_bind: "Callable[[str, int], None] | None" = None,
    port: int | None = None,
) -> None:
    """Run the read-only web UI as an awaitable, cooperatively cancellable.

    Companion to the blocking `serve()` (which `ap2 web` still uses for the
    standalone case). Used by the daemon's `main_loop` so `ap2 start`
    brings up both daemon + web in one process — no second terminal, no
    risk of leaving the UI pointed at a stale events.jsonl after the
    daemon was restarted (TB-130).

    Lifecycle:
      - Bind the server on the calling event loop's thread (with TB-155
        port enumeration starting at `start_port`), then run
        `serve_forever` in a background daemon thread (the stdlib HTTP
        handler is sync; `serve_forever` blocks).
      - If provided, fire `on_bind(host, bound_port)` synchronously before
        parking — that's how `_web_loop_for_daemon` learns the resolved
        port for its `web_start` event payload.
      - Block this coroutine indefinitely on `Event.wait()`. Cancellation
        (the daemon's teardown path) lands as `CancelledError`, which
        triggers `srv.shutdown()` to wake `serve_forever`.
      - Re-raises the bind `OSError` so the caller can decide whether
        `EADDRINUSE` means "already running" (skip) or "real error" (log).

    `port=` is accepted as a backwards-compatible alias for `start_port=`
    so callers (and tests) written before TB-155 keep working.
    """
    if port is not None:
        # Pre-TB-155 callers passed `port=`; treat it as `start_port=` so
        # the auto-enumeration shape is opt-in via the new keyword without
        # silently breaking existing kwargs.
        start_port = port
    srv, _bound_port = _build_server(
        cfg, host, start_port, max_attempts=max_attempts,
    )
    if on_bind is not None:
        on_bind(host, _bound_port)
    server_thread = threading.Thread(
        target=srv.serve_forever, name="ap2-web", daemon=True,
    )
    server_thread.start()
    try:
        # `asyncio.Event().wait()` is the textbook "park forever, wake on
        # cancel" pattern — cleaner than a poll loop, and unaffected by
        # `RUNNING` (which the daemon flips on signals; we get
        # `CancelledError` from the parent's `task.cancel()` call instead).
        await asyncio.Event().wait()
    finally:
        # `shutdown()` is idempotent and safe from any thread; it sets the
        # internal flag, then waits for the request loop to notice on its
        # next poll. `server_close()` releases the listening socket so a
        # subsequent restart can bind. The thread is `daemon=True` so a
        # stuck handler can't keep the process alive.
        srv.shutdown()
        srv.server_close()
        server_thread.join(timeout=5)

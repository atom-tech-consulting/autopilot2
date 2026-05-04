# Web port auto-enumerate on conflict

## Goal

When the daemon starts and the configured `AP2_WEB_PORT` (default 8729) is already bound by another process — typically a stale daemon, an `ap2 web` standalone, or another project's daemon on the same machine — silently try the next free port (port+1, port+2, ... up to a small bounded range) instead of emitting `web_error` and leaving the operator with no UI. Surface the port actually bound in the `web_start` event, in `ap2 status`'s `web:` line, and in the standalone-process bind path.

Today the bind failure path is hard: `_web_loop_for_daemon` translates the `OSError` into a `web_error` event and gives up. The operator has to hunt for the offending pid (usually their own prior daemon, or another project's daemon they forgot was running), kill it, and restart. Auto-enumeration handles this without intervention.

## Scope

Files to touch:

- `ap2/web.py` — `serve_async` (and any `serve_blocking` / equivalent) gain an optional `(host, start_port, max_attempts)` shape: bind to `start_port`; on `OSError` matching `EADDRINUSE`, try `start_port+1`, ..., up to `start_port + max_attempts - 1`. Return the port actually bound so callers can include it in their event payloads / log lines. Default `max_attempts = 10` (8729..8738) — bounded so a misconfigured port range doesn't degenerate into an unbounded probe.
- `ap2/daemon.py` — `_web_loop_for_daemon` consumes the actual-bound-port from `serve_async` and includes it in the `web_start` event's `port` field (it does this already; the change is making the value reflect the resolved port, not the input port).
- `ap2/cli.py` — `cmd_status`'s URL line reads from the `web_start` event in `events.jsonl` rather than recomputing from env (or, if reading env stays simpler, scans events.jsonl for the most recent `web_start` to report the resolved port).
- `ap2/cli.py` — `cmd_web` (the standalone `ap2 web` entry) uses the same enumeration.
- `ap2/web.py` — emit a `web_port_conflict` event (or fold into `web_start` with a `requested_port` + `bound_port` pair when they differ) so post-mortem can spot the conflict.
- Tests in `ap2/tests/test_web.py` and `ap2/tests/test_daemon_web.py`.

## Design

### Bind strategy

```python
def _bind_with_enumeration(host, start_port, max_attempts):
    for offset in range(max_attempts):
        port = start_port + offset
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port))
            return sock, port
        except OSError as e:
            if e.errno != errno.EADDRINUSE:
                raise
    raise OSError(
        f"no free port in range {start_port}..{start_port + max_attempts - 1}"
    )
```

`serve_async` builds on the bound socket rather than passing port directly to uvicorn; uvicorn's `Server` accepts pre-bound sockets via its config, so we don't double-bind.

### Event shape

Today's `web_start` has `{host, port, url}`. Extension: add `requested_port` when it differs from `port`. Backward-compatible — consumers that ignore the new field see the same `port` they did before, just resolved.

When all `max_attempts` ports fail, emit `web_error` (today's behavior) with a message naming the range tried, so the operator's hunt is easier:

```
web_error: no free port in range 8729..8738; previous web_start events
might point at the offending pid (port 8729 → daemon.pid in some prior
project? `lsof -iTCP:8729 -sTCP:LISTEN`)
```

### Standalone `ap2 web` parity

The standalone command uses port 7820 today (different default to avoid clobbering daemon's UI). Apply the same enumeration with a separate `max_attempts=10` window: 7820..7829. Same code path.

### `ap2 status` URL accuracy

Today `cmd_status` recomputes the URL from `AP2_WEB_PORT` env. Post-this-change, it should reflect the actual bound port. Two paths:

(1) Read the most recent `web_start` event from `events.jsonl` (canonical — matches the daemon's actual state). Cheap.
(2) Have the daemon write the resolved port to a small state file (`.cc-autopilot/web_state.json`) on bind. More indirection.

Pick (1) — events.jsonl is already polled for status surfaces, no new file needed.

### Why bounded enumeration, not unbounded

A misconfigured port range or a port-scanning environment shouldn't let the daemon climb into the ephemeral range and start binding random ports. 10 is enough for the realistic conflict case (operator has 1-2 stale daemons / standalones); beyond that the operator should investigate, not let auto-enumeration paper over it.

## Verification

- `uv run pytest -q ap2/tests/` — full regression gate passes (gating)
- `grep -qE "_bind_with_enumeration|EADDRINUSE" ap2/web.py` — enumeration helper present.
- New unit test in `test_web.py`: with port 8729 pre-bound by a fixture socket, `serve_async(host='127.0.0.1', start_port=8729)` returns a socket bound to 8730.
- New unit test: with ports 8729..8738 ALL pre-bound, `serve_async` raises a single error naming the range tried; no infinite loop.
- New unit test: with no conflicts, `serve_async(start_port=8729)` binds 8729 (resolved port equals requested port; no `requested_port` field in the event).
- New unit test in `test_daemon_web.py`: `web_start` event payload includes both `port` (bound) and `requested_port` (when they differ); URL field reflects the bound port.
- New unit test in `test_cli.py`: `cmd_status` prints the URL from the most recent `web_start` event, not from env. Pre-seed `events.jsonl` with a `web_start` carrying port 8731 and assert the printed URL contains `:8731`.
- New unit test: `cmd_web` (standalone) uses the same enumeration starting at 7820, picks 7821 when 7820 is taken.

## Out of scope

- Auto-killing the offending process on conflict — too aggressive, possible foot-gun (could be another project's legitimate daemon).
- Logging which pid holds the conflicting port — `lsof` shells out per port, slows startup, and platform-specific. Operator can `lsof -iTCP:<port>` themselves.
- Different `max_attempts` per surface (daemon vs standalone) beyond the single 10-port window. If a real use case for asymmetric ranges shows up, file separately.
- IPv6 / dual-stack binding — keep the existing 127.0.0.1 IPv4 behavior; multi-bind is a different problem.
- Surfacing the resolved port in the web UI itself (page footer or similar) — the URL the operator typed already reflects it.
## Attempts

### 2026-05-04 — verification_failed
(no summary)
- **kind:** per_task
- **failed_criteria:** [fail] New unit test: with no conflicts, `serve_async(start_port=8729)` binds 8729 (resolved port equals requested port; no `re
- **Debug dumps:** `prompt: .cc-autopilot/debug/20260504T062956Z-TB-155.prompt.md`, `stream: .cc-autopilot/debug/20260504T062956Z-TB-155.stream.jsonl`, `messages: .cc-autopilot/debug/20260504T062956Z-TB-155.messages.jsonl`

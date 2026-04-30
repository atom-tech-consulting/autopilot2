# TB-130 — Auto-start ap2 web as part of ap2 start; tie lifecycle to daemon

## Goal

Today the read-only web UI is a separate command (ap2 web) operators have to remember to launch in a second terminal — and remember to kill when the daemon stops. Bundle it into the daemon lifecycle so 'ap2 start' brings up both. Scope: (1) When daemon starts (cli.py cmd_start / daemon.py main_loop), spawn the web server in the same process via asyncio.create_task (uvicorn is already a dep — see uv.lock), bound to 127.0.0.1 on a fixed default port (suggest 8729; document in README). (2) Env-overridable: AP2_WEB_PORT to set port, AP2_WEB_DISABLED=1 to opt out (for headless / CI scenarios). (3) Lifecycle: web task gets cancelled when main_loop exits (RUNNING flag flips on SIGTERM/SIGINT — same teardown path as cron). Daemon_stop event already fires; web shutdown rides along. (4) ap2 web command stays available for the standalone case (ap2 not running, just want to view past events). (5) ap2 status should report the web URL when active so operators know where to point the browser. Why: current setup has a too-easy footgun where the web view runs against a stale events.jsonl after the daemon was restarted, or simply isn't running when needed. Coupling them removes the surprise.

## Scope

- (file / module to change)

## Design

(filled in by /tb prep or by the ideation agent)

## Verification

Concrete acceptance criteria the daemon's per-task verifier (TB-69)
runs after the agent's commit. Shell-command bullets (backtick-fenced
at the start of the bullet) are run automatically; prose bullets are
judged by an SDK call against the diff.

- `uv run pytest -q` — full suite passes
- (additional shell or prose bullets)

## Out of scope

- (filled in)

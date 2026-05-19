# Split `ap2/web.py` (179KB) by route group: home / events / task-run / stats / insights siblings

Tags: #autopilot #refactor #modularity #agent-friendliness #regression-pin

## Goal

`ap2/web.py` is currently 179KB containing the FastAPI app, middleware, route registration, page templates (HTML rendering), and JSON sub-endpoints for at minimum 5+ distinct surface areas — home page, `/events`, `/task-run/<id>` (TB-129 live detail view), `/stats` (TB-255 aggregates), `/insights/*`, plus their JSON sub-endpoints. Each route group has its own rendering helpers, query logic, and tests.

Web tasks (TB-129, TB-130, TB-148, TB-155, TB-256, TB-260, etc.) all load the full 179KB even when only touching one route's surface. The natural seam is by URL prefix — each route group is independently testable and rarely co-modified with the others.

Goal anchor: serves `goal.md` `## Done when` bullet "Failure recovery (verification fails, retries exhaust, daemon restart, cron drift, agent timeouts) is fully automatic; only genuine design forks escalate." Smaller per-module context loads on web-touching tasks reduce verify-timeout exposure (TB-256 was a web-rendering fix that needed to load all of `web.py` to change one card-rendering function).

Why now: web.py is the largest of the four splits in this batch by absolute size after tools.py. The route-group seam is clean — FastAPI's `APIRouter` was designed for exactly this composition pattern.

## Scope

- Keep `ap2/web.py` as the FastAPI app + middleware + router composition + uvicorn entrypoint.
- Lift route groups to focused sibling modules at the flat `ap2/` level using FastAPI's `APIRouter`. Suggested split (agent picks exact boundaries):
  - `ap2/web_home.py` — `/` home page + any home-specific JSON endpoints.
  - `ap2/web_events.py` — `/events` page + JSON sub-endpoint.
  - `ap2/web_tasks.py` — `/task-run/<id>` page + stream JSON sub-endpoint (TB-129).
  - `ap2/web_stats.py` — `/stats` page + `/stats.json` (TB-255).
  - `ap2/web_insights.py` — `/insight/<...>` pages.
  - `ap2/web.py` (remains) — FastAPI app construction, middleware, `APIRouter.include_router` for each group, uvicorn task spawning.
- Each sibling module exports `router = APIRouter(prefix="...")` and registers its endpoints on that router.
- Shared HTML-rendering helpers (header, footer, common chrome) either stay in `web.py` or move to a new `ap2/web_chrome.py` if they're sizable.

## Design

- Flat structure only — NO `ap2/web/` subpackage. Each route group becomes a sibling module at `ap2/`.
- FastAPI `APIRouter` composition is the natural primitive — each route group owns a router, `web.py` composes them. Same pattern Starlette / FastAPI ship for exactly this case.
- Page-template rendering stays as inline HTML strings (the current pattern) — no new templating layer (Jinja, etc.). The point is context reduction, not a rendering refactor.
- Auto-refresh JS / stream endpoints (TB-129) stay paired with their owning route group.
- The TB-130 daemon-bundled lifecycle (uvicorn task spawned from `daemon.py`) is unchanged — `web.py` still exposes the `make_app()` / `serve()` entrypoint daemon calls.
- TB-260's env-stale WARN line that surfaces in the web home stays paired with whichever module owns the home page after the split.

## Verification

- `uv run pytest -q` — full project suite passes (web tests included).
- `wc -c ap2/web.py | awk '$1 < 60000 { exit 0 } { exit 1 }'` — `web.py` reduced to under 60KB after the split (just app + middleware + router composition).
- `ls ap2/web_home.py ap2/web_events.py ap2/web_stats.py 2>/dev/null | wc -l | awk '$1 >= 3 { exit 0 } { exit 1 }'` — at minimum three of the suggested sibling modules exist.
- `python3 -c "from ap2.web import make_app; app = make_app(); routes = [r.path for r in app.routes]; assert '/' in routes; assert '/events' in routes; assert '/stats' in routes; print('routes ok:', len(routes))"` — the FastAPI app composes all expected routes (this check requires that make_app() or its equivalent is the canonical entrypoint; if the agent picks a different name, the bullet should adjust).
- prose: each sibling module owns one URL prefix and exports an `APIRouter` that `web.py` mounts via `include_router`. The judge can verify by reading `web.py` and confirming each `include_router` call references an import from a sibling module.
- prose: TB-130's uvicorn-from-daemon lifecycle still works — `daemon.py`'s web-task spawning path calls into `web.py`'s entrypoint, which composes routers from siblings. The judge confirms by reading the daemon's web-startup path and the web-entry function.
- prose: TB-260's env-stale WARN rendering on the web home is preserved end-to-end. The judge can verify by reading the home module and confirming the env-stale check / rendering is present.

## Out of scope

- Subpackage creation — flat-structure principle.
- Switching to a templating engine (Jinja, etc.) — inline HTML stays.
- Changing route URLs / response shapes / JSON schemas — pure refactor; web tests pin existing surfaces.
- Adding new pages / endpoints — separate concern.
- Splitting `tools.py` / `daemon.py` / `cli.py` — separate TBs in this batch.
- Refactoring stream-event protocol (TB-129's seq-based polling) — stays as-is.
- Changing the `AP2_WEB_PORT` / `AP2_WEB_DISABLED` / port-auto-enumerate behavior (TB-130, TB-155) — pure refactor.
## Attempts

### 2026-05-19 — verification_failed
(no summary)
- **kind:** per_task
- **failed_criteria:** [fail] prose: TB-260's env-stale WARN rendering on the web home is preserved end-to-end. The judge can verify by reading the ho
- **Debug dumps:** `prompt: .cc-autopilot/debug/20260519T221428Z-TB-265.prompt.md`, `stream: .cc-autopilot/debug/20260519T221428Z-TB-265.stream.jsonl`, `messages: .cc-autopilot/debug/20260519T221428Z-TB-265.messages.jsonl`

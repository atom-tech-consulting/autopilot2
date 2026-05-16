## Goal

Add an active-focus render line to `ap2/cli.py:cmd_status` text output (and a corresponding `active_focus` block to the JSON output) showing the current focus title + `N of M` position + (when set) the roadmap-complete halt state. Mirror on the web home as a small focus-rotation render above (or inside) the existing automation card. Sources already exist: `goal.parse_foci(...)` returns the list, `goal.active_focus(cfg, foci)` returns the current `FocusItem`, the pointer index lives in `focus_pointer.json`'s `index` field, and the halt state lives in the same file's `roadmap_complete_acked` flag (per TB-226 design and TB-237 e2e fixture).

Current focus: end-to-end automation — axis 4 multi-focus sequential execution (goal.md L115-138) shipped the foundation (TB-226) plus the e2e pin (TB-237) but has zero current-state operator surface. The walk-away promise scales with operator-declared roadmap length (goal.md L131-138: "walk-away time scales with the operator-declared roadmap length"); operator must be able to observe roadmap position on-demand to trust that scaling.

Why now: TB-226 emits `focus_advanced` and `roadmap_complete` event types but those are append-only events, not a current-state surface. Today the operator's only way to answer "what focus am I on, and how many remain?" is `ap2 logs -n N | grep focus_advanced` (last-event reconstruction) or manually reading `focus_pointer.json`. After TB-237 pinned the chain end-to-end, the natural next step is the operator-facing render parity that TB-227 / TB-228 already deliver for axes 1+2+3. Without this, axis 4 stays "shipped but not observable" and the walk-away-scaling claim stays unverifiable to the operator without manual file reading.

## Scope

- `ap2/cli.py:cmd_status` text: print `focus: <active_title> (<idx+1> of <len(foci)>)` line near the top of the report (after the `daemon:` / `version:` lines, before the `board:` counts). When `len(foci) == 1`, just print `focus: <title>` (no position counter — single-focus projects don't need it). When the daemon is in `roadmap_complete` halt state (per `focus_pointer.json`'s `roadmap_complete_acked` flag being False AND pointer at-or-past last focus), print `focus: ROADMAP_COMPLETE — \`ap2 ack roadmap_complete\` to resume` instead, mirroring TB-227's halt-state line shape.
- `ap2/cli.py:cmd_status` JSON: add `active_focus` block = `{"title": ..., "index": ..., "total": ..., "roadmap_complete": bool}`.
- `ap2/web.py` home: add a small focus-rotation rendering above the automation card showing active focus title + position, mirroring the text-render shape.
- Skip rendering entirely (no line in text, `active_focus: null` in JSON, no web element) when `goal.md` is missing or has zero `## Current focus:` headings — preserves the fresh-project no-op path.
- Tests: new `ap2/tests/test_tb242_status_active_focus_surface.py` covers (1) single-focus goal.md → text shows `focus: <title>` (no `(N of M)` suffix); (2) multi-focus goal.md → text shows `focus: <title> (1 of 3)`; (3) roadmap-complete state → text shows the halt-state line with the `ap2 ack roadmap_complete` hint; (4) JSON output includes the `active_focus` block; (5) web home HTML renders the focus title + position.

## Design

Pure read-layer composition over `goal.parse_foci()` + `goal.active_focus()` + `goal.read_focus_pointer()` (or whatever read helper TB-226 exposes). No new state files, no new env knobs, no daemon-side changes. Render-symmetry pattern is the same as TB-227's text/web surface for axes 1+2+3.

## Verification

- `uv run pytest -q ap2/tests/test_tb242_status_active_focus_surface.py` — new test module exists and all five behavioral cases pass.
- `uv run pytest -q ap2/tests/test_cli.py` — existing CLI tests stay green (no regression on `cmd_status`).
- `uv run pytest -q ap2/tests/test_web.py` — existing web tests stay green (no regression on home render).
- `uv run pytest -q ap2/tests/test_tb226_focus_rotation.py` — TB-226 foundation tests stay green.
- `uv run pytest -q ap2/tests/e2e/test_walk_away_loop.py` — TB-237 e2e test stays green (no regression on the upstream pointer-state contract this render depends on).
- `grep -n "active_focus\|focus_pointer" ap2/cli.py` — cli reads the pointer plus active focus.
- `grep -n "active_focus\|focus_pointer" ap2/web.py` — web renders focus state.
- Prose: `cmd_status` in `ap2/cli.py` includes the active-focus text line near the top of the report; the JSON output includes an `active_focus` block with `title` / `index` / `total` / `roadmap_complete` keys.
- Prose: the web home in `ap2/web.py` includes the active-focus title plus position (or the roadmap-complete halt line) rendered above or inside the existing automation card.

## Out of scope

- A standalone `ap2 focus` CLI verb for explicit focus inspection — `ap2 status` is the right cohesive surface.
- Operator-driven focus rotation (`ap2 advance-focus TB-N` or similar) — operator owns `goal.md` per goal.md Non-goals (L187-191); runtime focus advancement remains daemon-only via `_maybe_advance_focus`.
- Backfilling stale `focus_advanced` events when `goal.md` is edited mid-cycle — pointer semantics owned by TB-226; this task is render-only.

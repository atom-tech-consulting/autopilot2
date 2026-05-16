## Goal

Extend `ap2/cli.py:cmd_status` auto-approve render block (L360-385) and `ap2/web.py:_render_automation_card` (L1515-1595) to surface the four dry-run readiness fields TB-238 added to `automation_status.collect_auto_approve_state`: `auto_approve_dry_run_enabled`, `would_auto_approve_count_24h`, `auto_unfreeze_dry_run_enabled`, `would_auto_unfreeze_count_24h`. Render shape mirrors TB-238's status-report digest sub-block — a single `dry-run: would-approve N (24h) | would-unfreeze M (24h)` line printed below the existing `auto-approve: enabled ...` line when either dry-run knob is on, omitted entirely when both off. Web card adds parallel `would-approved (24h)` / `would-unfrozen (24h)` rows linking to `/events?type=would_auto_approve` and `/events?type=would_auto_unfreeze`, plus a `[dry-run]` badge next to the card header when either dry-run knob is on.

Current focus: end-to-end automation — axes 1 + 2 dry-run on-ramps (TB-232 / TB-233) are the trust-building observability layer goal.md L102-113 names as the safety floor. The signal exists in `events.jsonl` + the status-report cron digest after TB-238 but not in the operator's primary on-demand return surfaces.

Why now: TB-238 closed the surface gap for the status-report cron only (per its `## Out of scope`); `ap2 status` and web home are the operator's two on-demand return surfaces between status-report cron firings (status-report tick cadence is typically 2h). Operator who flips `AP2_AUTO_APPROVE_DRY_RUN=1` and runs `ap2 status` to observe the dry-run output gets a byte-identical auto-approve summary to pre-flip — zero evidence the knob changed anything. Without this surface-parity closure, the dry-run on-ramp's primary purpose (operator observes loop decisions during the dry-run window and gains confidence before flipping live) is partially defeated on the on-demand surfaces, forcing operator to wait for the next cron tick or grep events.jsonl manually.

## Scope

- Extend `cli.py:cmd_status` auto-approve block (L360-385): when either `state["auto_approve_dry_run_enabled"]` or `state["auto_unfreeze_dry_run_enabled"]` is True, print a `dry-run: would-approve N (24h) | would-unfreeze M (24h)` line immediately below the existing `auto-approve: ...` line.
- The block-visibility heuristic (`_has_24h_activity` at L367-371) must also count `would_auto_approve_count_24h + would_auto_unfreeze_count_24h` toward the "render-block" decision so fresh dry-run-only state doesn't fall through the existing `auto_approve_enabled == False AND no 24h auto-approve/auto-unfreeze activity` filter.
- JSON output already includes the full `auto_approve_state` dict per TB-238 — no schema change needed; only text-render parity.
- Extend `web.py:_render_automation_card`: add `would-approved (24h)` and `would-unfrozen (24h)` rows linking to `/events?type=would_auto_approve` and `/events?type=would_auto_unfreeze` when the corresponding dry-run knob is on. Add a small `[dry-run]` badge next to the card header when either dry-run knob is on.
- Tests: new `ap2/tests/test_tb241_status_dry_run_surface.py` covers (1) cli text render shows dry-run line when either knob on; (2) cli text render omits dry-run line when both off; (3) cli text render shows the block at all when both knobs off but dry-run 24h activity > 0; (4) web `_render_automation_card` HTML contains `would-approved` row when the auto-approve dry-run knob is on. Use the same fixture shape as TB-238's existing collector + digest tests.

## Design

The collector keys already exist (TB-238 commit `d861d83`). Rendering is a thin layer atop existing read paths. Mirror TB-227's text/web render-symmetry pattern.

## Verification

- `uv run pytest -q ap2/tests/test_tb241_status_dry_run_surface.py` — new test module exists and all four behavioral cases pass.
- `uv run pytest -q ap2/tests/test_cli.py` — existing CLI tests stay green (no regression on the existing auto-approve block render).
- `uv run pytest -q ap2/tests/test_web.py` — existing web tests stay green (no regression on `_render_automation_card`).
- `uv run pytest -q ap2/tests/test_tb238_automation_status_dry_run.py` — TB-238 collector + digest tests stay green (surface-parity neighbor).
- `grep -n "would_auto_approve_count_24h\|would_auto_unfreeze_count_24h" ap2/cli.py` — cli renders both 24h counts.
- `grep -n "would_auto_approve\|would_auto_unfreeze" ap2/web.py` — web card renders both 24h counts.
- Prose: `cmd_status` in `ap2/cli.py` emits a `dry-run:` text line when either dry-run knob is on, in the existing auto-approve block, and the block-visibility heuristic counts dry-run 24h activity toward render.
- Prose: `_render_automation_card` in `ap2/web.py` includes a `would-approved (24h)` row plus a `[dry-run]` badge when `auto_approve_dry_run_enabled` is True; symmetric rendering for the auto-unfreeze dry-run knob.

## Out of scope

- Adding an `ap2 status --watch` or web-card auto-refresh — current snapshot semantics preserved.
- Mutating the JSON output schema for `auto_approve_state` — TB-238 already added the keys; this task is text + web render only.
- Backfilling `would_*` events for the pre-TB-232/TB-233 window — collector reads events.jsonl forward only.

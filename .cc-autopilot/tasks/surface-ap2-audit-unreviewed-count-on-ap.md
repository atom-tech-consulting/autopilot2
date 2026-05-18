## Goal

This task targets the Current focus: end-to-end automation focus, axis 1 (Manual-approval bottleneck) — specifically the walk-away return-surface parity gap on the `ap2 audit` operator surface. The `ap2 audit` CLI verb ships the pull surface for retrospective review of unreviewed Complete + Frozen tasks, with state cleanly derived from `operator_log.md` via two helpers already in HEAD at `ap2/audit.py`: `list_unreviewed(cfg)` returning the unreviewed task list, and `parse_audit_cursor(cfg)` returning the last audit-run timestamp (no new state file). But `ap2/cli.py:cmd_status` carries no audit-count line — `grep -n list_unreviewed ap2/cli.py` returns zero — and the cron status-report (`ap2/status_report.py`) carries no `audit:` digest sub-block. The walk-away operator who returns after a quiet day must KNOW to run `ap2 audit` explicitly to learn how many shipped tasks bypassed their per-task review; the natural-cadence return surfaces stay silent on a count the system already knows.

This is the same push-vs-pull surface-parity shape several prior tasks have closed on different axes: TB-241 closed it for dry-run readiness, TB-242 closed it for axis-4 focus-pointer state, TB-244 closed it for `focus_advanced`/`roadmap_complete` cron digest, and TB-245 closed it for validator-judge fail-open activity. All four helpers consumed here are pure-read functions already available in HEAD; this work composes them onto the existing status + status-report surfaces with no daemon-side changes and no new state.

Why now: walk-away requires a return surface that names the "unreviewed shipped" count without an extra command — without it, the operator's first sighting of an unreviewed-task pile is one manual `ap2 audit` invocation later than the system already knew it was true, which weakens the goal.md L28-30 done-when bullet "walk away for a week without intervention."

## Scope

1. `ap2/cli.py:cmd_status` — call the existing `audit.list_unreviewed(cfg)` helper once and surface a single text line in the existing operator-attention cluster (after `queue:` / `pending review:` / `janitor:` / `classifications:`): `audit: N unreviewed since <cursor-ts>` when `N>0`. Omit the line entirely when `N==0`. Cursor-ts comes from the existing `audit.parse_audit_cursor(cfg)` helper; render as `(epoch)` when None.

2. Same `ap2 status --json` — add a top-level `audit` block: `{"unreviewed_count": N, "cursor_ts": "<ts>|null"}`. ALWAYS present (zero-state included) for parser stability — mirrors the `auto_approve` parser-stability promise.

3. `ap2/automation_status.py` — add a `collect_audit_state(cfg)` helper sibling to `collect_window_validator_judge` returning `{"unreviewed_count": N, "cursor_ts": "<ts>|null"}`. Pure read; reuses the existing `audit.list_unreviewed` helper.

4. `ap2/status_report.py` — add a `render_audit_state_section(state)` renderer that emits a sub-block only when `unreviewed_count > 0` (omit-on-empty). Wire into `run_status_report` `state_extras` alongside the existing axis renderers. Add `audit` to `_STATUS_REPORT_CONTRACT` in `ap2/prompts.py` so the SDK status-report agent verbatim-forwards the rendered sub-block.

5. Cross-reference in `ap2/howto.md` — add a sentence under the `audit` row of the operator-CLI-verbs table noting the new push surface, and under the status-report contract section.

6. Tests at `ap2/tests/test_tb258_audit_count_surface.py`: (a) zero-state omit-line in text mode, (b) `N>0` happy-path text format, (c) JSON shape pin (`audit.unreviewed_count` always present, zero-state included), (d) `collect_audit_state` returns expected shape, (e) `render_audit_state_section` omit-on-empty, (f) `render_audit_state_section` happy-path emits the count + cursor, (g) `_STATUS_REPORT_CONTRACT` contract-string pin.

## Design

Pure read-layer composition over the existing-in-HEAD `ap2/audit.py` helpers. No new state file, no daemon-side changes, no new env knobs. Mirrors the wrap-helper-into-render-+-status-extras pattern used across prior axis-parity tasks. Renderer is omit-on-empty so zero-state projects don't grow a zero-noise line.

## Verification

- `grep -q "list_unreviewed" ap2/cli.py` — `cmd_status` calls the audit helper.
- `grep -q 'audit:' ap2/cli.py` — `cmd_status` references the new `audit:` text line label.
- `grep -q "def collect_audit_state" ap2/automation_status.py` — collector helper exists.
- `grep -q "def render_audit_state_section" ap2/status_report.py` — renderer exists.
- `grep -q '"audit"' ap2/prompts.py` — `_STATUS_REPORT_CONTRACT` enumerates the new `audit` field.
- `uv run pytest -q ap2/tests/test_tb258_audit_count_surface.py` — new pin module passes.
- `uv run pytest -q ap2/tests/test_cli.py ap2/tests/test_tb_status_render.py` — existing status-surface tests stay green.
- `uv run pytest -q ap2/tests/` — full suite green (regression gate).

## Out of scope

- Auto-marking tasks reviewed without explicit `ap2 audit` action.
- Web home surface — a separate parity task can extend this if needed; this one keeps scope to the two natural-cadence return surfaces (CLI status + cron).
- Operator-queue verb additions (the `audit_skip` op already exists).
- Time-window scoping for the digest sub-block — the count is window-independent (cursor-based) so it always reports the full unreviewed pile, not just since-last-report.

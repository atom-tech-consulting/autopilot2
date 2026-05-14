# Surface auto-approve / auto-unfreeze loop state in `ap2 status` (text + JSON) and web home

## Goal

The current focus is `Current focus: end-to-end automation`. TB-223
(axis 1: `AP2_AUTO_APPROVE`), TB-224 (axis 3: token caps +
`task_error` halt), and TB-225 (axis 2: auto-unfreeze) all shipped on
2026-05-14, but none of them exposed operator-facing state. `grep -n
auto_approve ap2/cli.py ap2/web.py` returns empty; `grep -n
auto_unfreeze ap2/cli.py ap2/web.py` returns empty. The walk-away
operator's first-touch surface is `ap2 status` (or the web home page
when they ssh-tunnel); from those surfaces today they cannot tell
whether `AP2_AUTO_APPROVE=1`, whether the loop is paused awaiting
`ap2 ack auto_approve_window_resume`, how many consecutive freezes
have accumulated versus `AP2_AUTO_APPROVE_FREEZE_THRESHOLD`, the
cumulative window-token spend versus
`AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP`, or how many auto-approve /
auto-unfreeze events have fired since the last operator visit.

Why now: without this surface the walk-away promise (goal.md L28-29
"walk away for a week without intervention") stays fictional even
with axes 1-3 shipped — an operator who returns to a halted loop
will see "0A / 0R / 0B" and conclude the daemon idled correctly,
when in reality auto-approve is paused waiting for an ack that
never came because they never saw the pause. This closes the
observability gap between TB-223/TB-224/TB-225's state machinery
and the operator's first-touch surface.

## Scope

(1) Helper in `ap2/automation_status.py` (new module):
`collect_auto_approve_state(cfg: Config, *, now: datetime | None =
None, window_s: int = 86400) -> dict` returns a structured dict:
  - `auto_approve_enabled: bool` (from `AP2_AUTO_APPROVE` env)
  - `auto_approve_paused: bool` (reuses
    `daemon._auto_approve_paused`)
  - `consecutive_freezes: int` (current count, scanned from
    events.jsonl tail since last `auto_approve_unfreeze` token)
  - `freeze_threshold: int` (from
    `AP2_AUTO_APPROVE_FREEZE_THRESHOLD`, default 3)
  - `per_task_token_cap: int | None`
  - `window_token_cap: int | None`
  - `window_tokens_used: int` (cumulative since last
    `auto_approve_window_resume` ack)
  - `auto_approved_count_24h: int`
  - `auto_unfreeze_applied_count_24h: int`
  - `auto_unfreeze_skipped_count_24h: int`
  - `pause_reason: str | None` ("consecutive_freezes" |
    "per_task_token_cap_exceeded" | "window_token_cap_exceeded" |
    "task_error" — derived from the most recent
    `auto_approve_paused` event's notes field, when paused)

(2) `ap2 status` text branch (`ap2/cli.py:cmd_status`):
  - When `auto_approve_enabled` OR any 24h counter is non-zero,
    print an `auto-approve:` block summarizing the state. Healthy
    rendering: `auto-approve: enabled (24h: N approved, M
    auto-unfrozen)`. Paused rendering: `auto-approve: PAUSED
    (reason=<X>; M consecutive freezes / threshold N) — `ap2 ack
    auto_approve_window_resume`` (verb shown so the action is one
    readable nudge away, mirroring TB-151's pending-review line
    shape).
  - When `auto_approve_enabled` is False AND all 24h counters are
    zero, OMIT the line entirely so fresh / pre-opt-in projects
    don't grow a zero-line (mirrors TB-189's classifications line
    pattern).

(3) `ap2 status --json` branch: add `auto_approve` key with the
full helper dict; back-compat for parsers expecting only existing
keys (no key removed, just added).

(4) Web home page (`ap2/web.py` index route): add a `Automation`
card alongside the existing Board / Pending review / Janitor
clusters. Renders the same dict + a 24h sparkline of `auto_approved`
and `auto_unfreeze_applied` event counts bucketed hourly. When
paused, the card border tints red (reuses TB-148's status-color
palette). Link from each card row to `/events?type=auto_approved`
etc. for drill-down.

(5) Tests in new `ap2/tests/test_tb227_automation_status.py`:
  - `collect_auto_approve_state` happy + error paths (knob-off /
    knob-on / paused-on-freezes / paused-on-per-task / paused-on-
    window / paused-on-task_error / 24h counter aggregation /
    window-resume idx scoping).
  - CLI rendering: healthy block, paused block, omitted block.
  - JSON contract: all keys present with correct types regardless
    of enabled/paused state.
  - Web home: card present when knob on or counters non-zero;
    absent when both false; paused border tint pinned via class
    name match.

(6) Coverage drift: add ref counts so `test_coverage_drift.py`
sees the new helper / new module / new env knobs (no new knobs
introduced — the helper reads existing TB-223/TB-224 knobs).

## Design

- New module `ap2/automation_status.py` keeps the aggregation
  pure-function (testable without instantiating the daemon) and
  isolates the events.jsonl-scanning logic from `cli.py` and
  `web.py` (don't bloat either with event-tail-walking).
- 24h counters use the same `events.jsonl` tail-scan primitive
  daemon already uses for `_auto_approve_paused` /
  `_auto_approve_window_resume_idx`. Cheap: bounded tail walk
  scoped to the last 1500 lines (covers >24h of typical event
  volume).
- `pause_reason` derivation: scan the most recent
  `auto_approve_paused` event since the last
  `auto_approve_window_resume` ack idx; the event's notes field
  already carries the trigger (per TB-224's emission shape) — just
  surface it.
- The web Automation card is a new partial template rendered into
  the existing home grid. No JS framework change; one fetch +
  one `<svg>` sparkline (D3-free, hand-rolled `<polyline>`).

## Verification

- `uv run pytest -q ap2/tests/test_tb227_automation_status.py` —
  new test module exists and all behavioral cases pass.
- `uv run pytest -q ap2/tests/` — full suite green vs current
  1421 baseline.
- `test -f ap2/automation_status.py` — new helper module landed.
- `test -f ap2/tests/test_tb227_automation_status.py` — test
  module present.
- `grep -nE "^def collect_auto_approve_state" ap2/automation_status.py` — exported helper symbol.
- `grep -n "auto-approve:" ap2/cli.py` — at least one match where the new status line is emitted.
- `grep -n "auto_approve" ap2/web.py` — at least one match where the new automation card is rendered.
- `grep -nE "auto_approve" ap2/cli.py | wc -l` — at least 3 matches (helper call + status line + JSON key).
- Prose: `cmd_status` calls `collect_auto_approve_state` and surfaces the dict in both text and `--json` branches; judge confirms via Read of `ap2/cli.py`.
- Prose: when `auto_approve_enabled` is False and all 24h counters are zero, the `auto-approve:` text line is omitted; judge confirms by reading the test that pins the omit-line path.

## Out of scope

- Mattermost status-report digest of the same state — that's TB-228's
  scope; the cron post is a different surface (scheduled rather than
  synchronous-pull). Keep them separate so each can land cleanly.
- New env knobs. This task is pure surface — it reads existing
  TB-223 / TB-224 knobs.
- Historical replay / `ap2 audit auto-approve --window N` simulator.
  Deferred for after enough auto-approved events accumulate for
  retrospective analysis to be meaningful.

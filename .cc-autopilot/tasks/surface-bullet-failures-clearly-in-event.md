# Surface bullet failures clearly in events logs (CLI + web)

## Goal

Make `verification_failed` events readable at a glance — both `ap2 logs` (CLI) and the `/events` + `/task-run/<run-id>` web views. Today the failure structure (per-bullet status, kind, notes) is captured in the event payload but truncated and reflowed in a way that hides the "which bullet failed and why" question behind clicks and squinting. Sessions of "which bullet broke this time?" eat operator attention every time a task fails verification.

What "clearly" means: at a glance the operator sees N/M bullets passed, the failed bullet text, and the judge's note — without scrolling, without expanding `<details>`, without parsing JSON in their head.

## Scope

Files to touch:

- `ap2/cli.py` — `cmd_logs` (or its rendering helper) gets verification-failure-aware formatting: when a row is `type=verification_failed` (or `type=task_complete` with `status=verification_failed`), pretty-print the per-bullet pass/fail/unverified counts and the failing-bullet headlines + truncated notes. Keep the raw JSON path available behind a `--json` / `-v` flag.
- `ap2/web.py` — events table renders `verification_failed` rows with a per-row pass/fail counter and a one-click expansion that shows the failing bullets first (sorted), passing ones folded. The per-task-run detail page (TB-129) already has stream rows; add a top-of-page verification-summary block that renders the LATEST verification_failed event for that task with the failing bullets called out.
- `ap2/events.py` (or wherever event-rendering helpers live) — small `summarize_verification_failed(event_dict) -> str` helper shared between cli and web so the formatting stays in one place.
- Tests in `ap2/tests/test_cli.py` (CLI rendering pin) and `ap2/tests/test_web.py` (web rendering pin).

## Design

### Today's pain (concrete)

Looking at a real `verification_failed` event from `events.jsonl`:

```
{"type": "verification_failed", "task": "TB-142", "criteria": [
  {"kind":"shell","status":"pass","bullet":"`uv run pytest -q ap2/tests/` ..."},
  ...4 more pass...
  {"kind":"prose","status":"fail","bullet":"Manual: kick a long-running task on stoch...","notes":"Manual verification bullet requires a live..."},
  ...
]}
```

In `ap2 logs`, today's renderer prints: `2026-04-30T22:00:49Z verification_failed TB-142 fails=1` — counter only, the operator has to open `events.jsonl` in an editor to find what failed and why. The web `/events` page shows the same row collapsed. The detail page (TB-129) shows the stream but not the verification verdict directly.

### Proposed CLI shape

For `ap2 logs --tail 5` showing a verification_failed:

```
2026-04-30T22:00:49Z  verification_failed  TB-142  5/6 passed, 1 failed, 0 unverified
  ✗ [prose]  Manual: kick a long-running task on stoch, mention `@claude-bot status`...
            ↳ Manual verification bullet requires a live stoch deployment test
              — no evidence such a manual run was performed and recorded; ...
```

Three lines per failed bullet (header + note continuation); passing bullets summarized into the counter only. Failing bullet text trimmed to ~120 chars, note trimmed to ~200; full text behind `ap2 logs --json`.

If multiple bullets failed, list all in order. Pass/unverified count visible but bullets hidden — the noise/signal ratio is in the operator's favor.

### Proposed web shape

`/events` page row for a verification_failed: today renders as a single row with `failed_criteria=N`. Replace with a row that shows:

```
[verification_failed]  TB-142  5/6 passed · 1 failed
                       └─ ✗ Manual: kick a long-running task on stoch...
```

Row class is the existing failure-tinted (red/orange) one; the failed bullet headlines render inline as a sub-list. Click row → existing details/json view still available.

`/task-run/<run-id>` detail page: adds a top block when the task's most recent terminal event is `verification_failed`:

```
Verification: 5/6 passed, 1 failed
✗ Manual: kick a long-running task on stoch...
   note: Manual verification bullet requires a live stoch deployment test...
```

Sits above the stream rows so operators arriving from a `task_complete` link immediately see WHY without scrolling through tool calls.

### Shared formatter

`events.summarize_verification_failed(event)` returns `{summary_line: str, failed_bullets: list[{bullet, notes, kind}], pass_count: int, unverified_count: int}`. Both CLI and web consume this; render-time formatting (truncation lengths, ANSI vs HTML, sort order) is per-surface.

Sort order: failed > unverified > pass. Within failed, source order preserved (so the operator sees them in the same order they appear in the briefing).

Truncation defaults:
- Bullet text: 120 chars in CLI, 240 chars in web.
- Note: 200 chars in CLI, 400 chars in web; both with a `...` and a "see details" pointer.

### Apply same treatment to neighboring failure events

The pattern generalizes — `task_state_violation` (lists `fenced_files`), `task_timeout` (carries `last_messages`), `task_error` (carries `error` + `stderr_tail`) all benefit from the same headline-with-detail rendering. Out of scope for THIS task to keep diff small; the shared formatter scaffolding leaves room to extend.

## Verification

- `uv run pytest -q ap2/tests/` — full regression gate passes (gating)
- `grep -qE "summarize_verification_failed" ap2/events.py ap2/cli.py ap2/web.py` — shared helper exists and both surfaces consume it.
- New unit test in `test_cli.py`: `cmd_logs` (or its rendering helper) with a stubbed `events.jsonl` containing one `verification_failed` event with 5 pass + 2 fail + 1 unverified produces output containing `5/2 failed, 1 unverified` (or equivalent counter), the two failing bullet headlines (truncated), and the judge's notes (truncated). Passes are NOT individually rendered.
- New unit test in `test_cli.py`: `cmd_logs --json` flag bypasses the pretty formatter and prints the raw event JSON unchanged (regression — operators / scripts depending on raw output keep working).
- New unit test in `test_web.py`: rendering `/events` with a fixture containing a `verification_failed` event produces HTML carrying the pass/fail counter and the failing-bullet sub-list inline; passing bullets are NOT in the rendered HTML.
- New unit test in `test_web.py`: rendering `/task-run/<run-id>` for a task whose most recent terminal event is `verification_failed` produces HTML containing a verification-summary block at the top; for a task whose terminal event is `task_complete` with `status=complete`, no summary block appears.
- New unit test in `test_events.py` (or wherever the helper lives): `summarize_verification_failed(event)` returns the expected dict shape; sort order is `failed > unverified > pass` within the result; truncation respects the configured max lengths; legacy events missing the `criteria` field return a sensible fallback (`pass_count=0, failed_bullets=[]`).
- The diff includes screenshots / sample output in the commit message or the briefing's `## Attempts` section showing before/after for a real-looking event.

## Out of scope

- Generalizing the headline-with-detail rendering to `task_state_violation`, `task_timeout`, `task_error` events. Same pattern applies; file follow-up if this task's shared formatter pays off.
- Adding a "verification history" view per task (every prior verdict, not just the most recent). The TB-129 detail page already lists prior runs via the Runs section; that's enough today.
- Changing the verifier's criteria-emission shape (e.g. adding a `severity` field). This task is purely a presentation change against today's event payload.
- Rendering pass/unverified bullets in CLI by default — keep them collapsed-as-counter to avoid noise. Operators wanting full audit can use `ap2 logs --json` or read events.jsonl directly.
- Aggregating failures across runs ("TB-X failed bullet `Y` 3 times in a row"). Useful but separate scope.

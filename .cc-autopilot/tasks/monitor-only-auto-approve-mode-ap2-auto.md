# TB-232 — Monitor-only auto-approve mode: `AP2_AUTO_APPROVE_DRY_RUN=1` emits `would_auto_approve` events without stripping the `@blocked:review` codespan

## Goal

Current focus: end-to-end automation — goal.md L82-88 (axis 1)
specifies the auto-approval mode as the most concrete deliverable
that unlocks walk-away time, and goal.md L184-186 names the
"opt-in env knobs with conservative defaults" pattern explicitly.
Today `AP2_AUTO_APPROVE` is a binary cliff: either off (operator
approves every task, walk-away undelivered) or on (the daemon
strips `@blocked:review` from minute one, with all gating logic
active for the first time, with no prior observation of how the
gating would have decided). The events.jsonl recent-tail shows
zero `auto_approved` events since TB-223 shipped on 2026-05-14,
confirming the on-ramp friction is real — the feature is in HEAD
but undeployed. Add a `AP2_AUTO_APPROVE_DRY_RUN=1` mode that runs
the full auto-approve gate chain (tags + freeze-threshold +
per-task token cap + window token cap) but does NOT strip
`@blocked:review` — instead emits `would_auto_approve` events
with the same payload shape as `auto_approved`. Operator runs
the daemon with `AP2_AUTO_APPROVE=1` + `AP2_AUTO_APPROVE_DRY_
RUN=1` for a day, reads the events to confirm the gating logic
matches their judgment, then unsets the dry-run knob to engage
real dispatch.

Why now: TB-223 shipped the knob 2026-05-14T22:11Z and TB-227
shipped the operator-facing status surface 2026-05-15T18:14:21Z,
but events.jsonl has zero `auto_approved` events — the feature
has zero deployment volume. Without an on-ramp surface, the
operator's cost of trying the feature is "trust the daemon's
gating from minute one" which the walk-away promise can't
afford. Shipping dry-run now lets the operator start observing
the gate's decisions immediately on the next batch of approved
ideation proposals.

## Scope

(1) Add `AP2_AUTO_APPROVE_DRY_RUN` env-knob parsing helper next
to the existing auto-approve knob parsing (e.g.
`_is_auto_approve_dry_run()` in `ap2/automation_status.py`,
mirroring the `_is_truthy` shape used at L329 for
`AP2_AUTO_APPROVE`).

(2) Modify the auto-approve dispatch path in `ap2/daemon.py`
(around the `_was_auto_approved` / dispatch gate at L3769-3821):
when the gate would otherwise fire (all checks pass and the
review token would be stripped), branch on dry-run:
  - Dry-run OFF (default): existing behavior — strip the review
    token, emit `auto_approved` event, the task auto-promotes
    on the next tick.
  - Dry-run ON: do NOT strip the review token; emit
    `would_auto_approve` event with the same `{task, knob,
    ...}` payload PLUS a `dry_run: true` field; the task stays
    `@blocked:review` for operator-manual approval.

(3) Extend `ap2/automation_status.collect_auto_approve_state`
(the 11-key dict from TB-227) with two new keys:
  - `dry_run_enabled` (bool) — `AP2_AUTO_APPROVE_DRY_RUN` truthy.
  - `would_auto_approve_count_24h` (int) — count of
    `would_auto_approve` events in the 24h window (parallel to
    the existing `auto_approved_count_24h`).
  Surface these in `ap2 status` text + JSON + web home (the
  TB-227 surface code reads `collect_auto_approve_state`'s dict
  directly, so the additions surface mechanically).

(4) Document the dry-run knob in `ap2/howto.md` immediately
after the `AP2_AUTO_APPROVE` master-switch entry (currently at
howto.md L747-758). Add a one-paragraph "Enablement on-ramp"
note describing the recommended sequence: enable dry-run
first, observe `would_auto_approve` events for ≥24h, unset
dry-run.

(5) Tests in new `ap2/tests/test_tb232_auto_approve_dry_run.py`:
  - `test_would_auto_approve_event_fires_when_dry_run_set`:
    seeds a Backlog task with `@blocked:review`, env
    `AP2_AUTO_APPROVE=1` + `AP2_AUTO_APPROVE_DRY_RUN=1`; runs
    the dispatch gate; asserts (a) `would_auto_approve` event
    fires with `dry_run=true`, (b) no `auto_approved` event
    fires, (c) the task line still contains
    `` `@blocked:review` `` codespan.
  - `test_blocked_review_codespan_preserved_in_dry_run_mode`:
    pin (c) above explicitly via a board re-parse.
  - `test_real_auto_approve_unaffected_when_dry_run_unset`:
    seeds same task with `AP2_AUTO_APPROVE=1` only (dry-run
    unset); asserts `auto_approved` event fires + review token
    stripped — pins the no-regression guarantee.
  - `test_dry_run_flag_in_collect_auto_approve_state`: with
    dry-run set, asserts `collect_auto_approve_state(...)
    ["dry_run_enabled"] is True`.
  - `test_would_auto_approve_counter_in_collect_state`: seeds
    2 `would_auto_approve` events; asserts the 24h counter
    returns 2.

## Design

Dry-run check sits at the END of the auto-approve gate chain
(after tags / freeze-threshold / token-caps all otherwise
pass), so it does NOT bypass any of the existing safety
semantics — it only changes the WRITE step from "strip review
+ emit `auto_approved`" to "emit `would_auto_approve`". This
keeps the gating surface single-source-of-truth (operator can
toggle dry-run on and off and see identical decisions, just
different actions on those decisions).

Event-type naming: `would_auto_approve` mirrors the existing
`auto_approved` shape for symmetry. The `dry_run: true` payload
field disambiguates if anyone parses both event streams
together (e.g. the 24h counter aggregator).

No new auto-unfreeze dry-run (`would_auto_unfreeze`) in this
task — out of scope below.

## Verification

- `uv run pytest -q ap2/tests/test_tb232_auto_approve_dry_run.py` — new test module exists and all five behavioral cases pass.
- `uv run pytest -q ap2/tests/test_tb223_auto_approve.py` — existing auto-approve tests stay green (no regressions on the non-dry-run path).
- `uv run pytest -q ap2/tests/test_tb227_automation_status.py` — TB-227 collector tests stay green with the two new keys added.
- `uv run pytest -q ap2/tests/` — full suite green vs current baseline.
- `test -f ap2/tests/test_tb232_auto_approve_dry_run.py` — test module present on disk.
- `grep -nE "AP2_AUTO_APPROVE_DRY_RUN" ap2/daemon.py ap2/automation_status.py` — env knob wired into both surfaces.
- `grep -nE "would_auto_approve" ap2/daemon.py` — dry-run event emission present in daemon dispatch path.
- `grep -nE "dry_run_enabled|would_auto_approve_count_24h" ap2/automation_status.py` — new collector keys present.
- `grep -nE "AP2_AUTO_APPROVE_DRY_RUN" ap2/howto.md` — operator-facing docs updated.
- Prose: `ap2/daemon.py` Prose: the dry-run branch sits AFTER the existing tags / freeze-threshold / per-task-token-cap / window-token-cap gate checks (i.e. it never bypasses an existing safety check, it only changes the write action when all checks pass); judge confirms by reading the gate-site branch order in the auto-approve dispatch path.

## Out of scope

- Auto-unfreeze dry-run (`would_auto_unfreeze` event) — same
  shape but the auto-unfreeze path's blast radius is smaller
  (single Frozen-task patch vs. unbounded dispatch volume), so
  dry-run there is lower priority. Defer to a sibling task if
  operator wants it after auto-approve dry-run is live.
- Web `/dry-run` dedicated dashboard page — events.jsonl +
  `ap2 status` + the existing web home surface cover the
  operator's immediate need; a dedicated page is feature creep.
- Replaying dry-run events to retroactively dispatch tasks
  (i.e. "after a day of monitoring, apply all the
  `would_auto_approve` decisions I observed") — adds a new
  state-mutation surface beyond the env-knob flip; out of scope.
- Documentation drift gate for the new env knob — TB-203's
  docs-drift coverage should pick up the knob automatically;
  if it doesn't, file a follow-up rather than expanding scope.
## Attempts

### 2026-05-16 — verification_failed
(no summary)
- **kind:** per_task
- **failed_criteria:** [fail] Prose: `ap2/daemon.py` Prose: the dry-run branch sits AFTER the existing tags / freeze-threshold / per-task-token-cap / 
- **Debug dumps:** `prompt: .cc-autopilot/debug/20260516T011356Z-TB-232.prompt.md`, `stream: .cc-autopilot/debug/20260516T011356Z-TB-232.stream.jsonl`, `messages: .cc-autopilot/debug/20260516T011356Z-TB-232.messages.jsonl`

# TB-238 — Extend `automation_status` collector + status-report digest with dry-run readiness signal (`would_auto_approve` / `would_auto_unfreeze` 24h counts + auto-unfreeze dry-run badge)

## Goal

Current focus: end-to-end automation — TB-232 (`bfa368a`) +
TB-233 (`74bd793`) shipped the dry-run on-ramps for axes 1+2,
but the operator's primary return surface doesn't show the
dry-run window's verdict. Today
`automation_status.collect_auto_approve_state` exposes
`dry_run_enabled` + `would_auto_approve_count_24h` (TB-232,
auto-approve side only). The auto-unfreeze side has no parallel
fields — verified 2026-05-16 grep on
`ap2/automation_status.py`: zero matches for
`would_auto_unfreeze` / `auto_unfreeze_dry_run`. And the
status-report digest (`ap2/status_report.py:137`
`render_automation_loop_activity_section`, TB-228) shipped
BEFORE TB-232/233 and has zero `would_auto_approve` /
`would_auto_unfreeze` references either.

Result: operator flips `AP2_AUTO_APPROVE_DRY_RUN=1` or
`AP2_AUTO_UNFREEZE_DRY_RUN=1` expecting to observe the loop's
decisions for a window, then return to the Mattermost status
post for a readiness verdict — and finds only auto-approve
side fields, with the digest section saying nothing about
either dry-run. Goal.md L142-145 frames the on-ramps as
mutually reinforcing ("once an operator-trusted auto-approve
loop exists, the failure-recovery automation has a clear
deployment target") but that depends on the dry-run signal
being VISIBLE in the operator's return view.

Why now: TB-232 + TB-233 landed in concert at
2026-05-16T01:32:05Z and 2026-05-16T01:49:51Z (the same
ideation cycle's approvals just drained). Without this digest
extension, the dry-run signal accumulates in `events.jsonl`
but never reaches the operator's daily return surface — the
shipped on-ramps' promotion path stays operator-manual-recall
("did I remember to grep the logs?") rather than
operator-cued-by-cron ("status report says window has N
decisions, ready to flip").

## Scope

(1) Extend `ap2/automation_status.py`
`collect_auto_approve_state` — add auto-unfreeze sibling
fields parallel to the existing auto-approve dry-run fields:
  - `auto_unfreeze_dry_run_enabled` (bool) — reads
    `_is_auto_unfreeze_dry_run` (add helper mirroring
    `_is_auto_approve_dry_run` at line 86; reads
    `AP2_AUTO_UNFREEZE_DRY_RUN` env).
  - `would_auto_unfreeze_count_24h` (int) — rolling 24h count
    of `would_auto_unfreeze` events via the existing
    `_count_events_24h(tail, event_type="would_auto_unfreeze")`
    helper.
Place the new keys directly after the existing dry-run keys
in the return dict so the JSON ordering reflects axis-pairing
(auto-approve dry-run → auto-unfreeze dry-run).

(2) Extend `ap2/status_report.py`
`render_automation_loop_activity_section` to render a
"dry-run window" sub-block when EITHER
`dry_run_enabled` OR `auto_unfreeze_dry_run_enabled` is True
in the collector output. Sub-block format (Mattermost
markdown):

    *Dry-run window:*
    - auto-approve: `<N>` `would_auto_approve` in 24h
    - auto-unfreeze: `<M>` `would_auto_unfreeze` in 24h

Render the sub-block only for the axes whose dry-run is on
(skip the auto-unfreeze line if `auto_unfreeze_dry_run_enabled`
is False, etc.); omit the entire sub-block when both are
False (preserves existing TB-228 output for the default-off
case). Place the sub-block at the end of the existing digest
section so default-off output is byte-identical to today's.

(3) Tests:
  - Extend `ap2/tests/test_tb227_automation_status.py` with
    two new tests pinning `auto_unfreeze_dry_run_enabled` +
    `would_auto_unfreeze_count_24h` behavior across
    knob-on/knob-off + tail with/without
    `would_auto_unfreeze` events. Match the shape of the
    existing TB-232 dry-run-key tests (same fixture style).
  - Extend `ap2/tests/test_tb228_status_report_automation_
    digest.py` with two new tests: one pins the dry-run
    sub-block renders when either knob is on with the
    expected count; one pins the sub-block is OMITTED when
    both dry-runs are off (byte-identical-output regression
    pin against TB-228's existing default-off output).

(4) Update `ap2/howto.md` near the existing dry-run knob docs
(L967-997 for auto-unfreeze) to mention the new digest
sub-block as the operator's readiness-signal surface.

## Design

Collector extension follows TB-227/TB-232 precedent: new keys
on `collect_auto_approve_state`'s return dict, parallel to
the existing auto-approve fields, sourced from the existing
`_count_events_24h` helper. No new event types, no new env
knobs — just surfacing what TB-232/233 already emit.

Digest extension uses the existing
`render_automation_loop_activity_section` signature unchanged
— the new sub-block reads from the collector output that's
already passed in. Render-only change; no new collector
invocation needed.

Default-off invariant: when both `AP2_AUTO_APPROVE_DRY_RUN`
and `AP2_AUTO_UNFREEZE_DRY_RUN` are unset, the digest output
must be byte-identical to TB-228's current output. This is
the regression-pin that prevents the new code path from
leaking into the default operator experience. The test for
this (in step 3) is the load-bearing safety check.

Naming: keep the collector field name asymmetric with the
auto-approve side (`dry_run_enabled` for auto-approve already
shipped without an `auto_approve_` prefix; the new
`auto_unfreeze_dry_run_enabled` carries the prefix to
disambiguate). Document the asymmetry in the new field's
docstring referencing the TB-232 precedent.

## Verification

- `uv run pytest -q ap2/tests/test_tb227_automation_status.py` — collector tests pass with new auto-unfreeze dry-run keys.
- `uv run pytest -q ap2/tests/test_tb228_status_report_automation_digest.py` — digest tests pass with new dry-run sub-block + default-off byte-identical pin.
- `uv run pytest -q ap2/tests/` — full suite green vs current baseline.
- `grep -nE "auto_unfreeze_dry_run_enabled" ap2/automation_status.py` — new collector key declared in collector source.
- `grep -nE "would_auto_unfreeze_count_24h" ap2/automation_status.py` — new collector key declared in collector source.
- `grep -nE "would_auto_unfreeze|would_auto_approve" ap2/status_report.py` — digest renderer references both event types.
- `grep -nE "auto_unfreeze_dry_run_enabled|would_auto_unfreeze_count_24h" ap2/tests/test_tb227_automation_status.py` — collector tests pin both new keys.
- `grep -nE "dry-run window" ap2/tests/test_tb228_status_report_automation_digest.py` — digest tests pin the new sub-block header.
- `grep -nE "AP2_AUTO_UNFREEZE_DRY_RUN" ap2/howto.md` — howto.md still references the knob (regression-pin for the existing doc).
- Prose: `ap2/automation_status.py` Prose: the new `auto_unfreeze_dry_run_enabled` + `would_auto_unfreeze_count_24h` keys appear in `collect_auto_approve_state`'s return dict directly after the existing `dry_run_enabled` + `would_auto_approve_count_24h` keys (preserving axis-pairing); judge confirms by reading the function body in HEAD.
- Prose: `ap2/status_report.py` Prose: `render_automation_loop_activity_section` emits the dry-run sub-block ONLY when at least one of `dry_run_enabled` / `auto_unfreeze_dry_run_enabled` is True in its input (default-off byte-identical to TB-228); judge confirms by reading the rendering branch.

## Out of scope

- New env knobs — TB-232/233 already shipped the dry-run
  enable knobs; this task is observability-only.
- Promotion-criteria automation ("after N decisions
  auto-promote to real mode") — operator-only decision per
  goal.md L184-186; this task only surfaces the count,
  doesn't recommend a flip.
- Web-UI surface for the same fields — defer to a separate
  cycle; status-report cron is the operator's primary
  return channel.
- Backfilling `would_*` event counts beyond the 24h rolling
  window — matches existing TB-227 / TB-232 window
  convention.

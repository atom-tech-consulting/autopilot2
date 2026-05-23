## Goal

Stop the periodic status-report cron from posting back-to-back
near-identical content. Closes the second failure mode goal.md
names under `Current focus: operator-legible reporting and monitoring`
("Clock-driven repetition: the cron fires on a fixed interval and
re-states unchanged content; the existing 'skip if no activity' gate
is too coarse — suppresses only the fully-idle case, not
near-duplicate repetition") and directly satisfies the Done-when
bullet "Reports are significance-gated and delta-based: no two
consecutive reports repeat unchanged content; a report fires on
report-worthy change, not purely on the clock."

Why now: today's `_status_report_should_skip` (`ap2/status_report.py`
L927) suppresses only fully-idle windows where zero interesting
events landed; a window with a single new `task_complete` followed
by another single-event window posts two near-identical bodies in a
row. `ap2/cron.py` L196-210 is purely interval-driven — no
content-level significance gate exists, no `cron_skipped
reason=duplicate_content` event type registered. Three consecutive
low-delta posts train the operator to ignore the channel, defeating
the monitoring half of the walk-away promise goal.md L201-202 names
explicitly.

## Scope

(1) Add `compute_status_report_fingerprint(cfg, *, board, snapshot)
-> str` to `ap2/status_report.py` — returns a stable hash (SHA-1
hex, truncated to ~12 chars) of the structural inputs that drive
the rendered post: per-section board counts; sorted tuple of
pending-review TB-Ns; sorted tuple of decisions-needed bullet
texts; presence + content fingerprints of each digest sub-section
(`render_automation_loop_activity_section`,
`render_focus_rotation_activity_section`, the validator-judge
sub-block, the audit sub-block, the stats-window sub-block); the
most-recent halt reason if any. Explicitly excludes the headline
timestamp so two windows with identical structural state produce
the same hash.

(2) Persist `last_post_fingerprint: str` in `cron_state.json`
under the `status-report` job key. Extend `mark_run` in
`ap2/cron.py` (or add sibling `mark_run_with_payload`) so the
cron-complete code path stores the fingerprint of the
just-rendered post atomically with the timestamp under
`locked_sidecar`.

(3) Extend `_status_report_should_skip(cfg)` to compute the
prospective fingerprint pre-flight and return True when it
matches `last_post_fingerprint`. The existing fully-idle gate
remains as the cheap first check; fingerprint comparison runs
only when the idle gate would let the post through. When the
fingerprint matches, the caller emits a new `cron_skipped`
event with `reason=duplicate_content` so the operator can
audit suppressions via `ap2 logs` and the `/events` web view.

(4) Register `cron_skipped reason=duplicate_content` in
`ap2/events.py` (extend the documented `cron_skipped` reason
set; mirrors the TB-244 / TB-275 pattern of growing the
reason vocabulary alongside the gate). Cross-reference in
`ap2/howto.md`'s status-report section.

(5) Regression-pin module `ap2/tests/test_tb281_status_report_dedup.py`
covers: fingerprint stability across two equivalent snapshots
(same hash); fingerprint sensitivity to each input axis
(board counts change → different hash; new decisions-needed
bullet → different hash; new halt reason → different hash;
new digest sub-section appears → different hash); skip-gate
returns True + emits the `duplicate_content` event when the
prospective fingerprint matches the stored one; skip-gate
returns False on the first run (no stored fingerprint).

## Design

Fingerprint shape parallels how TB-228 + TB-244 already factor
the digest into discrete sub-section renderers — re-using their
outputs (not their inputs) makes the fingerprint sensitive to
exactly what the operator would see in the rendered post. The
two-tier skip-gate (idle gate cheap-first, fingerprint gate
second) preserves the current zero-cost fast path for fully-idle
windows and adds work only when a post would otherwise fire.
Persisting in `cron_state.json` (already lock-protected, already
on disk, already drained at restart) avoids any new state-file
fence concerns.

## Verification

- `grep -Eq "compute_status_report_fingerprint|last_post_fingerprint" ap2/status_report.py` — fingerprint helper + state-field wired.
- `grep -q "duplicate_content" ap2/status_report.py` — skip-reason rendered at the call site.
- `grep -q "duplicate_content" ap2/events.py` — reason registered in the event vocabulary.
- `test -f ap2/tests/test_tb281_status_report_dedup.py` — regression-pin module exists.
- `uv run pytest -q ap2/tests/test_tb281_status_report_dedup.py` — module passes.
- `uv run pytest -q` — full suite passes.

## Out of scope

- Adjusting the cron interval itself (the dedup gate handles
  the repetition case; rate-limiting the cron is a separate axis).
- Redesigning the digest sub-block render shapes (this task
  hashes the existing renderers' outputs; sub-block redesign
  belongs to the per-section tasks already shipped or planned).
- Web UI rendering of skip events (events.jsonl already carries
  them for `/events` view; no new UI work).

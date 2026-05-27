# Fix time-bombed render_attention_section test via now= injection seam

Tags: #autopilot #attention #status-report #testing #regression-pin #bug

## Goal

Thread an optional `now: datetime | None = None` kwarg through
`ap2/status_report.py:render_attention_section` to its
`ap2/attention.py:detect_attention_conditions` call, then update the
broken time-bombed test at
`ap2/tests/test_tb288_attention_validator_judge_noisy.py:415`
(`test_render_attention_section_includes_validator_judge_noisy`)
to pass its hardcoded reference time. Closes the goal.md `## Done when`
failure mode "Ideation reliably proposes goal-aligned next steps that
substantively advance the goal (not just goal-shaped pro-forma
compliance)" — the full pytest test suite currently breaks because
the failing test seeds events relative to a hardcoded 2026-05-26 12:00
UTC reference and calls the renderer without a `now` arg; the renderer
defaults to actual wall-clock time, the 24h window now excludes the
seeded events, the rendered string is empty, and the assertion fails.
This blocks TB-300 verification (correctly committed at 6b0f268) and
every future TB-N that uses `uv run pytest -q ap2/tests/` as the
regression gate.

Why now: 2026-05-27 — TB-300 hit `retry_exhausted` at 13:27:40Z because
the full pytest blocks on a single unrelated time-bombed test. Fix is
small (one optional kwarg + one test update); without it, NO future
verification can pass.

## Scope

(1) `ap2/status_report.py:render_attention_section`: add optional
keyword-only parameter `now: _dt.datetime | None = None`. Thread to
`_attention.detect_attention_conditions(cfg, tail=tail, now=now)`.
Production call sites that don't pass `now` remain unchanged.

(2) `ap2/tests/test_tb288_attention_validator_judge_noisy.py`:
update the broken test at L415 to pass its existing local `now`
to the renderer: `rendered = render_attention_section(cfg, since_event_idx=0, now=now)`.

(3) Audit sibling test modules
(`ap2/tests/test_tb282_attention_stuck_task.py`,
`ap2/tests/test_tb287_attention_task_frozen.py`,
`ap2/tests/test_tb289_attention_auto_approve_paused.py`,
`ap2/tests/test_tb290_attention_cost_cap_approach.py`) for the
same shape (calling `render_attention_section` without `now=`).
Apply the same fix to any that have it.

(4) The renderer docstring: add a line documenting the new
parameter (defaults to actual UTC; tests inject a deterministic
reference to avoid wall-clock-drift flakiness).

(5) New regression-pin module
`ap2/tests/test_render_attention_section_now_injection.py`:
- `render_attention_section(cfg, since_event_idx=0)` (no `now`) still
  works against same-day-seeded events (production path).
- `render_attention_section(cfg, since_event_idx=0, now=fixed_now)`
  uses the injected reference for the detector's window — pin a
  deterministic test that seeds events relative to a hardcoded `now`
  from 2025 and asserts rendered output is non-empty regardless of
  when the test is run.

## Design

Single-parameter injection seam, default-None preserves existing
production behavior. Production callers leave `now` unset; the renderer
uses actual UTC time. Tests pass a deterministic reference to avoid
wall-clock drift breaking them on a calendar day after the test was
written. The companion test in the same module already threads
`now=now` end-to-end through `detect_attention_conditions` directly;
this TB brings the renderer's API to parity so the end-to-end renderer
test uses the same pattern. Symmetric seam, no new abstraction.

Why not freezegun? Single-file dependency change for a one-test fix
is heavier than an optional parameter. The detector itself already
accepts `now=`; extending the same pattern one layer up is consistent.

Why not also thread `now` from production callers when they have one?
The cron status-report path doesn't carry a `now` reference; adding
that plumbing widens scope. The existing default-None behavior is
correct for production.

## Verification

- `grep -q 'now: _dt.datetime' ap2/status_report.py` — parameter added.
- `uv run python -c "import inspect, ap2.status_report; sig = inspect.signature(ap2.status_report.render_attention_section); assert 'now' in sig.parameters"` — signature has the kwarg.
- `uv run pytest -q ap2/tests/test_tb288_attention_validator_judge_noisy.py` — the previously-failing test passes.
- `test -f ap2/tests/test_render_attention_section_now_injection.py` — regression-pin module exists.
- `uv run pytest -q ap2/tests/test_render_attention_section_now_injection.py` — new pin tests pass.
- `uv run pytest -q ap2/tests/` — full suite passes (closes the verification gate).

## Out of scope

- Refactoring `detect_attention_conditions` itself.
- Adding `now=` injection to other renderer functions
  (`render_recent_task_activity_section`, etc.) — separate hardening
  pass if a similar time-bomb surfaces.
- Switching the test suite to freezegun.
- Unfreezing TB-300 as part of this TB — needs its own
  `ap2 unfreeze TB-300` invocation after this lands.
- Production callers explicitly passing `now=` — default-None
  fall-through to actual UTC is correct for production.

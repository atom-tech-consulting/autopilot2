# TB-245: Extend status-report cron digest with validator-judge fail-open activity

## Goal

Current focus: end-to-end automation. TB-243 (`647b771`) closed the
pull-surface half of validator-judge fail-open observability — `ap2 status`
text/JSON + the web home automation card render 24h counts of
`validator_judge_fail` + `validator_judge_timeout` with a `[noisy]` badge
when totals cross `AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD`. The push-surface
half (status-report cron digest, the operator's 2h Mattermost channel for
walk-away monitoring per TB-228 / TB-238 / TB-244) is still uncovered:
`grep validator_judge ap2/status_report.py` returns zero matches AND
`_STATUS_REPORT_AUTOMATION_INTERESTING_TYPES` (status_report.py:548-557)
lacks both event types, so a fresh validator-judge fail-open event won't
even un-skip the report's no-op gate via `_status_report_should_skip`.

Result: when the TB-235 dependency-coherence judge silently degrades
during a walk-away weekend (SDK timeouts, malformed JSON, transient
auth errors), the operator's primary push channel reports nothing —
only their next manual `ap2 status` carries the signal. This directly
weakens the goal.md L82-85 auto-approve safety claim ("upstream gates
already make this safe in practice"): the dep-coherence judge IS one
of those upstream gates, and a fail-open gate without push-channel
observability is functionally invisible during the walk-away window
goal.md L57-59 promises.

This is the exact TB-244-shape transplant from axis 4 to the
validator-judge axis: same closure mechanism, different event types.

Why now: TB-243 shipped pull-surface observability for these events
last cycle (commit `647b771`, 2026-05-16T23:59Z) but explicitly left
the push surface uncovered — same gap shape TB-244 (`aa971f8`,
2026-05-17T00:09Z) closed for axis-4 focus rotation one tick later.
Without parity closure, the walk-away-time promise (goal.md L57-59
"walk away for a week without intervention") stays partially
delivered: pull observability works for the operator who logs in,
but the operator who walks away gets no validator-judge signal
through the only channel that pushes to them.

## Scope

(1) Extend `_STATUS_REPORT_AUTOMATION_INTERESTING_TYPES` in
    `ap2/status_report.py` (current frozenset at line 548-557) to
    include `validator_judge_fail` + `validator_judge_timeout`. This
    ensures `_status_report_should_skip` correctly un-skips the report
    when a fresh validator-judge event arrives.

(2) Add `collect_window_validator_judge(cfg, now_s, window_s) -> dict`
    helper in `ap2/automation_status.py` mirroring the shape of
    `collect_window_focus_rotation` (TB-244). Returns
    `{"validator_judge_fail_count": int,
      "validator_judge_timeout_count": int,
      "total": int,
      "noisy_threshold": int,
      "is_noisy": bool}`. Reuses existing `_count_events_24h` helper
    (automation_status.py:505-512) and existing
    `validator_judge_noisy_threshold()` (automation_status.py:134).

(3) Add `render_validator_judge_activity_section(state) -> list[str]`
    renderer in `ap2/status_report.py` mirroring the shape of
    `render_focus_rotation_activity_section` (TB-244). Returns an
    empty list when both counts are zero (default-off byte-identical
    regression pin). When non-zero, renders a sub-section header
    (`*Validator-judge fail-open window (24h):*`) plus per-event-type
    lines and a `[noisy]` suffix when `is_noisy` is True (mirrors
    TB-243's pull-side badge convention).

(4) Wire the new renderer into `run_status_report`'s `state_extras`
    payload so the prompt-forwarded state carries the new section
    verbatim. Update `_STATUS_REPORT_CONTRACT` + `STATUS_REPORT_PROMPT`
    in `ap2/prompts.py` to enumerate the new line as
    verbatim-forwarded content (parallel to TB-244's focus-rotation
    contract addition). Update `ap2/cron.default.yaml` stub if it
    enumerates state-extra keys.

(5) Cross-reference the new push surface in `ap2/howto.md`
    "Validator judge" / "Verification" section so operators can find
    the push-channel counterpart to TB-243's pull surface.

## Design

Mirror TB-244's "option B" shape exactly — that's the freshest
worked example of push-surface parity in this codebase, was
operator-approved and shipped cleanly without edit, and the two
gaps differ only in the event-type-set being aggregated:

- **Collector helper** (`automation_status.py`): a new pure function
  `collect_window_validator_judge` modelled byte-for-byte on
  `collect_window_focus_rotation` (TB-244). Takes `(cfg, now_s,
  window_s)`, reads the events tail via the existing helper,
  counts the two event types via `_count_events_24h`, reads the
  threshold via the existing `validator_judge_noisy_threshold()`,
  returns a dict. NOT folded into `collect_auto_approve_state`
  (TB-243's collector) — the digest renderer reads its own keyed
  state-extras block, not the auto-approve state object.

- **Renderer** (`status_report.py`): a new pure function
  `render_validator_judge_activity_section(state)` returning a
  list[str], modelled on `render_focus_rotation_activity_section`.
  Empty-list-on-zero-counts is the load-bearing default-off
  byte-identical pin (operators today get zero validator-judge
  output and most days that should continue). When non-zero,
  emits 1 header line + 1 line per non-zero event type + the
  `[noisy]` suffix on the header when `is_noisy`.

- **Wiring** (`status_report.py:run_status_report`): include the
  collector call + renderer output in `state_extras` exactly where
  TB-244 added the focus-rotation block. Order: place the new
  sub-section directly after the focus-rotation block so the
  digest reads top-down as "automation activity → focus rotation
  → validator-judge fail-open" (axis-1 safety net at the end so
  the eye lands on it last; mirrors TB-243's web home placement
  of the validator-judge row at the bottom of the automation card).

- **Prompt-forwarding** (`prompts.py`): add one line to the
  `_STATUS_REPORT_CONTRACT` enumeration and the
  `STATUS_REPORT_PROMPT` verbatim-forwarding list. The contract
  is load-bearing — TB-228 / TB-238 / TB-244 all extended it the
  same way; the cron status-report agent reads the contract to
  decide what counts as forward-eligible.

- **Skip-gate behavior** (`status_report.py:_status_report_should_skip`):
  no code change needed; just adding the two event types to the
  frozenset is sufficient — the skip-gate reads from
  `_STATUS_REPORT_AUTOMATION_INTERESTING_TYPES` directly. Pin
  this with a regression test: a tail containing only a fresh
  `validator_judge_fail` event must NOT skip the report.

- **Tests** (`ap2/tests/test_tb245_status_report_validator_judge_digest.py`):
  new module, modelled byte-for-byte on
  `test_tb244_status_report_focus_rotation_digest.py`. Covers:
  collector returns zero-state correctly when no events;
  collector counts events within the 24h window and excludes
  older ones; renderer returns empty list on zero-state
  (byte-identical default-off pin); renderer emits the
  sub-section + `[noisy]` badge when threshold crossed; renderer
  emits the sub-section WITHOUT `[noisy]` when threshold not
  crossed; `_status_report_should_skip` does NOT skip on a tail
  with only a fresh `validator_judge_fail` event; prompt-forward
  contract enumerates both event types.

- **Cross-reference in howto.md**: extend the validator-judge
  subsection added by TB-243 (which described the pull surface)
  to point at the new push-surface counterpart. One paragraph;
  no separate top-level section.

Anti-shape avoided: do NOT fold the new collector into TB-243's
`collect_auto_approve_state` — keeping collector boundaries
aligned with renderer boundaries (per axis / per channel) is
what made TB-244 a clean extension and is what will make TB-246
or TB-247 cheap if a third event-class needs the same shape.

## Verification

- `uv run pytest -q ap2/tests/` — full suite green vs current baseline.
- `uv run pytest -q ap2/tests/test_tb245_status_report_validator_judge_digest.py` — new test module exists and all behavioral cases pass.
- `test -f ap2/tests/test_tb245_status_report_validator_judge_digest.py` — new test module present on disk.
- `grep -nE "validator_judge_fail" ap2/status_report.py` — event type added (at minimum in the interesting-types frozenset).
- `grep -nE "validator_judge_timeout" ap2/status_report.py` — sibling event type added.
- `grep -nE "def render_validator_judge_activity_section" ap2/status_report.py` — new renderer declared.
- `grep -nE "def collect_window_validator_judge" ap2/automation_status.py` — new collector helper declared.
- `grep -nE "validator_judge" ap2/howto.md` — howto.md cross-reference added or updated to name the push surface.
- `grep -nE "validator_judge_fail|validator_judge_timeout" ap2/prompts.py` — `_STATUS_REPORT_CONTRACT` / `STATUS_REPORT_PROMPT` enumerates the new verbatim-forwarded line.
- Prose: `ap2/status_report.py` Prose: `render_validator_judge_activity_section` mirrors `render_focus_rotation_activity_section`'s shape — returns an empty list when both 24h counts are zero (byte-identical default-off pin), and otherwise emits a sub-section header plus per-event-type lines with a `[noisy]` suffix when total crosses `AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD`.
- Prose: `ap2/status_report.py` Prose: `_STATUS_REPORT_AUTOMATION_INTERESTING_TYPES` includes both `validator_judge_fail` and `validator_judge_timeout`, and `_status_report_should_skip` reads the updated frozenset so a tail containing only a fresh `validator_judge_fail` event correctly un-skips the report (existing skip-gate test extended or new case added to cover this path).

## Out of scope

- Doctor-side runtime warning (extending `ap2/doctor.py` to read recent events.jsonl and WARN when validator-judge counts cross threshold under `AP2_AUTO_APPROVE=1`) — separate proposal; keeps doctor focused on env-knob misconfig shape (TB-234 / TB-239 lineage).
- Mattermost push-on-noisy (immediate post when threshold crossed) — daemon has no outbound MM helper today (`ap2/mattermost.py` is inbound-only); 2h cron cadence is the interim push channel; revisit if latency proves insufficient.
- Auto-pause auto-approve on validator-judge fail-open rate threshold — policy change requiring operator deliberation; observability ships first, gating policy later.
- Web home renderer for axis-4 focus rotation history — out of scope here; web home already renders the current focus card via TB-242 and the operator can drill into events via the existing /events page.
## Attempts

### 2026-05-17 — verification_failed
(no summary)
- **kind:** project_wide
- **verify_command:** uv run pytest -q ap2/tests/
- **exit_code:** None
- **stderr_tail:** 
- **Debug dumps:** `prompt: .cc-autopilot/debug/20260517T064516Z-TB-245.prompt.md`, `stream: .cc-autopilot/debug/20260517T064516Z-TB-245.stream.jsonl`, `messages: .cc-autopilot/debug/20260517T064516Z-TB-245.messages.jsonl`
### 2026-05-17 — verification_failed
(no summary)
- **kind:** project_wide
- **verify_command:** uv run pytest -q ap2/tests/
- **exit_code:** None
- **stderr_tail:** 
- **Debug dumps:** `prompt: .cc-autopilot/debug/20260517T095725Z-TB-245.prompt.md`, `stream: .cc-autopilot/debug/20260517T095725Z-TB-245.stream.jsonl`, `messages: .cc-autopilot/debug/20260517T095725Z-TB-245.messages.jsonl`

## Goal

Current focus: end-to-end automation — axis 4 (multi-focus sequential
execution, goal.md L115-138). TB-242 (6704ed52, 21:59:15Z) just
landed `roadmap_complete` + active-focus visibility in `ap2 status`
text/JSON and the web home page — the pull-surface half of axis-4
observability. The push-surface half (the 2h status-report cron post
that reaches Mattermost — the operator's primary walk-away channel)
does not yet carry axis-4 information: `roadmap_complete` and
`focus_advanced` are absent from
`_STATUS_REPORT_AUTOMATION_INTERESTING_TYPES`
(`ap2/status_report.py:426`) and the digest renderer
`render_automation_loop_activity_section`
(`ap2/status_report.py:137`) only covers axes 1+2+3. When the
daemon halts on roadmap-exhaustion at 03:00Z, the operator's only
push channel carries no axis-4 line — walk-away time on the
rotation-halt signal stays bounded by manual `ap2 status` checks,
which contradicts the focus's own framing ("walk-away time scales
with the operator-declared roadmap length", goal.md L137-138).

Why now: TB-242 just shipped the pull surface; the digest-renderer
test+code patterns from TB-228 + TB-238 are still fresh. This is the
cheapest moment to close the surface-parity gap before context
decays, AND axis 4's whole value proposition collapses if the
operator can walk away for a week but loses 6+ hours of progress
every time the daemon halts because they didn't get a push notice.

## Scope

(1) Add `"roadmap_complete"` and `"focus_advanced"` to
`_STATUS_REPORT_AUTOMATION_INTERESTING_TYPES` (frozenset at
`ap2/status_report.py:426`). Update the docstring on
`_status_report_should_skip` (L456-462) to name the two new types
alongside the existing axis-1/2/3 entries.

(2) Extend `render_automation_loop_activity_section` (~L137) — or
add a parallel `render_focus_rotation_section` invoked from the
same `state_extras` site in `run_status_report` (status_report.py:735
per agent verification) — with a "## Focus rotation activity"
sub-block that renders only when at least one `focus_advanced` OR
`roadmap_complete` event lands in the since-last-report window
(reuse `collect_window_loop_activity` + `find_previous_status_report_idx`
from TB-228, or a parallel `collect_window_focus_rotation` helper
in `automation_status.py` if cleaner).

  Rendering shape (omit-on-empty per TB-228 precedent):

      ## Focus rotation activity

      - focus_advanced: <from-title> → <to-title> (N of M)
      - roadmap_complete: all foci exhausted — `ap2 ack roadmap_complete` to resume

  Each line is rendered once per event in the window (so a window
  with 2 advances + 1 halt yields 3 lines). Read titles from each
  event's payload (TB-226 emits them).

(3) Wire the new renderer into `run_status_report`'s `state_extras`
the same way TB-228's `render_automation_loop_activity_section` is
wired. If extending the existing renderer in-place (option A from
scope item 2), no wiring change needed; if adding a parallel
renderer (option B), follow the same `state_extras` pattern.

(4) Update `_STATUS_REPORT_CONTRACT` + `STATUS_REPORT_PROMPT` (in
`ap2/prompts.py`) + the cron stub in `ap2/cron.default.yaml` to
teach the agent verbatim-forwarding for the new sub-block (parallel
to how TB-228 + TB-238 taught the automation digest + dry-run
sub-block).

(5) Tests in a new module
`ap2/tests/test_tb244_status_report_focus_rotation_digest.py`
covering: (a) `_status_report_should_skip` returns False when only
a `roadmap_complete` event sits in the since-last-report window;
(b) `_status_report_should_skip` returns False when only a
`focus_advanced` event sits in the window; (c) renderer emits the
expected lines for a window with 0/1/multiple focus_advanced events;
(d) renderer renders the `roadmap_complete` line with the ack hint
verbatim; (e) renderer omits the entire sub-block when the window
has zero axis-4 events (byte-identical to no-renderer baseline);
(f) the `_STATUS_REPORT_AUTOMATION_INTERESTING_TYPES` frozenset
contains both new tokens.

(6) Cross-reference in `ap2/howto.md` (the existing TB-226 focus
rotation section): brief paragraph noting that axis-4 events also
surface in the status-report cron digest per TB-244.

## Design

Two implementation shapes:

  - **Option A (in-place extension)**: append focus-rotation lines
    to the existing `render_automation_loop_activity_section`
    output, gated by a new sub-block heading inside the same
    section. Pro: single renderer call; con: muddles axis-1/2/3
    digest semantics with axis-4.

  - **Option B (parallel renderer)**: new
    `render_focus_rotation_activity_section` in `status_report.py`
    + new `collect_window_focus_rotation` helper in
    `automation_status.py` mirroring `collect_window_loop_activity`.
    Pro: clean separation, easier to evolve; con: two state_extras
    wirings.

Recommend Option B — keeps the axis-1/2/3 digest's existing test
expectations byte-identical and gives axis 4 its own surface that
can grow (next round: per-focus token usage, ideation cycles per
focus, etc.) without re-touching TB-228 / TB-238 territory.

Skip-gate change is one-line (add two strings to the frozenset).
The boring-types denylist at `_STATUS_REPORT_BORING_TYPES`
(status_report.py:413) does NOT currently list these tokens, so
in the structural-gate sense they already count as interesting;
the frozenset add makes the positive-allowlist anchor accurate
(per the comment at L418-425). Tests pin both behaviors.

Contract/prompt updates teach the cron agent that the new
sub-block is verbatim-forwarded — same pattern as TB-228's
contract update; no agent intelligence required, just a
"reproduce exactly" line.

## Verification

- `uv run pytest -q ap2/tests/test_tb244_status_report_focus_rotation_digest.py` — new test module passes.
- `uv run pytest -q` — full suite passes (TB-228 / TB-238 digest tests must remain byte-identical when no axis-4 events present).
- `grep -n '"roadmap_complete"' ap2/status_report.py` — at least one match (frozenset entry).
- `grep -n '"focus_advanced"' ap2/status_report.py` — at least one match (frozenset entry).
- `grep -n "Focus rotation" ap2/status_report.py` — at least one match (new renderer or in-place section heading).
- `grep -n "TB-244" ap2/howto.md` — at least one match (cross-reference).
- `grep -n "Focus rotation" ap2/cron.default.yaml ap2/prompts.py` — at least one match across the two files (contract/prompt teaches verbatim-forwarding of the new sub-block).
- `ap2/status_report.py` Prose: `_STATUS_REPORT_AUTOMATION_INTERESTING_TYPES` contains the literal strings `"focus_advanced"` and `"roadmap_complete"`, and the docstring of `_status_report_should_skip` names both event types as interesting; judge confirms via Read of `ap2/status_report.py` and the new test module's frozenset-membership assertion.

## Out of scope

- Daemon-direct Mattermost push on `roadmap_complete` emission (defer until 2h cron-post latency proves insufficient in practice; this task closes the same gap via the existing channel).
- Per-focus token usage / ideation-cycles aggregation in the digest (the new section is the substrate for that later; this task ships only event-line rendering).
- Auto-extending `goal.md` on `roadmap_complete` (explicit Non-goal per goal.md L187-191).
- Wiring `roadmap_complete` into the auto-approve halt path (TB-226's `goal.roadmap_exhausted` already gates dispatch; this task is observability only).
- Surfacing axis-4 in the web home automation card (TB-242 already covers the web pull surface; this task ships only the Mattermost push surface).

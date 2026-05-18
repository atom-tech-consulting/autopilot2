# Fix `_render_automation_card` in `web.py` — mirror TB-250's three-state rendering on the web home (auto-approve OFF + activity → renders "disabled", not "enabled")

Tags: `#autopilot` `#bug` `#web` `#operator-surface` `#regression-pin`

## Goal

Advance goal.md's **Current focus: end-to-end automation** focus's (1) **Manual-approval bottleneck** axis by fixing the web home's `_render_automation_card` rendering bug — symmetric mirror of TB-250 (`dd623ae`) which fixed the analogous bug in `cli.py:cmd_status`. Today `ap2/web.py:1668-1671` in `_render_automation_card` enters the "render the automation card" branch whenever `auto_approve_enabled` is true OR `counters_total > 0` (which includes validator-judge fail/timeout counts per TB-243), and unconditionally renders `body = "enabled — circuit healthy"` even when `auto_approve_enabled` is `false` and the only reason the card is showing is validator-judge activity. Web operator looking at `http://127.0.0.1:8730/` sees "Auto-approve: enabled — circuit healthy" while `ap2 status --json`'s `auto_approve_enabled: false` says the opposite — exactly the same operator-trust erosion TB-250 closed for the CLI surface.

Why now: operator (2026-05-18) opened the web UI right after the daemon restart picked up TB-250's CLI fix, expected the web surface to be consistent, and observed the rendering still says "enabled" on the web home. The CLI is correct; the web isn't. This is the same regression — TB-250's scope explicitly bounded "fix is local to text rendering, not the activity check," but it was applied only to `cli.py`, leaving `web.py`'s parallel render path unfixed. Fix the web mirror so both surfaces agree.

## Scope

(1) **Fix `ap2/web.py:_render_automation_card` (lines ~1668-1671)** to mirror TB-250's three-branch rendering:

  - **State A — auto-approve enabled, not paused**: header `"Auto-approve"`, body `"enabled — circuit healthy"`. Current text, unchanged. (klass=`"automation-status is-healthy"`)
  - **State A-paused — auto-approve enabled but paused**: header `"Auto-approve — PAUSED"`, body with reason + consecutive freezes. Current text, unchanged. (klass=`"automation-status is-paused"`)
  - **State B — auto-approve disabled but other activity present**: header `"Auto-approve"`, body `"disabled (24h activity: …)"` summarizing the validator-judge fail/timeout counts (and any other counters that triggered the card-visible branch). **NEW state — what TB-250 added on the CLI side, missing on web.** Use a neutral klass like `"automation-status is-disabled-but-active"` so CSS can style it distinctly from the green "is-healthy" state.
  - **State C — auto-approve disabled, no activity**: card suppressed entirely (existing `if not enabled and counters_total == 0: return ""` branch at line 1642 — unchanged).

(2) **Add CSS rule for the new klass** in the existing inline-CSS block — distinct background or border color from `is-healthy` (which is currently green-tinted per the existing style). Suggested neutral grey-tinted style to communicate "informational, not green-flag." Mirror the existing `is-paused` styling pattern (yellow/red-tinted).

(3) **Regression-pin tests** (`ap2/tests/test_tb_web_automation_card_rendering.py` or extend the closest existing web-test module):
  - `test_web_card_renders_enabled_when_auto_approve_on`: stub `collect_auto_approve_state` with `auto_approve_enabled=True`; assert rendered HTML contains `"enabled — circuit healthy"` AND does NOT contain `"disabled"`.
  - `test_web_card_renders_disabled_when_off_but_validator_activity_present`: stub with `auto_approve_enabled=False`, `validator_judge_fail_count_24h=4`; assert HTML contains `"disabled"` AND a count of `4` somewhere in the body. Should NOT contain `"enabled — circuit healthy"`.
  - `test_web_card_suppressed_when_off_and_no_activity`: stub with all counters zero + `auto_approve_enabled=False`; assert `_render_automation_card` returns the empty string (unchanged existing behavior).
  - `test_web_card_paused_state`: stub with `auto_approve_paused=True`; assert HTML contains `"PAUSED"` (existing behavior unchanged).

(4) **Don't refactor the surrounding rendering** — only the conditional branch + body string + CSS rule. Sibling sections (sparklines from `_hourly_sparkline_buckets`, dry-run badge, focus-rotation block) stay byte-identical.

(5) **Don't extend the test surface beyond the four cases above** — this is a single rendering branch with three meaningful states + the suppressed case. More tests would be over-engineering.

(6) **Don't update `cli.py`** — TB-250 already shipped that fix. This TB is the web mirror only.

## Design

Direct application of TB-250's design to the web surface. The conditional logic structure is shared between `cli.py:cmd_status` (TB-250's fix) and `web.py:_render_automation_card` (this fix); a shared helper would eliminate the duplication entirely. But factoring out the rendering logic touches two surfaces simultaneously and introduces a new abstraction — bigger blast radius than this surgical fix. If a third surface (e.g. a future MCP tool that returns the same summary) appears at n=3 per goal.md L74-77, that's the trigger to extract; today's threshold-two doesn't motivate it.

The CSS klass distinction (`is-disabled-but-active`) lets operators visually distinguish "OK, all green" from "card visible because something is happening but auto-approve is off" without reading the body text. Small UX win that costs ~3 lines of CSS.

**Goal-anchor**: same as TB-250 — the Done-when bullet "an operator can point ap2 at a fresh project, paste a `goal.md`, and walk away for a week without intervention" depends on accurate state representation across surfaces. A web home that says "enabled" when JSON says "disabled" creates false confidence; the operator might base auto-approve decisions on the wrong picture.

## Verification

- `uv run pytest -q ap2/tests/` — full suite green (exit 0).
- `uv run pytest -q ap2/tests/test_tb_web_automation_card_rendering.py` — new test module passes (4 cases per Scope §3).
- `grep -nE 'disabled.*24h activity|disabled \(' ap2/web.py` — exit 0; the new disabled-with-activity branch is wired (body string is searchable).
- `grep -nE 'is-disabled-but-active|is-disabled-with-activity' ap2/web.py` — exit 0; the new CSS klass is present (string-grep on the klass name; matches the implementer's naming choice if they pick a different exact string).
- Prose: post-fix, with `AP2_AUTO_APPROVE` unset AND validator-judge fails in the 24h window, loading `http://127.0.0.1:8730/` MUST show an automation card whose body text contains `"disabled"` AND MUST NOT show `"enabled — circuit healthy"`. Judge confirms via curl + grep (`curl -s http://127.0.0.1:8730/ | grep -E "auto-approve|Auto-approve" | head -5` after restart).
- Prose: when `AP2_AUTO_APPROVE=1` IS set, the card text reverts to the original `"enabled — circuit healthy"` (regression-pin — the fix doesn't break the enabled-state rendering). Judge confirms via test case `test_web_card_renders_enabled_when_auto_approve_on`.

## Out of scope

- Refactoring the shared rendering logic between `cli.py:cmd_status` and `web.py:_render_automation_card` into a common helper — see Design. Threshold-two; defer until a 3rd render surface appears.
- Restructuring the `counters_total` aggregator — the aggregator is correct as-is (TB-243's design); only the text branch needs fixing.
- Adding sparkline/trend visualization for the disabled-with-activity state — separate UX surface.
- Updating Mattermost status-report digest's auto-approve framing — separate surface (and the digest already says "auto-approve: enabled" only when truly enabled per TB-228's renderer; not affected by this bug).
- Daemon restart automation that picks up rendering fixes without operator action — see the stale-running-daemon discussion; separate observability TB if filed.

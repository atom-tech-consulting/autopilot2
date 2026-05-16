## Goal

Current focus: end-to-end automation — close the observability gap on
TB-235's dependency-coherence judge fail-open path so the
auto-approve safety claim (goal.md L82-85: "upstream gates already
make this safe in practice") stays falsifiable. TB-235 added a
Haiku-4.5 judge to `_validate_briefing_structure` that rejects
briefings naming an implicit hard predecessor not declared in
`@blocked:TB-N`; it emits `validator_judge_fail` /
`validator_judge_timeout` events on SDK errors (registered in
`ap2/events.py:87`) and fails open — the briefing passes the gate
when the judge couldn't render a verdict. Today no surface anywhere
consumes those events: `automation_status.collect_auto_approve_state`
(automation_status.py:331) carries 11 keys covering the auto-approve
loop but nothing on validator-judge health; `ap2 status`
(cli.py:106) and the web home automation card
(`_render_automation_card`, web.py:1584) render the collector's
output but expose no validator-judge line. An operator with
`AP2_AUTO_APPROVE=1` who has not noticed the judge silently timing
out for 10 consecutive briefings has 10 briefings of weakened
dep-coherence coverage before the next audit — undetectable from
either pull surface.

Why now: TB-241 (fc14fe3, 21:50:26Z) just landed dry-run readiness
parity in `ap2 status` and the web home automation card; the
collector + surface pattern is fresh in the test+rendering modules,
making this the cheapest moment to add the parallel validator-judge
pair before context decays. Without this, the auto-approve safety
narrative depends on a gate whose health the operator cannot
observe — every step toward "walk away" implicitly trusts a
fail-open without a way to detect failure mode bias.

## Scope

(1) Extend `collect_auto_approve_state(cfg)` in
`ap2/automation_status.py` (currently 11 keys; ~line 331) with two
new keys derived from the same `events.tail(cfg.events_file, n=2000)`
scan already in use:

  - `validator_judge_fail_count_24h: int`
  - `validator_judge_timeout_count_24h: int`

Both default to 0 when no events seen. Use the same `ts >= now - 24h`
filter the existing 24h counters use.

(2) Text-rendering branch of `cmd_status` in `ap2/cli.py` (the
`automation:` block, ~line 106): when EITHER 24h count > 0, append a
single sub-line of the shape

    validator-judge: N fail | M timeout (24h)

  omit-on-zero (both counts must be 0 to omit; matches the existing
  dry-run line's omit-on-empty pattern from TB-241).

(3) `--json` branch: add nested object under the existing
`auto_approve` key, always present (zeros when no events):

    "validator_judge": {"fail_count_24h": N, "timeout_count_24h": M}

(4) `_render_automation_card` in `ap2/web.py` (~line 1584): add a
"Validator judge (24h)" row showing the two counts; render only when
either count > 0 (omit-on-empty); same TB-148 palette as the dry-run
row (neutral tint when below threshold, warn tint when at-or-above).

(5) Env knob `AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD` (default 5).
When `(fail + timeout) >= threshold`, the text sub-line gets a
` [noisy]` suffix and the web row gets the warn tint. Document the
knob in `ap2/howto.md` alongside the TB-235 dep-coherence section.

(6) Tests in a new module
`ap2/tests/test_tb243_validator_judge_surface.py` covering:
collector zero-default; collector 24h-window correctness (events
outside window excluded); cli text omit-on-zero + render-on-nonzero
+ `[noisy]` suffix at threshold; cli `--json` always-present zero
object; web HTML omit-on-zero + render-on-nonzero + warn-tint class
at threshold.

(7) Cross-link in `ap2/howto.md`: brief paragraph in the existing
TB-235 dep-coherence section pointing to the new surface.

## Design

Collector extension follows the same pure-tail-scan pattern
established in TB-227 / TB-238 / TB-241: one `events.tail(..., n=2000)`
call, two new counter accumulators ticked when
`evt.get("type") in {"validator_judge_fail", "validator_judge_timeout"}`
and the ts is within the 24h window. Two keys exposed on the
collector dict so the rendering layer (`ap2 status`, `_render_automation_card`,
plus future status-report digest consumers) can read them without
re-scanning events.jsonl.

Threshold gating lives at the rendering layer only — the collector
emits raw counts. `AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD` is parsed
once per render call (cheap; matches how TB-224 token caps are read).
Default 5 chosen so a single transient SDK blip doesn't flip the
surface to warn-tint, but a sustained issue (>5 fails in 24h) does.
The knob's parse semantics mirror TB-224 / TB-234: unset / empty /
non-int / non-positive → treat as default (5).

Text + JSON + web are three parallel surfaces consuming the same
collector keys; tests pin each surface independently. The
omit-on-zero text/web behavior mirrors TB-241's dry-run line so
operators reading `ap2 status` see a stable surface when the gate
is healthy and only see the noise line when it's actually noisy.

No daemon-side changes (no new events emitted; no new gates;
no auto-disable behavior — those are explicit out-of-scope items).
All work is read-layer composition over events.jsonl, matching the
TB-227 / TB-241 / TB-242 family of surface-extension tasks.

## Verification

- `uv run pytest -q ap2/tests/test_tb243_validator_judge_surface.py` — new test module passes.
- `uv run pytest -q` — full suite passes (no regressions on existing TB-227 / TB-238 / TB-241 automation-card tests).
- `grep -n "validator_judge_fail_count_24h" ap2/automation_status.py` — at least one match (new collector key wired).
- `grep -n "validator_judge_timeout_count_24h" ap2/automation_status.py` — at least one match (new collector key wired).
- `grep -n "validator-judge" ap2/cli.py` — at least one match (new text sub-line).
- `grep -n "validator_judge" ap2/web.py` — at least one match (new web card row).
- `grep -n "AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD" ap2/howto.md` — at least one match (env knob documented).
- `ap2/web.py` Prose: `_render_automation_card` renders the new "Validator judge (24h)" row with omit-on-empty behavior, warn-tint when `(fail + timeout) >= AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD` (default 5), and neutral tint otherwise; judge confirms via Read of `ap2/web.py` and the new test module's HTML assertions.

## Out of scope

- Adding a `ap2 doctor` warning for noisy validator-judge (cross-axis doctor composite deferred per ideation_state.md).
- Surfacing per-briefing judge verdicts (only aggregate health here; per-briefing forensics stay in events.jsonl + the existing judge_call debug dumps).
- Auto-disabling the dep-coherence judge when noisy (operator decides via `AP2_VALIDATOR_JUDGE_DISABLED=1` per TB-235; this task only surfaces the signal).
- Mattermost push on noisy threshold (defer until the 2h status-report digest proves insufficient as the push channel).
- Extending the status-report cron digest with the validator-judge counts (a parallel proposal could cover this; keep this task scoped to the `ap2 status` + web home pull surfaces to mirror TB-241's split).

---
name: ap2-observability
description: "Use when inspecting ap2 event types, the events.jsonl timeline, prose-judge verification diagnostics, or `ap2 logs` / stats-dashboard output."
---

# ap2 observability — events, prose-judge diagnostics, logs & stats

How to read what an ap2 daemon is actually doing. Four self-contained
surfaces:

- **Event schema** — every `type` string the daemon can write to the
  append-only `.cc-autopilot/events.jsonl` timeline, grouped by category,
  so you can map an event you just saw back to the code that emitted it.
- **Prose-judge diagnostics** — how to debug a `## Verification`
  prose-bullet verdict (length signals, parse-error categories, on-disk
  dumps).
- **Live event tail (`ap2 logs`)** — watch an active arc unfold instead of
  grepping the JSONL by hand.
- **Stats dashboard** — multi-day trend aggregates for walk-away review.

Reach for the relevant section below; each stands on its own.

## Event schema (the canonical timeline)

`.cc-autopilot/events.jsonl` is append-only. Every line has `ts` (UTC
ISO-8601) + `type`; other fields vary. Categories:

**Lifecycle.** `daemon_start`, `daemon_stop`, `daemon_pause`,
`daemon_resume`, `task_solve` (TB-385 — renamed from `task_start`; the
first of the three per-task lifecycle verbs `task_solve` → `task_verify`
→ `task_complete`; pre-TB-385 history still carries `task_start`, which
readers accept alongside the new name), `task_verify` (TB-385 — the
single terminal verification event, emitted once after the project-wide
`AP2_VERIFY_CMD` gate AND all per-task prose-bullet judging, just before
`task_complete`; folds the old mid-stream `verify_passed` + per-bullet
`judge_call` events into one legible event. Payload: `verdict`
(pass|fail|partial), `shell`/`prose` `"N_pass/N_total"` tallies,
`verify_cmd` (optional `{command, exit_code, duration_s}` from the
project-wide gate), `bullets` (per-bullet `{idx, kind, verdict}` so the
drill-down survives without per-bullet events), optional
`transient_retries`. Emitted on ALL outcomes — pass, fail, AND partial —
so it is the one terminal verification event regardless of result),
`task_complete`, `cron_start`,
`cron_complete`, `cron_skipped` (status-report no-op — carries a
`reason` field naming which gate fired:
`reason=no_activity_since_last_report` (TB-128/153 — the inter-report
window carries zero "interesting" events past the previous
`cron_complete name=status-report`); `reason=duplicate_content`
(TB-281 — events DID land but the prospective post is structurally
identical to the last one, per the SHA-1 fingerprint stashed under
`status-report.last_post_fingerprint` in `cron_state.json` over
board counts + pending-review TB-Ns + decisions-needed bullets +
digest sub-section contents + halt reason; closes a goal.md focus
`Progress signals:` bullet on report-worthy change vs clock-driven
re-fires)), `cron_bootstrap` (first-run
seeding of `cron.yaml` from `cron.default.yaml`), `ideation_empty_board`
(skip — no slots OR cooldown), `ideation_forced` (operator forced via
`ap2 ideate --force`), `ideation_skipped` / `ideation_skipped_no_slots`,
`ideation_complete`, `ideation_state_updated`, `ideation_state_scrubbed`
(TB-284 — `_run_ideation`'s post-write filter stripped exhaustion-
asserting sentences from `ideation_state.md` after the agent finished
writing; payload `removed_chars=<N>` byte-length delta; fires only
when the scrubbed text differs from the agent's original — already-
clean files are the steady-state silent no-op; the scrub is fail-safe
by returning the input unchanged on any SDK error),
`ideation_state_scrub_error` (TB-294 — `_maybe_scrub_ideation_state`
caught a typed `ideation_scrub.ScrubError` subclass and preserved
the original file on disk; payload `reason=timeout|sdk_error|empty_output`
+ `duration_s` (wall-clock to the exception catch) + `error` (the
exception message — `<ExceptionType>: <message>` for `sdk_error`,
worker-grace message for `timeout`, fixed sentinel for `empty_output`);
fail-open audit closes the silent-timeout blind spot the original
TB-284 design left when the scrub SDK call hit the 60s budget on every
production cycle — see `ap2/ideation_scrub.py` for the typed
exception classes and the `thinking={"type": "disabled"}` SDK-options
companion fix that eliminates the Haiku-4.5 extended-thinking
auto-engagement that was the silent-timeout root cause),
`web_start`, `web_stop`,
`env_reloaded` (TB-271 — daemon `_tick` re-sourced `.cc-autopilot/env`
at tick-top and detected at least one knob whose value changed; payload
`changed` / `hot` / `fixed` / `other` knob lists; mutates the tunable
`Config` dataclass fields in-place AND overwrites `os.environ` for
file-sourced keys while preserving "shell export wins" for keys never
set by the file; removes the restart-to-apply-a-knob friction TB-260
only warned about; mtime-gated so a static env file is a cheap no-op
each tick — see the operator manual's `Configuration knobs` reference for
the hot-reloadable vs fixed split).
Per-run cost/usage: `task_run_usage` (per task agent run, TB-180),
`control_run_usage` (per cron / ideation / MM-handler run, TB-179),
`judge_call` (TB-69 + TB-181 — **as of TB-385 emitted only by the
janitor's per-finding judge**; the per-task-VERIFIER prose-bullet
`judge_call` was folded into the terminal `task_verify` event's
`bullets[]`, so verifier prose verdicts no longer stream as separate
events). Verifier per-run wall-clock: `verify_passed` (TB-252 — **legacy
as of TB-385**; the project-wide `AP2_VERIFY_CMD` success audit is now
carried by `task_verify`'s `verify_cmd.duration_s`; pre-TB-385 history
still carries `verify_passed`, and `verify_timeout_audit` reads BOTH
shapes to size `AP2_VERIFY_TIMEOUT_S` against observed-typical successful
run duration).

**Failure.** `task_error`, `task_timeout`, `task_state_violation` (TB-110
post-hoc fenced-file check tripped), `task_rollback` (TB-110
rollback to pre-task state), `verification_failed` (per-task or
project-wide), `verification_partial`, `retry_exhausted`,
`effort_downshift` (TB-356 — a task failed with the bundled-CLI
thinking-block-immutability 400 and the daemon stepped its reasoning
effort down one tier for the automatic retry; payload `task`, `from`
(the just-failed run's effort), `to` (the retry's effort), `level`
(the new per-task downshift level), `reason=thinking_block_corruption`.
Gated by `AP2_THINKING_BLOCK_EFFORT_DROP_DISABLED` and emitted only for
this specific 400 — `verification_failed` / generic `task_error` never
downshift),
`cron_error`, `cron_timeout`, `ideation_error`, `ideation_timeout`,
`mattermost_error`, `mattermost_timeout`, `mm_poll_error`,
`env_reload_error` (TB-271 — `env_reload.maybe_reload_env` raised at
tick-top; swallowed defensively so the rest of the tick continues on
whatever cfg state survived; payload `error=<ExceptionType>: <message>`),
`effective_config_write_error` (TB-379 — publishing the per-tick
effective-config snapshot (`.cc-autopilot/effective_config.json`, the
daemon's actually-resolved component/knob state that `ap2 status` reads
cross-process so it reports the DAEMON's config, not a CLI-local env
re-resolution) raised; swallowed defensively at the tick-top write and
the daemon-start write so a filesystem hiccup never takes the daemon
down — a stale/absent snapshot just sends `ap2 status` down its labelled
`(daemon not running — showing local config)` fallback; payload
`error=<ExceptionType>: <message>`),
`env_deprecated` (TB-323 — the structured-config back-compat shim in
`ap2/config_compat.py::_apply_flat_back_compat` detected a flat-name
`AP2_*` env var listed in `FLAT_TO_SECTIONED` and overlaid the value at
its sectioned counterpart on the loaded `Config`; one-shot per process
per knob — module-level `_EMITTED_ONCE: set[str]` guarded by a
`threading.Lock` records each flat name's first hit, so a daemon read
at startup + re-checks on later config reloads stay silent past the
first; payload `flat` (the deprecated env name — e.g. `AP2_AUTO_APPROVE`),
`sectioned` (its replacement path — e.g.
`components.auto_approve.enabled`), `process_pid` (so a multi-daemon
operator setup can attribute the event to its emitter); the
audit trail makes the migration discoverable in `events.jsonl` —
a fresh ap2 upgrade surfaces every still-set legacy knob at first
daemon-start, operators remove them in favor of the sectioned config
keys, subsequent starts go silent; NOT emitted by the sectioned-env
override path nor for knobs in
`config_compat._KNOBS_STAYING_ENV_ONLY` — the 12-factor exemption set
(Mattermost auth / channel identity, integration secrets, deployment
paths) doesn't migrate to TOML by design),
`config_updated` (TB-324 — operator-CLI `ap2 config set <path>
<value>` was drained by the daemon and wrote the resolved value into
`.cc-autopilot/config.toml` under `board_file_lock`; fires once per
drained `config_set` op (not per process, like `env_deprecated`),
payload `path` (full dotted config path — `core.<field>` or
`components.<name>.<key>`) + `value` (resolved value AFTER coercion
against the schema's declared type, so a `bool` knob set to `"1"`
lands as `true` here, not the raw string)),
`state_commit_error`, `rollback_error`, `web_error`,
`pipeline_pending_sweep_error`, `operator_queue_error` /
`operator_queue_drain_error`, `auto_diagnose_error` /
`auto_diagnose_post_error` / `auto_diagnose_no_destination`,
`notification_error` (TB-389 — the communication component's outbound
tick failed to post a queued notification to a channel; the
notification stays pending for the next tick's retry),
`communication_error` (TB-389 — the `Phase.COMMUNICATION` tick walk in
`daemon._tick` raised),
`classify_record_missing` / `classify_record_unreadable` (TB-194/195
post-task classify routine couldn't find or read its record).

**State / observability.** `task_implicit_commit` (HEAD-salvage),
`task_pipeline_pending` (TB-115 launching task parked while pipelines
run), `task_unfrozen`, `task_deleted` (TB-138 `ap2 delete`),
`task_updated` (TB-141 queue-routed update), `task_classified` (TB-194
post-task auto-classifier verdict), `backlog_auto_promoted`,
`cron_proposed`, `cron_proposal_error`, `pipeline_start`,
`orphan_recovery`, `board_malformed_line`, `mattermost`,
`mattermost_reply` (handler emitted a reply), `auto_diagnose_fired`,
`notification_delivered` (TB-389 — the communication component's
outbound tick delivered a queued notification to a channel),
`janitor_finding` (TB-178 chore-judge surfaced a candidate), `goal_updated`
(TB-189 operator-queued `update_goal` op landed), `pending_review_reminder`
(TB-184 unadopted cron-proposal nudge), `operator_ack` (TB-141
`@claude-bot ack: …`), `operator_queue_append` /
`operator_queue_drained`, `ideation_approved` (TB-121 operator
`ap2 approve TB-N` promoted a proposed task), `ideation_proposal_recorded`
/ `ideation_proposal_reconciled` (TB-188 per-proposal audit trail),
`auto_approved` (TB-223 — ideation omitted the `@blocked:review` codespan
on a proposed task because `AP2_AUTO_APPROVE` is on and the task carries
no `AP2_AUTO_APPROVE_GATE_TAGS` tag; `knob=` payload field captures the
env value at proposal time so the forensic trail survives env changes
during the daemon's lifetime), `would_auto_approve` (TB-232 monitor-only
dry-run sibling — fires at proposal time when both `AP2_AUTO_APPROVE=1`
AND `AP2_AUTO_APPROVE_DRY_RUN=1` and the tags gate would have stripped
`@blocked:review`; payload `task`, `knob`, `dry_run=true`; the codespan
is preserved so operator-manual `ap2 approve` is still required),
`auto_approve_paused` (TB-223 —
cumulative-regression circuit-breaker tripped; auto-promote of
auto-approved tasks halted until operator emits `ap2 ack
auto_approve_unfreeze`), `auto_unfreeze_applied` (TB-225 —
agent-diagnosed briefing-shape fix from a `BriefingFix:` prefix was
auto-applied to a Frozen task; payload `task`, `shape`, `from`, `to`),
`auto_unfreeze_skipped` (TB-225 — auto-unfreeze attempt refused at
one of the layered guards; payload `task` + `reason` token, where
reason is one of `shape_not_in_allowlist`, `briefing_mismatch`,
`briefing_path_missing`, `per_task_cap`, `per_day_cap`, `queue_error`,
`sweep_error`), `would_auto_unfreeze` (TB-233 monitor-only dry-run
sibling of `auto_unfreeze_applied` — fires when both
`AP2_AUTO_UNFREEZE_FIX_SHAPES` and `AP2_AUTO_UNFREEZE_DRY_RUN=1` are
set and the full guard chain would have passed; payload `task`,
`shape`, `file`, `line`, `from`, `to`; the briefing file is NOT
mutated and no operator-queue ops are appended).

**Real-SDK smoke check (TB-350).** `smoke_check_skipped`,
`smoke_check_passed`, and `smoke_check_failed` are the three outcome
events of the 6-hourly `real-sdk-smoke` cron job, dispatched by
`daemon.run_cron` through the `ap2.smoke_runner.run_smoke_check`
routine (a deterministic subprocess pass, NOT an LLM agent — control /
cron agents have no Bash). `smoke_check_skipped` (payload
`reason="AP2_REAL_SDK unset"`) fires when the inert-by-default gate
trips — the routine never runs paid live-API calls unless `AP2_REAL_SDK`
is set to a non-falsey value, so the shipped `cron.default.yaml` job is
a one-event no-op on installs that haven't opted in. When the flag IS
set the routine runs `uv run --extra dev pytest -q ap2/tests/smoke/` as
a subprocess bounded by `AP2_VERIFY_TIMEOUT_S`, emitting
`smoke_check_passed` (payload `duration_s`) on exit 0 or
`smoke_check_failed` (payload `reason` ∈ {`nonzero_exit`, `timeout`},
`exit_code`, `duration_s`, `failure_tail` — the last ~2000 chars of the
pytest output, which carries the failing `FAILED <nodeid>` lines) on
non-zero exit / timeout. Failure-only alerting: the routine posts a
concise Mattermost alert (via the shared `registry.channel_adapters`
delivery path, channel resolved like the status-report routine —
`AP2_MM_REPORT_CHANNEL` then `AP2_MM_CHANNELS[0]`) ONLY on
`smoke_check_failed`; the pass record lives in `events.jsonl` and a 6h
"smokes OK" post would be noise alongside the 8h status-report digest.
This restores the live-API SDK-wiring canary (cron_propose /
pipeline_task_start / report_result / prose-judge / validator-judge
round-trips) that the per-task verification gate dropped on 2026-05-30
to stop transient-blip false-fails — out-of-band and deterministic
instead of on every task.

**Codex coverage guard (TB-375).** `smoke_check_codex_coverage_missing`
is a DISTINCT failure event the same `run_smoke_check` routine emits
(instead of `smoke_check_passed`) when codex was EXPECTED to run but a
codex-parametrized smoke variant nonetheless skipped — "green-by-skipping",
the exact signal that hid the phantom-SDK bug for weeks. A session-scoped
guard in the smoke harness (`ap2/tests/smoke/conftest.py` →
`ap2/tests/smoke/_codex_guard.py`) defines "codex expected" as the
conjunction of three presence signals — `AP2_REAL_SDK` set, `openai_codex`
importable, AND a codex credential present (reusing the daemon-start auth
gate's `_codex_credentials_present` helper — `OPENAI_API_KEY` or a
`$CODEX_HOME`/`~/.codex/auth.json` ChatGPT-login session, presence-only,
no token contents read). Under that condition a single skipped codex
variant forces a non-zero pytest exit and prints a stdout sentinel;
`run_smoke_check` greps the sentinel and emits
`smoke_check_codex_coverage_missing` (payload `reason="codex_expected_but_skipped"`,
`exit_code`, `duration_s`, `skipped_coverage` naming the skipped variants,
`failure_tail`) plus the same failure-only Mattermost alert — NEVER a
pass. When codex is legitimately absent (SDK not installed or no codex
credential) the guard stays quiet and a Claude-only box still passes; the
per-test `call_with_transient_retry` skip semantics are unchanged (this
guard operates at the run level).

**Briefing-validator LLM judge (TB-235).** `validator_judge_timeout`
and `validator_judge_fail` are fail-open audit events from check #7
in `briefing_validators._validate_briefing_structure` (LLM-driven
dependency-coherence judge; TB-262 split this out of `tools.py`). They fire when the Haiku-4.5 judge call exceeds
`AP2_VALIDATOR_JUDGE_TIMEOUT_S` (default 60s; TB-269 calibration)
or fails for any other reason (network, parse error, model
unavailable). The validator's policy on judge failure is fail-open
— refusing to gate `ap2 add` / `ap2 update` on a transient Anthropic
API hiccup is the load-bearing trade-off — so each skipped call
lands as an event for operator visibility. Payload:
`validator_judge_timeout` carries `timeout_s` + `error`;
`validator_judge_fail` carries `error` (the exception repr or
`"non-dict judge response"`). When `AP2_VALIDATOR_JUDGE_DISABLED=1`
is set, the check is skipped entirely and neither event fires
(clean bypass, not a fail-open).

`validator_judge_passed` (TB-269) is the successful sibling: emitted
when the SDK worker returns without timeout / SDK exception, BEFORE
the JSON parse, so the wall-clock distribution feeds the doctor's
`validator_judge_timeout_audit` surface (axis-1 mirror of TB-252's
`verify_timeout_audit`) regardless of whether the response parsed
cleanly. Payload: `duration_s`, `briefing_bytes`, `max_turns`,
`timeout_s`. Completes the happy-path / fail-open / timeout
triangle on a single event namespace.

TB-243 surfaces the rolling 24h counts of both event types on
`ap2 status` (text: a `validator-judge: N fail | M timeout (24h)`
sub-line under the `auto-approve:` block, omitted when both counts
are zero; JSON: a nested `auto_approve.validator_judge.{fail_count_24h,
timeout_count_24h}` object, always present) and on the web home
Automation card (a "Validator judge (24h)" row, omitted when both
counts are zero, warn-tinted amber when
`(fail + timeout) >= AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD`, default
5). Closes the silent-degradation hazard the fail-open design
otherwise left for an operator with `AP2_AUTO_APPROVE=1`: 10
silently-timed-out judge calls used to take up to a full
status-report cron tick to surface — now they appear on the
on-demand pull surfaces immediately.

TB-245 closes the push-surface half of the same observability gap:
the status-report Mattermost cron post (operator's primary
walk-away channel) now also carries a
`*Validator-judge fail-open window (24h):*` sub-block listing the
same two 24h counts, with the same `[noisy]` suffix when
`(fail + timeout) >= AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD` (default
5). Window is identical to TB-243's pull-surface 24h so the
operator never has to reconcile two different validator-judge
counts between `ap2 status` and the cron post. Sub-block is
omitted when both counts are zero (quiet windows stay
byte-identical to the pre-TB-245 baseline); both event types are
also listed in `_STATUS_REPORT_AUTOMATION_INTERESTING_TYPES` in
`ap2/status_report.py` so a lone fresh fail-open event keeps the
skip-gate from firing — operator never misses a degradation
signal because the status-report cron post coincided with an otherwise-quiet
window.

**Proactive attention surface (TB-282; TB-287, TB-288, TB-289, TB-290 extended).**
`attention_raised` is the distinct push-surface event for conditions
that warrant immediate operator attention. Detector inventory:
`task_stuck` (TB-282) flags an Active task whose most recent
`task_solve` (with a legacy `task_start` fallback, so pre-TB-385 runs
still in the tail are caught) is older than
`AP2_TASK_STUCK_THRESHOLD_S` — default 14400s / 4h — and has no
intervening terminal event;
`task_frozen` (TB-287) flags a Frozen task whose most recent
freeze-entry event (`retry_exhausted` / `task_failed`) is within
`AP2_TASK_FROZEN_RECENCY_S` — default 86400s / 24h — and has no
intervening operator-driven `task_unfrozen` / `task_deleted` event,
so a walk-away operator returning after a day sees an
`ap2 unfreeze` nudge per fresh freeze instead of just a `3F`
aggregate count tick; `validator_judge_noisy` (TB-288) flags when
the rolling 24h sum of `validator_judge_fail` +
`validator_judge_timeout` events is ≥
`AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD` (default 5) — singleton
project-wide condition (key `validator_judge_noisy`, NOT per-event)
that promotes the noisy state from the bottom-of-digest TB-245
sub-block and `[noisy]` suffix in `ap2 status` to a top-of-post
`## Attention needed` bullet, additive to (not a replacement for)
those existing pull-surfaces; `auto_approve_paused` (TB-289) flags
when `collect_auto_approve_state(cfg).pause_reason` is non-None
(today: `consecutive_freezes` / `validator_judge_noisy`; future:
`per_task_token_cap_exceeded` / `window_token_cap_exceeded` /
`task_error` from the TB-224 cost halts), keyed per-reason
(`auto_approve_paused:<reason>`) so a sequential reason transition
surfaces both bullets — closes Progress signal #3's "pending
decision" leg by promoting the pause state from the bottom-of-
digest TB-228 automation-digest sub-block + `ap2 status` line to a
top-of-post `## Attention needed` bullet naming the
`ap2 ack <verb>` resume nudge (verb resolves via
`_PAUSE_REASON_ACK_VERB` in `ap2/automation_status.py`);
`cost_cap_approach` (TB-290) is the pre-trip companion to the
post-trip `auto_approve_paused:window_token_cap_exceeded` surface —
singleton project-wide condition (key `cost_cap_approach:window`,
NOT per-task) that fires when the rolling 24h auto-approved
`task_run_usage` token sum is ≥
`AP2_AUTO_APPROVE_COST_APPROACH_PCT` (default 75) percent of
`AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP` AND strictly below the cap, so
the walk-away operator gets a budget-spending nudge hours before
dispatch halts and they must `ap2 ack auto_approve_window_resume`.
The walk reuses the same `_auto_approve_window_resume_idx` reset
+ `_auto_approved_task_ids` filter + 24h roll + `_event_combined_tokens`
sum the TB-224 trip-check in
`auto_approve._auto_approve_check_violations` uses, so the
approach-check sum is structurally guaranteed to match the
trip-check sum (no drift between predicting the pause and the
pause itself). Hands off explicitly above the cap so the operator
sees one bullet, not two; no-op when the cap is unset
(operator-opt-in, mirroring the TB-224 trip surface's
"operators who haven't budgeted their project don't get a
hardcoded cap surprising them" design). The
daemon's `_tick` calls
`ap2.attention.detect_attention_conditions(cfg)`, debounces each
candidate against any prior matching fire within
`AP2_ATTENTION_DEBOUNCE_S` (default 21600s / 6h), and emits one
`attention_raised` event per fresh condition. Per-(attention_type,
key) debounce so a second stuck/frozen task doesn't get suppressed
because a first one fired recently. Payload: `attention_type`
(detector kind — `task_stuck`, `task_frozen`,
`validator_judge_noisy`, `auto_approve_paused`, and
`cost_cap_approach` are the seeds today; future detectors land
alongside as `decisions_needed_new` / etc.), `key` (per-condition
dedup key — e.g. `task_stuck:TB-N` / `task_frozen:TB-N` for per-
task detectors, `validator_judge_noisy` (singleton) for the
noisy-window detector, `auto_approve_paused:<reason>` for the
per-reason pause detector, or `cost_cap_approach:window`
(singleton) for the window-cap-approach detector), `summary`
(operator-legible one-line string the status-report renderer
surfaces), plus a detector-specific extras blob (`task_stuck`
carries `task`, `title`, `age_s`, `start_ts`, `threshold_s`;
`task_frozen` carries `task`, `title`, `age_s`, `freeze_ts`,
`recency_s`; `validator_judge_noisy` carries `fail_count_24h`,
`timeout_count_24h`, `threshold`, `window_s`;
`auto_approve_paused` carries `pause_reason`, `ack_verb`,
`consecutive_freezes`, `validator_judge_fail_count_24h`,
`validator_judge_timeout_count_24h`; `cost_cap_approach` carries
`total_tokens_24h`, `window_cap`, `approach_pct`, `pct_used`,
`window_s`).
The
status-report cron's `render_attention_section` reads the still-
active conditions per tick and emits one bullet per condition under
a distinct `## Attention needed` section the agent forwards
VERBATIM into the Mattermost post — positioned BEFORE the routine
progress bullets so the walk-away operator sees the attention
signal first. Listed in both `IDEATION_RELEVANT_EVENT_TYPES`
(ideation reasons against fresh attention events next cycle) and
`_STATUS_REPORT_AUTOMATION_INTERESTING_TYPES` (a fresh fire un-
skips the dedup/idle gate, parallel to the TB-244 / TB-245
extension pattern).

The push-delivery leg of the same surface emits three audit events:
`attention_pushed` (an `attention_raised` condition was delivered to a
channel), `attention_push_error` (the underlying `_mm_post` failed —
the condition stays eligible for a later push), and
`attention_push_no_destination` (the immediate-push path found no
configured channel; sticky one-shot audit so a chronically-undestined
project doesn't spam the timeline).

**Focus state (TB-226 axis 4; collapsed TB-342).** `roadmap_complete`
tracks the daemon's ideation-exhaustion halt against goal.md's
`## Current focus:` headings. The operator manual's `Focus state` reference
carries the full design (which also covers the TB-342 collapse from the
pre-existing multi-focus rotation pointer walk into a single
exhaustion detector).

- `roadmap_complete` (TB-226; rescoped TB-275 / TB-340 / TB-342) —
  ideation produced zero proposals for `AP2_IDEATION_HALT_EMPTY_CYCLES`
  (default 3) consecutive cycles. The ideation TRIGGER parks
  (`_maybe_ideate` emits `ideation_skipped reason=roadmap_complete`)
  until the operator edits goal.md via `ap2 update-goal` (the drain
  handler emits `goal_updated`, which `reset_pointer_on_goal_updated`
  uses to clear `roadmap_complete_emitted` + the empty-cycles counter)
  or runs `ap2 ideate --force`. `ap2 ack roadmap_complete` does NOT
  resume — it only DISMISSES the recurring operator nag while
  ideation stays parked (TB-340). Task dispatch is NOT affected —
  already-queued Backlog tasks continue to drain. Use `ap2 pause` for
  an explicit full-stop. Payload: `exhausted_count` (current focus-list
  length), `trigger=empty_cycles_heuristic` (the sole trigger
  post-TB-342). Fired once per exhaustion episode (suppression via
  the pointer's `roadmap_complete_emitted` flag; the same emit clears
  the `roadmap_complete_ack_idx` dismissal marker so each fresh
  episode re-nags exactly once).

  (Historical note: pre-TB-342 the daemon also emitted
  `focus_advanced` events for the multi-focus rotation pointer walk
  and a `pointer_past_last` trigger value when the pointer crossed
  the final focus. TB-342 collapsed the rotation theatre — ideation
  never actually scoped itself to the "active" focus, so the walk
  changed nothing about what was proposed — into a single
  exhaustion detector that emits `roadmap_complete` directly when
  the empty-cycles threshold trips. The `focus_advanced` event is
  no longer emitted, the `pointer_past_last` trigger value is gone,
  and the operator-CLI `ap2 rewind-focus` recovery verb went away
  with the rotation. Multi-`## Current focus:` headings remain
  expressive prose/priority hints for the operator and ideation
  agent; the daemon just doesn't sequence them.)

**Additional audit events (enumerated for completeness).** These types
are emitted by ap2 source and carry their full semantics in the matching
operator-manual reference (auto-approve gate, auto-unfreeze sweep,
retrospective audit); they are listed here so the timeline enumeration
stays complete:
`auto_approve_halted` (auto-promote of auto-approved tasks halted by a
per-task or window token cap or a `task_error` cost halt; payload
`reason` ∈ {`per_task_cap`, `window_cap`, `task_error`} plus the
triggering counts; deduped once per triggering episode via a tail scan),
`auto_approve_skipped` (an auto-approval was suppressed because the
validator-judge fail-open window is noisy —
`reason=validator_judge_noisy`; fires once per episode),
`auto_unfreeze_disabled` (the briefing-shape auto-unfreeze sweep was
reached while disabled; one-shot per process, sticky dedup),
`validator_judge_deprecated_knob` (a deprecated validator-judge env knob
is still set; one-shot per process), and `task_audit_skipped` (the
post-task retrospective-audit routine was skipped for this task —
distinct from `classify`).

`diagnose.MEANINGFUL_EVENT_TYPES` is what the watchdog counts as "the
daemon making progress"; `FAILURE_EVENT_TYPES` is what counts as broken.

## Prose-judge diagnostics

The prose judge (`ap2/verify.py::_judge_prose_bullet`, dispatched as the
`verifier_judge` agent-kind) turns each `## Verification` prose bullet
into a pass / fail / unverified verdict. As of TB-385 it no longer emits
a per-bullet `judge_call` event — the per-bullet verdict is folded into
the daemon's single terminal `task_verify` event (`bullets[]`, one
`{idx, kind, verdict}` per bullet), so prose verdicts are read off
`task_verify`, not a stream of `judge_call` events. The `judge_call`
event itself still exists, but post-TB-385 it is emitted only by the
still-streaming judge kind — the janitor's per-finding judge (see the
Event schema section above) — so the `judge_call`-keyed queries below
apply to those calls and to pre-TB-385 history, NOT to current prose
verification bullets. TB-236's prose-judge prevention + parse-failure
observability survive the fold, just on different surfaces (the prompt
constraint is unchanged; the parse-failure detail now lives in the
on-disk dump rather than an event field), so silently-skipped prose
bullets under `AP2_AUTO_APPROVE=1` stay diagnosable:

- **Prompt constraint (prevention).** The judge prompt now caps the
  rationale at ≤200 characters and is explicit that the FINAL message
  must be a JSON object only (no markdown fences, no preamble, no
  trailing prose). Intermediate `Read` / `Grep` / `Glob` tool calls are
  unconstrained — only the last message is.
- **`response_length`** / **`rationale_length`** (length signals).
  `response_length` is the character length of the judge's final
  assistant text; `rationale_length` is the length of the extracted
  `rationale` field on a successful parse. They let operators watch the
  prompt-tightening effect / rationale-cap creep over time (if
  `rationale_length` drifts above ~200 over a week the prompt constraint
  is slipping). Pre-TB-385 these rode on the per-bullet `judge_call`
  event; post-TB-385 the prose judge emits no such event, so for prose
  verdicts the signal lives in the on-disk dump (below) rather than an
  event field.
- **`parse_error`** (parse-failure category). One of:
  - `no_json_object` — response had no `{` / `}` at all.
  - `trailing_prose_after_json` — `{...}` parses cleanly but non-
    whitespace follows the closing brace (judge added commentary).
  - `unescaped_in_string` — usually an unescaped `"` or `\` inside a
    string value.
  - `json_truncated` — response cut off mid-string-value.
  - `parse_error_other` — catch-all.
  The full enum lives in `ap2/verify.py::PARSE_ERROR_CATEGORIES`.
- **On-disk dump** (the surviving prose parse-failure surface). On a
  parse failure the verifier writes the FULL raw last-assistant-text to
  the per-bullet dump file at
  `.cc-autopilot/debug/<run_ts>-<task>-judge-bullet<idx>-response.txt` —
  not the 200-char preview the verifier keeps in the bullet's `notes`.
  Open it when you need to see what the judge actually emitted (unescaped
  backticks, prose preamble, etc.). Successful parses leave no dump on
  disk. Pre-TB-385 this path was also surfaced as a `judge_response_dump`
  field on the per-bullet `judge_call` event; post-TB-385 that event is
  gone for prose, so locate dumps by listing the debug dir directly.

Pattern-detection workflow. The `judge_call`-keyed queries below resolve
against pre-TB-385 history (where the prose judge still streamed a
per-bullet `judge_call` carrying `parse_error` / `judge_response_dump`);
for current prose parse failures, scan the debug dir directly instead.

```
ap2 events tail -n 500 | jq 'select(.type=="judge_call" and .parse_error)'
```

Counts by category, last 24h:

```
ap2 events tail -n 2000 | jq -r 'select(.type=="judge_call") | .parse_error // "ok"' | sort | uniq -c
```

Open the worst-offender dump:

```
ap2 events tail -n 500 | jq -r 'select(.type=="judge_call" and .judge_response_dump) | .judge_response_dump' | tail -1 | xargs cat
```

For current (post-TB-385) prose parse failures there is no `judge_call`
event to key on — list the surviving on-disk dumps in the debug dir:

```
ls -t .cc-autopilot/debug/*-judge-bullet*-response.txt | head
```

Failure recovery for prose-judge parse failures stays soft-pass:
`verification_partial` → Complete (per the existing aggregator). The
fields above don't change that policy — they just make the partials
diagnosable rather than silent. If a single category dominates (e.g.
`unescaped_in_string` >50% of failures over a week), the appropriate
follow-up is a TB to either tighten the prompt further or harden the
parser — informed by the dump files instead of guessing.

## Live event tail — `ap2 logs`

`ap2 logs --json -n 30 | jq` works if the CLI is on PATH; defaults
truncate to 120 chars per field, `--json` gives full payloads.

**Live event tail — `ap2 logs --follow`.** Live-tails
`.cc-autopilot/events.jsonl` (project-aware via the global `--project`)
and emits one compact line per arc-relevant event from a curated
allowlist (ideation lifecycle, validation + queue, task lifecycle,
focus + attention + watchdog + daemon). Complements one-shot `ap2 logs
-n` (the static tail) and `ap2 status` (the periodic snapshot): reach
for it when you want to watch an active arc unfold live —
task-dispatch sequences, ideation cycles, focus advances, attention
conditions — without manually grepping `events.jsonl` or re-running
`ap2 logs -n` in a loop. Usage:

```
# From the project root (allowlist-filtered, compact format):
ap2 logs --follow

# Explicit project root:
ap2 --project /path/to/project logs --follow

# Disable the allowlist (stream every event type — debug escape hatch):
ap2 logs --follow --all

# Raw JSON line per kept event (compose with --all for an unfiltered raw stream):
ap2 logs --follow --json
```

Each kept line has the shape
`HH:MM:SS | <event_type> | key=val ... | summary=<truncated>`. The
allowlist + compact formatter + `tail -F` follow loop live in
`ap2/event_monitor.py` (TB-352 folded in the former loose
`scripts/monitor_events.py`); the `KEEP` set there widens or narrows
coverage. The default is intentionally noisy-filtered for arc tracking,
not exhaustive event logging (one-shot `ap2 logs` covers that).

`scripts/monitor_events.py` is retained as a thin shim that delegates to
`ap2.event_monitor` so an existing Claude Code `Monitor` watch on
`python3 -u scripts/monitor_events.py` keeps working unchanged — repoint
it at `ap2 logs --follow` whenever convenient.

## Stats dashboard

The `/stats` page (HTML, server-rendered, no JS) and `/stats.json`
endpoint (JSON, scripting-friendly) surface trend aggregates over
an operator-configurable window — the return-and-review surface for
multi-day walk-away cycles. URLs:

- `http://127.0.0.1:8730/stats` — human-readable dashboard.
- `http://127.0.0.1:8730/stats.json` — machine-readable contract.

`?window=` accepts `1d` / `7d` (default) / `30d`, plus arbitrary
`Nh` / `Nm` / `Nd` suffixes. Values are clamped to `[1h, 90d]` so a
typo doesn't either flood the events.jsonl scan or render an empty
page.

Metrics surfaced:

| Section | Metric |
|---|---|
| Tasks | total count, completion rate, avg/p50/p95 duration + num_turns, total + avg cost, top-10 longest, top-10 most expensive, duration-bucket histogram (≤1m / 1–5m / 5–15m / 15–30m / 30–60m / >60m), attempts-per-task histogram (1st-try / 2nd / 3rd / retry-exhausted), frozen rate |
| Per-bullet verifier | total prose-judge call count, avg/p50/p95 duration, top-10 slowest, validator-judge fail + timeout counts (window-bounded — `automation_status`'s `_24h` counters are the 24h-only sibling) |
| Ideation | cycle count, avg/p50/p95 duration + turns + cost, proposals recorded, proposals/cycle, rejection rate |
| Cron | per-job cycle count + avg duration + avg cost (auto-discovered by `control_run_usage label=cron-*`) |

**What to look for during walk-away review**: rising avg cost or
p95 duration relative to a prior week is the silent-overhead-creep
signal TB-235 (the LLM-judge regression that quintupled test-suite
runtime; see `.cc-autopilot/insights/test-suite-slowness-2026-05-17.md`)
would have surfaced earlier. Climbing frozen-rate or
validator-judge-fail counts indicate gate erosion. Climbing top-10-
most-expensive against a fairly stable top-10-longest indicates
silent token spend per turn — likely a model regression or prompt
bloat.

The JSON contract is the stable interface; HTML layout can change
without breaking scripted consumers. Top-level shape:

```json
{
  "window": "7d",
  "window_s": 604800,
  "computed_at": "2026-05-18T16:42:00Z",
  "tasks":    {...},
  "verifier": {...},
  "ideation": {...},
  "cron":     {...}
}
```

**Status-report push surface (TB-259).** The status-report
Mattermost cron post (operator's primary walk-away channel) also
carries a top-line digest of the same aggregates as a
`*Stats window aggregates (<window>):*` sub-block — three bullets
summarizing task completions (with p50/p95 duration), ideation
cycles + proposals, and bullet-judge evaluations + fail-open count
over the inter-report window. Closes the push-vs-pull parity gap
TB-255 left open: the dashboard pays rent only during active
operator sessions, but the walk-away promise (goal.md L28-30
"walk away for a week without intervention") needs the digest to
land without the operator opening a browser tab. Window is scoped
to "now - last status-report cron_complete ts" so the sub-block
matches the inter-report window the TB-228 / TB-244 / TB-245 /
TB-258 sub-blocks above it scope against; falls back to 24h when
no parseable previous-report ts exists (first-ever run, or the
previous one rolled out of the tail). Omit-on-empty: the sub-block
is suppressed when the window's task-completion count is zero, so
quiet windows stay byte-identical to the pre-TB-259 baseline and
the `/stats` pull surface still renders the full zero-state
dashboard for operators who load it directly. Mirrors the
wrap-helper-into-state-extras pattern shipped across prior
axis-parity tasks (TB-241 / TB-242 / TB-244 / TB-245 / TB-258).
Pure read-layer composition over the existing `collect_stats`
helper — no new aggregates, no new state file, no daemon-side
changes, no new env knobs.

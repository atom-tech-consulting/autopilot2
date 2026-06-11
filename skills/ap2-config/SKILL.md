---
name: ap2-config
description: "Use when configuring an ap2 daemon — discovering or tuning an `AP2_*` environment knob, authoring `.cc-autopilot/config.toml` keys (`[core]` / `[components.*]` / `[agent_backends]`), or setting up the Codex agent backend."
---

# ap2 configuration — env knobs, config.toml keys & backend setup

The operator-facing configuration reference for an ap2 daemon. Discover or
tune any tunable here — an operator should never have to grep `ap2/howto.md`
for a knob. Two parallel surfaces describe the same tunable set:

- **Configuration knobs** — the full flat `AP2_*` environment-variable
  catalogue (loop cadence + per-run timeouts, agent model/effort,
  verification, the briefing validator, the auto-approve / auto-unfreeze
  gates, ideation, watchdog, attention, janitor, channel adapters,
  Mattermost, the local web UI), the per-agent-kind backend selector, and
  the Codex backend install + auth setup.
- **Config keys (TOML)** — the typed `.cc-autopilot/config.toml` surface the
  structured-config focus is migrating the flat env set onto (`[core.*]`,
  `[components.<name>.*]`, `[agent_backends]`).

Two docs-drift gates in `ap2/tests/test_docs_drift.py` keep this skill in
lock-step with the source: `test_every_env_knob_documented` (every `AP2_*`
knob read in `ap2/*.py` carries a backtick-fenced mention here) and
`test_every_config_key_documented` (every `ConfigKey` declared on a
component or core schema is documented here). A source-side knob / key
addition that skips this skill trips one of those gates until docs catch
up.

## Configuration knobs

Set in shell, in `<project>/.cc-autopilot/env`, or in
`~claude-agent/.zshenv`. The full set the ap2 source consults
(`grep -nE 'AP2_[A-Z_]+' ap2/*.py` is the source-of-truth — the
`test_every_env_knob_documented` gate in `ap2/tests/test_docs_drift.py`
fails CI if a new knob is added and not listed here).

**12-factor exempt set + CI gate (TB-338).** The subset of knobs that
NEVER migrate to TOML — Mattermost auth / channel identity, integration
secrets (`AP2_WEBHOOK_URL`), deployment-environment paths
(`AP2_CHANNEL_FILE_PATH`), sandbox-identity placeholders (`AP2_DIR`,
`AP2_REAL_SDK`) — is enumerated in
`ap2/config_compat.py::_KNOBS_STAYING_ENV_ONLY`. The
`test_tb338_env_only_cut_line` gate enforces this cut-line on every
PR: any new `os.environ.get("AP2_…")` read added outside the exempt
set + the `ap2/config.py` / `ap2/env_reload.py` bootstrap path fails
CI until the author migrates via `cfg.get_*_value` or explicitly
documents the new knob in `_KNOBS_STAYING_ENV_ONLY` with a one-line
justification:

**Hot-reload vs restart (TB-271).** Most tunable knobs (timeouts,
max-turns, model/effort, auto-approve / auto-unfreeze thresholds,
verify gate, tick intervals, ideation knobs, watchdog thresholds)
hot-reload — the daemon re-sources `.cc-autopilot/env` at the top of
every `_tick`, refreshes the tunable `Config` fields in-place, and
overwrites `os.environ` for file-sourced keys. A bumped knob takes
effect on the next tick (≤30s) without `ap2 stop && ap2 start`. The
canonical set is `env_reload.HOT_RELOADABLE_KNOBS`; the reload emits
an `env_reloaded` event with the changed keys for the audit trail.
TB-323 extended the watcher to `.cc-autopilot/config.toml` as well — a
bumped mtime on EITHER file triggers the next-tick HOT_RELOADABLE-
filtered refresh, so an operator editing the TOML to bump a tunable
gets the same propagation an env-file edit enjoys. (The TOML values
themselves are not re-parsed by the reload helper — `os.environ` is
the authoritative source for the refresh pass; the structured-config
layer's env-override / back-compat shim already wrote there at
daemon-start.)
A small fixed-knob set (`env_reload.FIXED_KNOBS` — `AP2_WEB_PORT`,
`AP2_WEB_DISABLED`, `AP2_MM_CHANNELS`) still requires a restart:
each configures a stateful resource (a bound HTTP socket, a
subscribed MM channel set) wired up once at daemon-start and not
re-applied by the reload. TB-260's `WARN: .cc-autopilot/env modified
... ap2 stop && ap2 start` line persists for the fixed-knob set and
clears automatically after a hot-reload that only touched
hot-reloadable knobs. "Shell export wins" still holds for keys
never sourced from the file: a `export AP2_FOO=bar` in the
operator's shell takes precedence over a `AP2_FOO=baz` later added
to the file, even on reload (you'd need to either un-export and
restart, or set the value via the file before daemon-start).

**Loop cadence + per-run timeouts.**
- `AP2_TICK_S` (30) — main-loop tick interval.
- `AP2_MM_TICK_S` (10) — Mattermost polling tick interval (separate
  loop, TB-122).
- `AP2_TASK_TIMEOUT_S` (1200) — per-task SDK query timeout.
- `AP2_TASK_MAX_TURNS` (200) — max turns per task agent (raised from
  50 in TB-278 after TB-122 hit `error_max_turns` at 51 turns; this
  project's own env bumps further to 500 for heavy refactors).
- `AP2_CONTROL_TIMEOUT_S` (1200) — per-control-agent timeout (cron,
  ideation, MM handler). Raised from 300s in TB-278 — `xhigh`-effort
  ideation routinely blew the old 5-min wall.
- `AP2_CONTROL_MAX_TURNS` (15) — max turns per control agent (cron
  + MM handler share this default; ideation has its own).
- `AP2_IDEATION_MAX_TURNS` (100) — max turns for the ideation agent
  (raised from 30 in TB-278 after a goal.md rewrite mid-cycle hit
  `error_max_turns` at 31 turns; ideation's Step 0 / 0.5 / 1.5 chain
  runs deeper than other control jobs).
- `AP2_MAX_RETRIES` (3) — failed-task retries before Frozen.
- `AP2_EVENT_CONTEXT` (50) — count of recent events inlined into agent
  prompts.

**Agent model + effort.** Per-run knobs that override the per-job default.
- `AP2_AGENT_MODEL` (`claude-opus-4-7`) — model for task agents and
  the SDK-judge plumbing (verifier, janitor).
- `AP2_AGENT_EFFORT` (`xhigh`) — global effort level. Each
  sub-job has its own override that falls back here:
  `AP2_STATUS_REPORT_EFFORT`, `AP2_VERIFY_JUDGE_EFFORT`,
  `AP2_JANITOR_JUDGE_EFFORT`.
- `AP2_THINKING_BLOCK_EFFORT_DROP_DISABLED` (unset → enabled) — kill
  switch for the TB-356 graceful-degradation path. By default, when a
  task run fails with the bundled-CLI thinking-block-immutability 400
  (`... thinking or redacted_thinking blocks in the latest assistant
  message cannot be modified`, surfaced as a generic `task_error`), the
  daemon steps THAT task's effort down one tier on the automatic retry
  (`xhigh`→`high`→`medium`→`low`, floored at `low`) and emits an
  `effort_downshift` event. The first attempt always runs at full
  `AP2_AGENT_EFFORT`; only this specific 400 triggers a one-tier drop
  per occurrence (other failure classes retry at unchanged effort). Set
  to `1` / `true` / `yes` / `on` to disable — constant effort, blind
  retry as before.
- `AP2_VERIFY_JUDGE_MAX_TURNS` (20), `AP2_JANITOR_JUDGE_MAX_TURNS` (12)
  — max turns for the per-bullet prose-judge and the janitor chore-judge.

**Verification.**
- `AP2_VERIFY_CMD` — project-wide regression gate (e.g.
  `uv run pytest -q`). Unset = no project-wide gate.
- `AP2_VERIFY_JUDGE_DISABLED` — hard off-switch for the per-task
  verifier's optional LLM prose-bullet judge. TB-382 had modeled the
  prose judge as a `verifier_judge` component; TB-386 demoted it back
  into the core verify runner (a judge invoked only as an internal
  sub-step of `verify_task` is not a loop-level participant), so this is
  now a plain config knob read directly by `verify.py::verify_task` via
  `os.environ.get`. When set to a truthy value (`1` / `true` / `yes`),
  `verify_task` skips the SDK judge and prose bullets record as
  `unverified` (soft, non-gating) while the deterministic shell bullets
  still gate — so a deployment can verify with shell bullets alone. Prose
  judging is on by default; mirrors `AP2_VALIDATOR_JUDGE_DISABLED`.
- `AP2_VERIFY_TIMEOUT_S` (600) — timeout for the project-wide gate.
  `ap2 doctor` warns when set below observed-typical successful verify
  duration (TB-252; reads `verify_passed` events for the last 7 days
  or 20 samples, whichever is larger; uses `max()` of durations so the
  worst-case successful run sizes the recommendation).

**Briefing validator (LLM-judge dependency coherence, TB-235).** Check
#7 in `ap2/briefing_validators.py::_validate_briefing_structure` (TB-262
split out of `ap2/tools.py`) runs a Haiku-4.5
judge over a freshly-authored briefing AFTER the six deterministic
checks (TB-154 canonical sections, TB-91/TB-102 parseable Verification,
≥1 bullet, TB-161 goal-anchor, TB-164 Why-now, TB-171 no-Manual)
pass. The judge identifies "hard predecessors" the briefing's prose
names implicitly (e.g. "ap2/_shared.py must already exist — created
by the _locked extraction") and the validator rejects when any judge-
named TB-N is missing from the task's `@blocked:` codespan. Closes
the dependency-coherence hole that under `AP2_AUTO_APPROVE=1`
(TB-223) would let ideation auto-promote a task out of dispatch
order — TB-220's prose vs codespan mismatch is the canonical
historical instance. Fail-open by design: on judge timeout / parse
failure / SDK error the validator logs a `validator_judge_timeout`
or `validator_judge_fail` event and lets the briefing through (the
cron status-report surfaces a climbing skip rate so operators
notice). The check fires on both `do_operator_queue_append`
(primary surface — ideation, MM handler, operator CLI all hit it)
and `do_board_edit` (legacy direct-board-mutation path) for shape
symmetry.

- `AP2_VALIDATOR_JUDGE_DISABLED` — hard off-switch. When set to a
  truthy value (`1` / `true` / `yes`), check #7 is bypassed
  entirely and the validator falls back to the six deterministic
  checks. Operator escape hatch if the judge is causing false-
  positives during a specific workflow; the deterministic gates
  still fire so the briefing-shape contract is preserved.
- `AP2_VALIDATOR_JUDGE_TIMEOUT_S` (default 60) — wall-clock timeout
  for the per-briefing judge call. Exceeded → log
  `validator_judge_timeout` event + skip the check. TB-269 bumped the
  default from 15 → 60 after the TB-257 investigation artifact
  (`.cc-autopilot/insights/validator-judge-timeout-2026-05-18.md`)
  measured the real SDK call at 17.6-46.8s wall-clock — the previous
  20s ceiling (15s default + 5s outer-thread grace) sat below the
  median completion of even the smallest measured briefing, so the
  axis-1 dep-coherence gate was silently fail-open on essentially
  every operator queue-append. The doctor surface
  `validator_judge_timeout_audit` in `ap2/doctor.py` (TB-269; axis-1
  mirror of TB-252's `verify_timeout_audit`) closes the calibration-
  drift loop — it reads `validator_judge_passed` events from
  `.cc-autopilot/events.jsonl` and surfaces a WARN with a one-line
  fix recommendation if a future workload shift takes the observed-
  typical successful call duration back above the configured floor.
  TB-270 ships the complementary axis-1 lever the same artifact named
  as the secondary factor (`prompt-too-heavy`):
  `_slice_briefing_for_dep_judge(briefing_text)` in
  `ap2/briefing_validators.py` (the dep-coherence judge lived in the
  flat module `ap2/validator_judge.py` until TB-316 moved it to a
  `validator_judge` component, then TB-386 demoted it back into the core
  briefing-validation runner) narrows the user payload's
  `briefing_markdown` field to the briefing's `## Goal` + `## Scope`
  sections only (Design / Verification / Out-of-scope are bytes the
  judge wouldn't have used to change its hard-predecessor verdict).
  Shrinks typical input from ~6KB → ~1-2KB and the SDK wall-clock
  proportionally — independent of the timeout knob, so the two
  levers compound. Defensive fallback in the helper returns the full
  `briefing_text` on briefings missing either canonical heading or
  with empty section bodies, guaranteeing the judge is never blind
  on legacy / hand-edited shapes.
- `AP2_VALIDATOR_JUDGE_MAX_TURNS` (default 2) — TB-249 canonical
  budget knob. Bounds the judge's SDK turn count. The validator is a
  single-shot JSON-emitting judge: one assistant message (the verdict)
  + one optional tool call (Read/Grep) is plenty; `2` keeps the call
  bounded and the cost ≤$0.005 at Haiku rates. Mirrors the
  `AP2_VERIFY_JUDGE_MAX_TURNS` / `AP2_JANITOR_JUDGE_MAX_TURNS` knob
  pattern (the SDK's native budget primitive).
- `AP2_VALIDATOR_JUDGE_MAX_TOKENS` — **deprecated** alias kept for
  one-cycle backward compatibility (TB-249). If set AND
  `AP2_VALIDATOR_JUDGE_MAX_TURNS` is unset, the value is reused as
  `max_turns`, ceiling-capped at 5 (so a stale `500` from the pre-
  TB-249 default doesn't translate into a 500-turn runaway). Emits a
  one-shot-per-process `validator_judge_deprecated_knob` event the
  first time the alias resolves; a future TB removes the alias once
  operator engagement confirms no env files still carry it. Migration:
  rename to `AP2_VALIDATOR_JUDGE_MAX_TURNS` with a value in `[1, 5]`
  (default `2`) — the old `500` (output-token cap) translates to a
  turn budget of `5` after the cap.
- `AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD` (default 5) — TB-243 surface
  threshold. When the rolling 24h sum
  `validator_judge_fail_count_24h + validator_judge_timeout_count_24h`
  is ≥ this number, `ap2 status` appends ` [noisy]` to its
  `validator-judge:` sub-line and the web home Automation card's
  "Validator judge (24h)" row gets the warn-tint (amber). Below the
  threshold both surfaces stay in the neutral palette so a single
  transient SDK blip doesn't tint the card. Closes the silent-
  degradation hazard left by the fail-open design above: an
  operator with `AP2_AUTO_APPROVE=1` whose judge has been quietly
  timing out for the last N briefings sees the warn-tint before the
  next audit. Unset / empty / non-int / non-positive → default
  (matches the TB-224 / TB-234 token-cap parse semantics). TB-272
  promotes the same threshold to a load-bearing safety floor: the
  auto-approve dispatch path now pauses (emits
  `auto_approve_skipped reason=validator_judge_noisy`) when the
  rolling-24h sum crosses this threshold — see
  `AP2_AUTO_APPROVE_NOISY_PAUSE_DISABLED` below for the opt-out.
- `AP2_AUTO_APPROVE_NOISY_PAUSE_DISABLED` — TB-272 opt-out for the
  validator-judge noisy-state auto-approve pause. **Unset by default
  → pause ACTIVE** (the safety-floor closure for the axis-1+3
  cross-cut hazard goal.md L82-88 names: the TB-235 dep-coherence
  judge that the auto-approve safety claim depends on can silently
  fail-open at high rate while `AP2_AUTO_APPROVE=1` continues
  stripping `@blocked:review` and dispatching ideation proposals).
  When the rolling 24h sum
  `validator_judge_fail_count_24h + validator_judge_timeout_count_24h`
  crosses `AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD` (default 5; TB-243),
  the daemon refuses to auto-promote `auto_approved` Backlog tasks
  and emits `auto_approve_skipped reason=validator_judge_noisy
  fail_count_24h=<N> timeout_count_24h=<M> threshold=<T>` per
  preempted promotion attempt. The pause-reason discriminator
  surfaces as `validator_judge_noisy` on the existing TB-227 `ap2
  status` text/JSON + web home Automation card + TB-228 cron
  status-report digest renderers (no new operator-facing surfaces).
  Resume: the rolling-24h window self-clears as old events age out,
  OR the operator runs `ap2 ack auto_approve_unfreeze` (same verb
  `consecutive_freezes` uses — no new ack token), OR they set this
  knob to a truthy value (`1` / `true` / `yes`, matching the
  sibling auto-approve knobs' parse). Set the knob when you
  explicitly trust the upstream judge degradation surface and want
  the pre-TB-272 cosmetic-only TB-243 behavior — the `[noisy]`
  badge stays on `ap2 status` / web home but the dispatch path
  isn't gated on it. Hot-reloadable (TB-271) so the operator can
  flip it without a daemon restart.

**Ideation.**
- `AP2_IDEATION_DISABLED` — set to `1`/`true` to opt out of empty-board
  ideation entirely.
- `AP2_IDEATION_COOLDOWN_S` (7200) — minimum gap between ideation runs.
- `AP2_IDEATION_TRIGGER_TASK_COUNT` (3) — fire ideation when Ready+Backlog
  count is BELOW this threshold (Active is still a hard gate). Set to
  `1` for the legacy "fire only when the working queue is fully empty"
  behavior; raise it (e.g. `5`) for projects with very fluid scope.
  Invalid (non-int, non-positive) values fall back to the default.
- `AP2_IDEATION_SCRUB_MODEL` (default `claude-haiku-4-5-20251001`) —
  TB-284: model for the post-write scrub that strips exhaustion-
  asserting sentences ("this focus is essentially done", "once Y ships
  nothing remains") from `ideation_state.md` after each ideation cycle.
  The scrub keeps verdict language from priming the next cycle to
  repeat the verdict. Haiku-4.5 is the cost-target floor since the
  task is sentence-level classification, not deep reasoning; operators
  can swap models for cost / quality trade-offs without a daemon
  restart (knob is hot-reloadable). On any SDK error the scrub
  fail-opens and leaves the file unchanged — structure (axis
  breadcrumbs, proposed-task lists) is more valuable to keep than
  verdict sentences are to remove on any single cycle. See
  `ap2/ideation_scrub.py` for the prompt contract.

**Operator-in-the-loop relaxations (TB-223).** Three layered safety
knobs that let an operator who trusts the upstream gates dispatch
ideation-proposed tasks without running `ap2 approve` on each one.
Defaults are unset / conservative — current behavior is preserved for
operators who haven't opted in. Cross-references `goal.md`'s
**Current focus: end-to-end automation** axis on the manual-approval
bottleneck: a representative ap2 session approves 10-20 tasks per
cycle, which contradicts the Mission's "walk away for a week without
intervention" promise. The trio is layered so an operator can dial
trust precisely: `AP2_AUTO_APPROVE` is the master switch,
`AP2_AUTO_APPROVE_GATE_TAGS` is the per-shape opt-out (operator names
tag categories that retain manual review even in auto-approve mode),
and `AP2_AUTO_APPROVE_FREEZE_THRESHOLD` is the systemic-regression
circuit-breaker (auto-promote halts when consecutive task failures
land in Frozen).

- `AP2_AUTO_APPROVE` — master switch. **Unset by default.** When set
  to a truthy value (`1` / `true` / `yes`, matching
  `AP2_IDEATION_DISABLED`'s convention), ideation-authored
  `add_backlog` rows omit the `@blocked:review` codespan so the
  daemon's next-tick auto-promote dispatches the task immediately. The
  operator decision-log entry in `ap2 logs` still surfaces what
  auto-approval shipped (the `auto_approved` event — see the
  **ap2-observability** skill's `## Event schema`), so the audit trail is
  preserved for offline review.
  Off-by-default keeps the legacy approve-every-task behavior in place
  for operators who haven't verified the upstream gates (briefing
  structural validation, goal-alignment validation, per-task
  verification, retry budget, rollback).
- `AP2_AUTO_APPROVE_DRY_RUN` — TB-232 monitor-only on-ramp. **Unset
  by default.** When set to a truthy value alongside
  `AP2_AUTO_APPROVE=1`, the auto-approve gate chain (tags +
  freeze-threshold + token caps) still runs but the WRITE step is a
  no-op on the board row: instead of stripping `@blocked:review` and
  emitting `auto_approved`, the daemon emits a `would_auto_approve`
  audit event (same `task` + `knob` payload, plus `dry_run=true`) and
  leaves the codespan intact for operator-manual `ap2 approve`. Use
  this to observe the loop's decisions without committing to the
  binary cliff. **Enablement on-ramp:** set both
  `AP2_AUTO_APPROVE=1` AND `AP2_AUTO_APPROVE_DRY_RUN=1`, leave the
  daemon running for ≥24h, read `ap2 status --json` and grep
  `events.jsonl` for `would_auto_approve` events to confirm the
  gate's decisions match your judgment, then unset
  `AP2_AUTO_APPROVE_DRY_RUN` (keep `AP2_AUTO_APPROVE=1`) to engage
  real dispatch. The `would_auto_approve_count_24h` field on
  `collect_auto_approve_state` (surfaced via `ap2 status` + web home)
  rises as decisions accumulate so you can confirm at a glance the
  gate is exercising decisions before flipping the switch. TB-238
  also surfaces the same count as a trailing `*Dry-run window:*`
  sub-block on the scheduled `status-report` Mattermost post's
  `## Automation loop activity` section, so a walk-away operator
  sees the readiness signal in their primary return surface
  without alt-tabbing to `ap2 status --json`.
- `AP2_AUTO_APPROVE_GATE_TAGS` (default `#breaking-change,#high-risk`)
  — comma-separated list of tag strings. When auto-approve is on, a
  proposed task carrying ANY of these tags **retains** its
  `@blocked:review` codespan so it still requires `ap2 approve`. This
  is the operator's escape hatch for categories of work they don't
  trust to auto-ship; the defaults align with the tags ideation itself
  uses to self-mark elevated-risk proposals. Operators may type the
  tag with or without the leading `#` (both parse identically); empty
  string falls back to the default set.
- `AP2_AUTO_APPROVE_FREEZE_THRESHOLD` (default `3`) — integer count.
  When N consecutive `task_complete` events have status in
  `{verification_failed, blocked, error, failed}` AND end in
  `retry_exhausted` (the failure chain actually froze a task rather
  than looping a single TB through retries), the daemon halts
  auto-promotion of `auto_approved` tasks. Operator-approved tasks
  (those promoted via `ap2 approve` → `ideation_approved` event)
  continue to dispatch normally — the freeze is targeted at the auto
  layer, not blanket. Operator unfreezes via `ap2 ack
  auto_approve_unfreeze --reason "<one-line rationale>"` (uses the
  existing TB-106 ack pattern — the daemon scans `operator_ack`
  events' `note` field for the `auto_approve_unfreeze` token and
  resets the failure counter). Setting the threshold to `0` (or any
  non-positive int) disables the circuit-breaker entirely — the
  explicit escape hatch for operators who trust the upstream gates
  beyond this layer.

**Cost + blast-radius guards (TB-224).** Two layered token caps and a
single-event `task_error` halt that ride on top of TB-223's auto-approve
gate. Without these, `AP2_AUTO_APPROVE=1` trades manual review for
unbounded token spend — a "successful-but-wasteful" loop can satisfy
verification while burning tokens indefinitely, and a `task_error`
cascade (SDK timeout, agent OOM, kernel SIGKILL) needs operator
attention not a silent retry. **Defaults are unset on both knobs** —
operators who haven't done the cost-budgeting math for their project
don't get a hardcoded cap surprising them. The recommended pattern:
set both caps BEFORE flipping `AP2_AUTO_APPROVE=1`. Cross-references
`goal.md`'s **Current focus: end-to-end automation** axis 3 ("Cost and
blast-radius guards").

- `AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP` — integer cap on combined
  `input_tokens + output_tokens` per task. **Unset by default → no
  cap.** When set to a positive integer, the daemon checks each
  `task_run_usage` event (TB-165, emitted at every terminal path) for
  auto-approved tasks; an event whose combined tokens exceed the cap
  trips a `per_task_cap` halt — the daemon emits
  `auto_approve_halted reason=per_task_cap used=<N> cap=<M>` and
  pauses auto-promote of `auto_approved` tasks until operator emits
  `ap2 ack auto_approve_window_resume`. Catches the single-runaway
  pattern (one task in an infinite tool-call loop). Manual
  `ap2 approve` continues to dispatch even while halted — the pause
  is targeted at the auto-approved bucket only.
- `AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP` — integer cap on cumulative
  `input_tokens + output_tokens` across all auto-approved tasks in a
  rolling **24-hour window**. **Unset by default → no cap.** Computed
  by summing `task_run_usage` token fields over tasks identified as
  auto-approved (via TB-223's `auto_approved` audit event) within
  `now - 24h`. No new state file; tail-scan of events.jsonl, same
  shape the cron status-report uses. When the sum exceeds the cap,
  the daemon emits `auto_approve_halted reason=window_cap
  window_used=<N> cap=<M>` and pauses auto-promote. The rolling-24h
  shape matches the operator's natural rhythm without calendar-day
  timezone ambiguity. Catches the drift pattern: 50 small tasks each
  within the per-task cap but cumulatively unbounded.
- `task_error` single-event halt — distinct from
  `verification_failed` (which TB-223's `FREEZE_THRESHOLD` requires
  N=3 of). A `task_error` event indicates an infrastructure failure
  (SDK timeout, agent OOM, briefing read failure) per `events.jsonl`
  conventions; **one occurrence is enough** to halt auto-promote
  because infrastructure failures aren't statistical noise — they
  need operator attention immediately. When a `task_error` lands for
  an `auto_approved` task, the daemon emits
  `auto_approve_halted reason=task_error task=TB-N
  error_excerpt=<...>` AND appends a `## Decisions needed from
  operator` bullet to `.cc-autopilot/ideation_state.md` naming the
  failing TB-N + error excerpt (so `ap2 status` and the web home
  page surface it without waiting for the next ideation cron).
- **Shared resume ack:** `ap2 ack auto_approve_window_resume --reason
  "<rationale>"` clears any of the three halt reasons above (one ack
  covers all three since they share the same auto-promote-paused
  state). Different verb from TB-223's `auto_approve_unfreeze`
  because the two halts have semantically-distinct entry paths
  (cumulative-regression vs. cost/blast-radius) and the audit trail
  benefits from one log line per class of issue. Reuses the existing
  TB-106 ack pattern (the daemon scans `operator_ack` events' `note`
  field for the `auto_approve_window_resume` token and resets the
  halt state).

Audit events: `auto_approve_halted` fires once per triggering
episode (deduped via tail scan); `auto_approve_skipped` fires once
per preempted auto-promote tick (with the would-have-promoted TB-N)
so the cumulative skipped-count is visible in `ap2 logs` for
operators tuning the cap values.

**Pre-flight surface for cap misconfiguration (TB-234).** `ap2 doctor`
has an `auto-approve safety floor` audit section that fires WARN when
`AP2_AUTO_APPROVE` is set to a truthy value (`1` / `true` / `yes`) but
`AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP` and/or
`AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP` is unset, empty, zero, or
non-integer. With both caps disabled, an additional summary WARN names
the configuration as "safety floor OFF" and cross-links `goal.md`
L102-113. WARN, not FAIL — the operator may have a reason to run
uncapped for a short window — but loud enough that an `ap2 doctor`
run after flipping `AP2_AUTO_APPROVE=1` reveals the gap before the
SDK bill arrives. When auto-approve is unset (the default), the section
emits a single INFO line stating manual approval is required per task.
The audit is purely diagnostic: no events written, no daemon state
mutated. Pair with `ap2 status`'s continuous `automation_status`
surface (TB-227) — doctor is the one-shot pre-flight, status is the
ongoing snapshot.

**Auto-unfreeze on agent-diagnosed briefing-shape fixes (TB-225).**
Self-heals the recurring class of retry-exhausted Frozen tasks whose
root cause is a briefing-shape regression the agent already diagnosed
in its `task_complete status=blocked` summary. Two known prod examples
the brief calls out: TB-204 (`grep -lE` → `grep -rlE` on a directory
target — missing the `-r` flag returns nothing), TB-207 (literal-
backtick in shell bullets truncates the shell command at the first
backtick). Both shapes are catalogued in
`ap2/ideation.default.md`'s `## Shell-bullet pitfalls to AVOID`
section. With this gate on, the daemon parses the agent's structured
`BriefingFix:` prefix, verifies the briefing-line literal match,
patches the briefing via the operator-queue `update` op, and unfreezes
the task — all without operator-manual `ap2 unfreeze`. **Defaults are
unset / conservative — feature is opt-in only.** Cross-references
`goal.md`'s **Current focus: end-to-end automation** axis 2
("Failure-recovery operator dependency").

The canonical agent-prefix contract (task agents emit this line as
part of their `report_result(status="blocked", summary=...)` payload
when they diagnose a briefing-shape regression as the root cause):

    BriefingFix: <shape> at <briefing_path>:<line>: <from> -> <to>

Worked example:

    BriefingFix: grep_missing_r_on_dir at .cc-autopilot/tasks/foo.md:23: grep -lE 'pattern' ap2/tests/ -> grep -rlE 'pattern' ap2/tests/

The parser (`ap2._shared.parse_blocked_summary_fix_shape`) is
strictly structured — no regex-on-prose guessing — so an agent that
authors free-text diagnoses (no `BriefingFix:` line) falls through
to today's manual-unfreeze path identically.

See also `skills/ap2-task/SKILL.md` § "Reporting failures
(`task_complete blocked` summaries)" — the upstream emitter contract
the per-task agent reads at run time, with one fenced worked example
per bootstrap fix-shape (TB-229).

- `AP2_CRON_DISABLED` — TB-381 component-level kill switch for the
  cron scheduler (the `ap2/components/cron/` component that owns the
  due-check loop + per-job handler dispatch + `cron_*` lifecycle
  events). **Unset by default → cron fires on schedule.** When set to
  a truthy value (`1` / `true` / `yes` / `on`, case-insensitive) the
  scheduler's tick hook self-gates at the top of `Phase.CRON_DISPATCH`
  and no cron job runs. Mirrors `AP2_JANITOR_DISABLED` /
  `AP2_AUTO_UNFREEZE_DISABLED` polarity / naming (suppress-polarity /
  `default_enabled=True`), so `ap2 status` renders the on/off state
  correctly. The interval engine (`cron.yaml` / `cron_state.json`,
  job schedules) and the `cron_propose` / `cron_edit` write-path are
  unchanged — this knob only stops the daemon-side scheduler from
  dispatching.
- `AP2_AUTO_UNFREEZE_DISABLED` — TB-320 component-level kill switch
  for the auto-unfreeze sweep. **Unset by default → sweep runs.**
  When set to a truthy value (`1` / `true` / `yes` / `on`,
  case-insensitive), `_maybe_auto_unfreeze` short-circuits at the
  top of the tick hook before any other guard runs and emits an
  `auto_unfreeze_disabled` event once per process (sticky dedup;
  resets only on daemon restart). Mirrors `AP2_JANITOR_DISABLED` /
  `AP2_VALIDATOR_JUDGE_DISABLED` polarity / naming. The registry's
  `Manifest.is_enabled` filter for the `auto_unfreeze` component
  uses the same knob (suppress-polarity / `default_enabled=True`),
  so `ap2 status` renders the on/off state correctly. Coarser-
  grained than `AP2_AUTO_UNFREEZE_FIX_SHAPES` (which selects which
  shapes are auto-patched); this knob disables the entire sweep
  regardless of allowlist contents.
- `AP2_AUTO_UNFREEZE_FIX_SHAPES` — comma-separated allowlist of
  fix-shape tokens. **Unset by default → feature disabled.** The
  daemon refuses to auto-apply any shape that isn't in this
  allowlist; unknown shapes still require manual `ap2 unfreeze`.
  The env-knob string IS the trust contract: operators audit each
  shape and opt in by listing tokens. Recommended bootstrap list
  (each names a known pitfall in
  `ap2/ideation.default.md`'s `## Shell-bullet pitfalls to AVOID`
  section):
  - `grep_missing_r_on_dir` — `grep -lE 'pattern' <dir>/` returns
    nothing without `-r`. Fix: `grep -rlE 'pattern' <dir>/`.
  - `bare_python_to_uv_run` — `python -c '...'` exits 127 in the
    daemon environment. Fix: `uv run python -c '...'`.
  - `literal_backtick_in_shell_bullet` — a bullet with literal
    backticks like `` `grep ... | wc -l` `` truncates at the first
    backtick. Fix: drop the wrapping backticks; the bullet body IS
    the command.
  - `bare_path_to_test_f` — a bullet whose body is a bare path
    (e.g. `reports/foo.md`) tries to execute the file (exit 126).
    Fix: `test -f reports/foo.md`.
- `AP2_AUTO_UNFREEZE_MAX_PER_TASK` (default `1`) — integer cap on
  auto-unfreeze attempts per task before fallback to manual
  `ap2 unfreeze`. Bounds oscillation when the patched briefing
  ALSO fails. `0` disables the per-task cap (unbounded retries —
  intentionally not the default; disabling should be an explicit
  operator decision).
- `AP2_AUTO_UNFREEZE_MAX_PER_DAY` (default `3`) — rolling 24h cap
  on total auto-unfreeze applications across all tasks. When
  exceeded, the daemon halts further auto-unfreeze attempts on the
  tick AND appends a `## Decisions needed from operator` bullet to
  `.cc-autopilot/ideation_state.md` so `ap2 status` surfaces the
  systemic-regression signal. `0` disables the per-day cap.
- `AP2_AUTO_UNFREEZE_DRY_RUN` — TB-233 monitor-only on-ramp.
  **Unset by default.** When set to a truthy value (`1` / `true` /
  `yes`, case-insensitive) alongside a non-empty
  `AP2_AUTO_UNFREEZE_FIX_SHAPES`, the auto-unfreeze guard chain
  (allowlist + per-task cap + per-day cap + briefing-line match)
  still runs but the WRITE step is a no-op: instead of calling
  `_apply_auto_unfreeze_patch` (which queues `update` + `unfreeze`
  ops on the operator queue and mutates the briefing file), the
  daemon emits a `would_auto_unfreeze` audit event with the same
  payload shape as `auto_unfreeze_applied` plus the
  `file` + `line` fields from the parsed `BriefingFix:` prefix.
  The per-day-count + per-task-prior counters do NOT increment in
  dry-run (no real application). Use this to observe the loop's
  decisions on the live Frozen set without committing to the binary
  cliff. **Enablement on-ramp:** set both
  `AP2_AUTO_UNFREEZE_FIX_SHAPES=<shapes>` AND
  `AP2_AUTO_UNFREEZE_DRY_RUN=1`, leave the daemon running for a
  window (e.g. ≥24h), read `ap2 logs --type would_auto_unfreeze` to
  confirm the gate's decisions match your judgment, then unset
  `AP2_AUTO_UNFREEZE_DRY_RUN` to engage real patching. Sibling
  on-ramp to `AP2_AUTO_APPROVE_DRY_RUN` (TB-232) on the axis-1
  auto-approve side. **Pre-flight diagnostic** (TB-239): `ap2 doctor`
  emits a WARN in the `auto-unfreeze safety floor` section when
  `AP2_AUTO_UNFREEZE_DRY_RUN=1` is set without
  `AP2_AUTO_UNFREEZE_FIX_SHAPES` — `_maybe_auto_unfreeze` early-
  returns on empty allowlist BEFORE the dry-run check, so the
  observation knob is a silent no-op without the allowlist. Run
  `ap2 doctor` after flipping the dry-run knob to confirm both env
  vars are wired. **Operator's primary readiness surface** (TB-238):
  the scheduled `status-report` Mattermost post's
  `## Automation loop activity` section grows a trailing
  `*Dry-run window:*` sub-block while either dry-run knob is on,
  listing the 24h rolling count of `would_auto_unfreeze`
  (and/or `would_auto_approve`) events. Watch the count rise post-by-
  post for confidence the gate is exercising decisions before
  flipping the knob off; the sub-block is omitted entirely when
  both dry-runs are off so default-off projects stay byte-identical
  to TB-228 output.

Audit events: `auto_unfreeze_applied` (success — payload `task`,
`shape`, `from`, `to`); `auto_unfreeze_skipped` (any guarded skip —
payload `task` + `reason` token; one of
`shape_not_in_allowlist`, `briefing_mismatch`,
`briefing_path_missing`, `per_task_cap`, `per_day_cap`,
`queue_error`, `sweep_error`). The `knob_unset` baseline does NOT
emit per-tick — the feature is opt-in and operators who haven't
set `AP2_AUTO_UNFREEZE_FIX_SHAPES` shouldn't see noise.

Why operator-curated allowlist (not heuristic detection): arbitrary
briefing edits by the daemon are blast-radius-unsafe. The allowlist
lets the operator audit each fix-shape and opt in specifically;
shapes can be removed instantly if one misfires by editing the env
string. New shapes never auto-promote — the operator opens new
shapes by editing the env value, the daemon never invents them.

Why the briefing-line literal match check: the agent's diagnosis may
be stale if the briefing was operator-edited between failure and
freeze handling (e.g. the operator hand-edited it trying to fix it
themselves). Verifying the `from` pattern is literally present on
the named line before patching closes the data-race window. A
mismatch emits `auto_unfreeze_skipped reason=briefing_mismatch` and
leaves the task Frozen — fail-safe.

**Ideation-exhaustion halt (TB-226 axis 4; collapsed TB-342; merged
to core TB-345).** Two env knobs gate the ideation-exhaustion halt
(now core ideation lifecycle in `ap2/ideation_halt.py`). See
`### Focus state (axis 4)` below for the architecture + the resume
verb (`ap2 update-goal`) vs. the `ap2 ack roadmap_complete` dismiss
verb (TB-340: ack dismisses the nag, never resumes ideation; TB-342:
editing goal.md is the sole resume signal post-collapse).

- `AP2_IDEATION_HALT_EMPTY_CYCLES` (default `3`, min `1`, max `20`) —
  ideation-exhaustion halt threshold: number of consecutive empty
  (0-proposal) ideation cycles since the last `goal_updated` before
  the daemon emits `roadmap_complete` and parks the ideation trigger.
  TB-283 collapsed the pre-existing two-path advance mechanism
  (LLM-judge against `Done when:` bullets + empty-cycles heuristic) to
  the single empty-cycles signal; TB-345 merged the detector into the
  core `ap2/ideation_halt.py` module. Invalid (non-int / empty) values
  fall back to the default; values outside the clamp range are pinned
  to the nearest bound, i.e. clamped to `[1,20]` (so a typo `0`
  doesn't disable the halt and `999` doesn't wedge it permanently).
  Replaces the deprecated alias `AP2_FOCUS_ADVANCE_EMPTY_CYCLES`
  (deprecated alias — kept as a back-compat alias for one release,
  mapping to the same `core.ideation_halt_empty_cycles` path).
- `AP2_IDEATION_HALT_DISABLED` — kill switch for the
  ideation-exhaustion auto-halt. Set to `1` / `true` / `yes` / `on`
  (same convention as `AP2_IDEATION_DISABLED`) to prevent the daemon
  from auto-emitting `roadmap_complete` even when the threshold trips;
  the daemon surfaces a `## Decisions needed from operator`
  (decisions-needed) bullet instead so the operator can halt manually
  (by editing goal.md). Default unset → false → auto-halt enabled.
  Replaces the deprecated alias `AP2_FOCUS_AUTO_ADVANCE_DISABLED`
  (deprecated alias — kept as a back-compat alias for one release,
  mapping to the same `core.ideation_halt_disabled` path).

**Watchdog (auto-diagnose).**
- `AP2_AUTO_DIAGNOSE_IDLE_THRESHOLD_S` (10800 = 3h) — idle duration
  before the watchdog posts a `DiagnoseReport`.
- `AP2_AUTO_DIAGNOSE_COOLDOWN_S` (21600 = 6h) — minimum gap between
  watchdog posts (re-fire spam guard).

**Attention surface (TB-282 / TB-287 / TB-288 / TB-289 / TB-290 / TB-297).**
The five attention detectors (`task_stuck`, `task_frozen`,
`validator_judge_noisy`, `auto_approve_paused`, `cost_cap_approach`)
each read fresh from `os.environ` at detection time — see
`AP2_TASK_STUCK_THRESHOLD_S` / `AP2_ATTENTION_DEBOUNCE_S` /
`AP2_TASK_FROZEN_RECENCY_S` / `AP2_AUTO_APPROVE_COST_APPROACH_PCT`
described in the **ap2-observability** skill's "Proactive attention
surface" section. The
knob below controls the push-side cadence (immediate vs the
status-report cron's tick rate):
- `AP2_ATTENTION_IMMEDIATE_PUSH` — TB-297 opt-in immediate-Mattermost-
  push on `attention_raised` emission. **Unset by default → push
  OFF** so the status-report cron remains the routine push
  surface (TB-282's `## Attention needed` section already carries
  the same conditions there). Set to a truthy value
  (`1` / `true` / `yes` / `on`, case-insensitive) to enable. With
  the knob ON, the daemon's `_maybe_push_attention` helper posts a
  one-line `[<project_name>] ⚠ <summary>` message to
  `AP2_MM_CHANNELS[0]` AFTER each fresh `attention_raised` event
  appends — per-(type, key) push debounce reuses the
  `AP2_ATTENTION_DEBOUNCE_S` window structurally (the push runs
  only when a fresh event emits, which already honors the
  detector-debounce). Missing-destination handling mirrors the
  watchdog: one sticky `attention_push_no_destination` audit event
  per state-file lifetime when `AP2_MM_CHANNELS` is unset (flag
  lives in `.cc-autopilot/attention_push_state.json`, gitignored;
  resets to false on the next successful push). Audit events:
  `attention_pushed` on success; `attention_push_error` on
  `_mm_post` failure; `attention_push_no_destination` on the
  missing-channel sticky-warn path. The push knob is operator
  opt-in once they've sampled their own detector cadence — set
  this when the post-trip `auto_approve_paused` /
  `cost_cap_approach` / time-sensitive conditions in your project
  warrant inside-one-tick visibility rather than up-to-the-next-cron-tick-wait
  visibility. Hot-reloadable (TB-271) so an operator flipping the
  knob takes effect on the next tick without a daemon restart.

**Janitor (chore-judge, TB-178).**
- `AP2_JANITOR_MAX_FINDINGS_LLM` (10) — cap on per-cycle findings sent
  to the SDK judge. `0` disables the judge call entirely (the janitor
  emits rule-based findings only).
- `AP2_JANITOR_DISABLED` (TB-309) — kill switch for the entire janitor
  component (declared in `ap2/components/janitor/manifest.py`). Set
  truthy (`1`, `true`, `yes`) to disable; default unset = enabled.
  Distinct from `AP2_JANITOR_MAX_FINDINGS_LLM=0` (which keeps the
  deterministic detector running but disables the LLM judge); this
  flag skips the janitor entirely. Reserved for the axis-(2) daemon
  tick-hook walk landing in a later TB — for TB-309 the schema field
  is declared but the daemon does not yet consult it before
  dispatching the `janitor` cron job.

**Channel adapters (axis 3).**
- `AP2_CHANNEL_FILE_PATH` (TB-312) — target path for the
  `FileAppendChannelAdapter` (one of three core-shipped sibling
  adapters in `ap2/channel.py`). Defaults to
  `<cwd>/.cc-autopilot/channel.log` when unset. Operators wiring a
  non-Mattermost delivery (or piping ap2's outbound digests into a
  local log file for grep / tail) point this at the destination
  they want appended. Hot-reloadable: read fresh from `os.environ`
  on every `.post(...)` call so a hot-swapped env value (TB-271)
  takes effect on the next adapter dispatch.
- `AP2_WEBHOOK_URL` (TB-312) — destination for the
  `WebhookChannelAdapter` (POSTs `{"text": <text>, **meta}` as JSON
  to the URL). Unset → adapter returns `None` without raising, the
  caller's audit event notes the no-destination state. Compatible
  with Slack incoming webhooks, Discord webhooks, internal HTTP
  collectors. Read fresh per `.post()` call (same hot-reload
  semantics as `AP2_CHANNEL_FILE_PATH`).

**Mattermost.**
- `AP2_MM_CHANNELS` — comma-separated MM channel IDs to poll for
  `@claude-bot` mentions. **TB-312 polarity note**: `AP2_MM_CHANNELS`
  is also the `env_flag` on the `mattermost` component's manifest
  with `default_enabled=False`. Unset / empty → component is
  disabled, `registry.channel_adapters(cfg)` returns no Mattermost
  adapter, the watchdog / attention-push paths emit the
  `*_no_destination` audit event family they already used pre-TB-312
  when `_first_mm_channel()` returned "". Any non-empty value
  enables both delivery (the `MattermostChannelAdapter` is
  registered) AND polling (the daemon's `_mm_loop` walks the
  registry's `inbound_poll` hook and reaches
  `check_new_messages`). The env-knob name is verbatim-preserved
  per goal.md L64-67 — DO NOT rename this key without an operator-
  visible migration.
- `AP2_MM_REPORT_CHANNEL` (TB-190) — explicit channel ID for
  status-report posts. Unset → falls back to `AP2_MM_CHANNELS[0]`.
- `AP2_PROJECT_NAME` (TB-280) — operator-facing project identity that
  leads the status-report Mattermost headline (`**[<project_name>]
  Autopilot Status Report** — <now>`). Defaults to `project_root.name`
  so a project at `/home/user/code/stoch` posts under `[stoch]`
  without configuration; override when the directory name is generic
  (`main`, `proj`) or carries a layout suffix the operator doesn't
  want surfaced. Hot-reloadable — a rename takes effect on the next
  tick without `ap2 stop && ap2 start`.
- `AP2_MM_MENTION` (`@claude-bot`) — pattern that triggers handler
  dispatch.
- `AP2_MM_BOT_USER_ID` — bot's user ID (used for self-message
  filtering so the handler doesn't loop on its own replies).
- `AP2_MM_TEAM_ID` — Mattermost team ID (sandbox install-channel
  helper uses this).

**Local web UI (`ap2 web`, daemon-spawned read-only HTTP).**
- `AP2_WEB_PORT` (7820) — bind port. Malformed values fall back to
  the default rather than crashing daemon startup.
- `AP2_WEB_DISABLED` — set to `1`/`true`/`yes`/`on` to skip starting
  the daemon-spawned web UI.

**Agent backend selection (`[agent_backends]` / `AP2_AGENT_BACKEND_<KIND>`,
TB-358).** Every agent kind runs on a pluggable backend — `claude` (the
default; `claude_agent_sdk` against the bundled Claude Code binary) or `codex`
(OpenAI's `openai_codex` SDK). `select_adapter(kind, cfg)` resolves a kind's
backend via `Config.get_agent_backend(kind)`, precedence high → low:

1. `AP2_AGENT_BACKEND_<KIND>` env override — `<KIND>` upper-cased onto the
   suffix (e.g. `AP2_AGENT_BACKEND_TASK=codex`,
   `AP2_AGENT_BACKEND_STATUS_REPORT=codex`). Call-time-evaluated, so a
   mid-process export propagates on the next dispatch; a blank value is treated
   as unset.
2. The `[agent_backends]` TOML table in `.cc-autopilot/config.toml`
   (`task = "codex"`, …).
3. `DEFAULT_AGENT_BACKEND = "claude"` — an unmapped kind, and an all-default
   install, resolves to claude.

Selectable kinds (`ap2.adapters.select.AGENT_KINDS`): `task`, `ideation`,
`status_report`, `cron`, `mattermost`, plus the `verifier_judge`,
`ideation_scrub`, `validator_judge`, `janitor_judge` adapter-routed
agent-kind calls — each
independently routable. An unknown / typo'd backend id degrades to claude
rather than crashing dispatch. Whichever backends the resolved map references
must have their credentials present at daemon start (next).

**Model pin caveat (`AP2_AGENT_MODEL` / `[core] agent_model`, TB-396).**
`agent_model` is a SINGLE global model applied to whichever backend a kind
resolves to — there is no per-backend model knob (yet; deferred). Its default is
provider-neutral (unset → resolves to `None`), so each backend self-defaults
(Claude's CLI default, codex's native default) and a codex-routed kind isn't
handed a Claude model out of the box. If you PIN it, the pinned value must be
valid for EVERY backend in your `[agent_backends]` map: a `claude-*` id pinned
via `AP2_AGENT_MODEL` will fail a codex-routed kind (the live codex turn rejects
the unknown model). So either leave it unset, or — if you mix backends — set a
model each backend accepts (or repoint the affected kind back to claude).

**Daemon-start auth gate (`_require_oauth_token`, TB-79 → TB-358 →
TB-370).** Before forking, the daemon walks the resolved per-kind
backend map and requires exactly the credentials that set implies:

- Any **claude**-backed kind requires `CLAUDE_CODE_OAUTH_TOKEN`
  (present-only check; the SDK control protocol silently times out at
  `initialize` without it). An all-claude install — the default —
  needs only this, exactly as pre-axis-5.
- Any **codex**-backed kind requires a codex credential, satisfied by
  **EITHER** of codex's two auth modes:
  - `OPENAI_API_KEY` — metered OpenAI API billing; or
  - a **codex ChatGPT-login OAuth session** — created by `codex login`
    (browser) / `codex login --device-auth` (headless), stored at
    `$CODEX_HOME/auth.json` (default `~/.codex/auth.json`) with
    `"auth_mode": "chatgpt"` plus auto-refreshing access+refresh
    tokens. This is the subscription path — no per-call billing — and
    matches ap2's own Claude posture (OAuth subscription, not an API
    key; goal.md Constraints).

The codex check is a **presence-only pre-flight**, the exact analog of
the Claude side: it reads `auth.json` only to confirm the file exists
and is `auth_mode: chatgpt`. It does NOT shell out to codex, hit the
network, validate, or refresh the token (token contents are never
read or logged) — `openai_codex` rotates the refresh token (~every 8
days) at runtime. A codex kind with neither `OPENAI_API_KEY` nor a
chatgpt `auth.json` fails the gate with a message naming both options
(set `OPENAI_API_KEY`, or run `codex login`).

**Installing the codex backend (the `codex` extra).** Credentials alone
are not enough — a codex-backed kind needs the `openai_codex` handle
installed too, and that ships as an opt-in extra so the default install
stays Claude-only. The base `pip install autopilot2` / `uv sync`
resolves only `claude-agent-sdk` (the always-installed backend); to run
a codex-backed kind you must additionally install the extra:

    pip install 'autopilot2[codex]'
    # or, in a uv-managed checkout:
    uv sync --extra codex

The extra pulls the `openai-codex` distribution (OpenAI's official Codex
SDK, which bundles the Codex CLI binary) that provides the
`import openai_codex` handle `CodexAdapter` lazily imports at first
dispatch (matching the daemon-start gate's `uv pip install openai-codex`
remediation hint). A live codex-backed kind therefore needs **both**
the `codex` extra installed **and** an OpenAI/codex credential (the
auth gate above — `OPENAI_API_KEY` or a chatgpt `auth.json`). Without
the extra the codex-handle gate refuses to start; without a credential
the auth gate does.

### Focus state (axis 4)

Closes goal.md L115-138's axis 4 design — the operator's intent /
priority surface for what the daemon should work on. The operator
authors one or more `## Current focus:` headings in `goal.md`
(priority order, top → bottom); the daemon's runtime detector flags
ideation as exhausted when proposals stop landing. The daemon never
mutates goal.md itself (goal.md L187-191 "Goal.md auto-rotation"
Non-goal).

**Multi-focus prose, not a state machine (TB-342).**
The operator can list several `## Current focus:` headings as
priority-ordered prose / intent — the ideation agent reads the whole
goal file every cycle and the goal-anchor validator accepts ANY of
those headings as a proposal anchor. The daemon does NOT sequence
foci with a pointer walk: pre-TB-342 it advanced an in-memory
pointer from focus 1 → focus 2 once empty-cycles tripped, but
ideation never actually scoped itself to the "active" focus (the
ideation prompt was never told which focus was active), so the walk
changed nothing about what got proposed. TB-342 collapsed that
rotation theatre into a single ideation-exhaustion detector — same
end state, less wasted SDK spend (the halt now fires after
`threshold` empty cycles instead of `num_foci × threshold`).

**Pointer file.**
`.cc-autopilot/focus_pointer.json` carries the runtime state
(`empty_cycles`, `roadmap_complete_ack_idx`, `roadmap_complete_emitted`,
`updated_ts`, `schema`). Fenced from task agents
(`TASK_AGENT_FENCED_PATHS`) and gitignored so rollbacks (TB-111)
don't re-fire stale `roadmap_complete` events. Schema-versioned via
the `schema: 1` field so a future migration can branch cleanly. The
pre-TB-342 multi-focus rotation fields (`active_index`,
`active_title`, `exhausted_titles`) went away with the pointer walk.

**Exhaustion heuristic (empty-cycles, sole signal).**
Each tick, `_maybe_advance_focus(cfg, sdk)` runs as step 0.6 of
`_tick` (after the auto-unfreeze sweep, before cron / pipeline /
dispatch / ideation). One signal drives the halt: the daemon counts
consecutive recent ideation cycles that produced 0 proposals. Each
cycle is delimited by an `ideation_empty_board` entry marker
(daemon-emitted at cycle start regardless of outcome) and one of
`ideation_complete` or `ideation_cycle_summary` (agent-emitted exit
marker — `_complete` when the cycle proposed at least one task,
`_cycle_summary` when no proposals). The counter increments at the
exit marker if no `ideation_proposal_recorded` fired within the
cycle; resets to 0 if any proposal fired. `ideation_timeout` /
`ideation_error` exits don't count (infrastructure failure ≠
"ideation reasoned and found nothing"). The counter ALSO resets at
the most recent `goal_updated` event — the operator-edits-goal.md
signal that fires from the `ap2 update-goal` drain handler; pre-edit
empty cycles don't count against the post-edit runway. When the count
reaches `AP2_IDEATION_HALT_EMPTY_CYCLES` (default `3`, clamped to
`[1, 20]`), the daemon emits `roadmap_complete` and parks the
ideation trigger.

The intuition: ideation reads the full goal.md + the recent task
arc every cycle. If multiple consecutive cycles can find nothing
substantive worth proposing, ideation has itself implicitly judged
the goal exhausted — that's the load-bearing signal. Per-focus
`Progress signals:` bullets (if present) feed ideation's per-cycle
assessment as advisory context but do NOT gate the halt; ideation's
empty-output behavior is what the daemon reacts to.

Pre-TB-283 the daemon also ran an LLM-judge advance path that
diff-read recent task commits against the operator-authored
per-focus completion bullets (the sub-block now named
`Progress signals:`) and advanced on a `yes` verdict. That path
collapsed multi-week foci into ~3-task cycles whenever each commit
shape-satisfied one bullet, without ever actually verifying
substantive progress (the judge had no way to execute the code it
was reading diffs of). TB-283 deleted it; TB-285 renamed the
sub-block to clear the gating connotation that the prior name
carried.

On halt, the daemon emits `roadmap_complete exhausted_count=<n>
trigger=empty_cycles_heuristic` and writes the updated pointer
(`roadmap_complete_emitted=True`, dismissal marker cleared). The
`focus_advanced` event the pre-TB-342 rotation pass emitted is no
longer fired; the `pointer_past_last` trigger value the pre-TB-342
halt emitted is gone — the trigger is `empty_cycles_heuristic`
across the board now (the empty-cycles heuristic IS the halt).

**Kill-switch.**
`AP2_IDEATION_HALT_DISABLED=1` short-circuits the auto-halt
even when criteria are met. The daemon surfaces a `## Decisions
needed from operator` bullet so the operator halts manually (by
editing goal.md). The next tick re-emits the bullet if criteria
still trip — acceptable noise floor. Use this for full-manual
governance when the operator wants per-halt review. TB-345 renamed
this knob from `AP2_FOCUS_AUTO_ADVANCE_DISABLED` (kept as a
deprecated back-compat alias for one release).

**Operator workflow.**
The lifecycle is fully covered by existing verbs — no dedicated
`ap2 advance` / `ap2 rewind-focus` command exists or is needed:

- *Extend or reword the goal* — author additional `## Current focus:`
  headings (or rework existing ones) in `goal.md` and apply via
  `ap2 update-goal --file PATH`. The drain handler emits
  `goal_updated`, calls `goal.reset_pointer_on_goal_updated` to clear
  `roadmap_complete_emitted` + `empty_cycles` + the dismissal marker,
  and ideation resumes on the next tick. The empty-cycles counter
  also resets at the `goal_updated` cutoff, so the runtime read and
  the on-disk pointer agree.
- *Retire the roadmap (genuinely done)* — let the detector trip
  `roadmap_complete`, then run `ap2 ack roadmap_complete --reason
  "..."`. This DISMISSES the recurring nag via the pointer's
  `roadmap_complete_ack_idx` marker (read by
  `goal.roadmap_complete_notice_dismissed`); ideation STAYS PARKED.
  The ack is NOT a resume verb — the pre-TB-340 implementation
  wrongly folded it into `roadmap_exhausted` so it un-parked
  ideation, resuming wasteful ~$1-3 SDK cycles against an already-
  exhausted goal every cooldown.
- *Force an ideation cycle anyway* — `ap2 ideate --force` bypasses
  the gate for one cycle. Useful when you just extended goal.md
  and don't want to wait for the next natural cooldown.
- **Do NOT direct-edit `.cc-autopilot/focus_pointer.json`.** The
  file is gitignored runtime state; a manual edit (even with the
  daemon paused) does not emit a `goal_updated` event, so the
  empty-cycles counter's reset cutoff doesn't move and pre-edit
  empty cycles keep counting against the post-edit runway. Use
  `ap2 update-goal` to refresh the goal file instead; the drain
  handler is the only legitimate operator path that moves the
  pointer's halt state.
- *Pause the whole loop* — `ap2 pause` for full stop (in-flight
  tasks finish, no new dispatch). Distinct from roadmap-complete:
  the parked-ideation state still dispatches operator-added
  Backlog tasks; pause stops everything.
- *Full-manual governance* — set
  `AP2_IDEATION_HALT_DISABLED=1` as above. The daemon never
  halts on its own; the operator halts manually via the decisions-
  needed bullet.

**Roadmap-complete: ideation parks (TB-275).**
When the empty-cycles threshold trips, the daemon emits
`roadmap_complete exhausted_count=<n> trigger=empty_cycles_heuristic`
(once, suppressed via the pointer's `roadmap_complete_emitted`
flag). From then on, the IDEATION TRIGGER skips: `_maybe_ideate`
emits `ideation_skipped reason=roadmap_complete` and bumps the
cooldown (TB-246), so a walk-away weekend that exhausts the goal
stops piling speculative proposals against an already-exhausted
context (without this gate, a 60-min cooldown × 48h weekend wastes
up to ~48 ideation SDK calls). The skip-gate is a sibling to
TB-174's focus-exhausted gate (same `ideation_skipped` event shape
with a different `reason` field; `force_ideate` bypasses both so
`ap2 ideate --force` works on the operator's recovery path).
TASK DISPATCH IS NOT AFFECTED (TB-275): already-queued Backlog
tasks — operator-added via `ap2 add`, operator-approved via
`ap2 approve`, or previously auto-approved by ideation — continue
to auto-promote and dispatch normally. Once ideation is gated, no
new speculative work can enter the Backlog anyway, so everything
in the queue is operator-originated or already-proposed and should
always drain. A genuine full-stop is `ap2 pause`, a separate
explicit mechanism. RESUMING ideation is editing goal.md (TB-342
collapsed the pre-existing `rewind-focus` recovery path): the
`ap2 update-goal` drain handler emits `goal_updated` and calls
`reset_pointer_on_goal_updated`, which clears
`roadmap_complete_emitted` so `goal.roadmap_exhausted` returns False
naturally and ideation re-arms on the next tick. `ap2 ack
roadmap_complete --reason "..."` does NOT resume — it sets the
pointer's `roadmap_complete_ack_idx` dismissal marker, read ONLY by
`goal.roadmap_complete_notice_dismissed`, which quiets the recurring
operator nag on the `ap2 status` / web / cron surfaces; ideation
STAYS PARKED. `_maybe_advance_focus` clears that marker on each
fresh `roadmap_complete` emit, so a dismissal can't go stale across
an extend→re-exhaust cycle at the same foci count (the 2026-05-29
bug, where a stale ack_idx from a prior episode defeated the cheap-
skip and ideation auto-resumed wasteful SDK cycles). The pre-TB-340
implementation instead folded an events-jsonl ack-scan (the same
shape TB-223's `auto_approve_unfreeze` / TB-224's
`auto_approve_window_resume` use) into `roadmap_exhausted` itself,
so the ack wrongly un-parked ideation.

**Status-report push surface (TB-244).**
The `roadmap_complete` event surfaces in the status-report
Mattermost cron post — the operator's primary walk-away channel.
The routine renders a `## Focus rotation activity` sub-block
(parallel to TB-228's `## Automation loop activity` digest) listing
one bullet per `roadmap_complete` event in the inter-report window,
with the two-verb hint (`ap2 update-goal` resumes; `ap2 ack
roadmap_complete` dismisses — TB-340 / TB-342) rendered verbatim on
the halt line so the operator can copy-paste it from the post
(suppressed once the operator dismissed THIS episode). Closes
the push-surface gap TB-242 left open: the pull surfaces
(`ap2 status` text/JSON + web home) showed the focus list +
halt state on-demand, but a `roadmap_complete` halt at 03:00Z used
to wait for the operator's next manual `ap2 status` to surface. Now
the next status-report cron post carries it. Omit-on-empty: the
sub-block is suppressed when no `roadmap_complete` events landed in
the window. The `_STATUS_REPORT_AUTOMATION_INTERESTING_TYPES`
frozenset in `ap2/status_report.py` also lists `roadmap_complete` so
a lone halt event keeps the routine's skip-gate from firing.

(Historical note: pre-TB-342 the digest also rendered
`focus_advanced` bullets for the multi-focus rotation pointer walk,
showing `(N of M)` positions. TB-342 collapsed the rotation theatre;
the digest now carries only the halt line.)

**Why never auto-mutate goal.md.**
Goal.md L187-191 names goal.md auto-rotation as a Non-goal. The
operator owns the focus list; the daemon halts when ideation runs
empty but never writes the file. Adding / reordering / retiring
foci stays `ap2 update-goal`-only. This keeps the surface symmetric
with the other operator-only paths (cron mutation via `ap2 cron
edit`, classify-verdict via `ap2 classify`, ack via `ap2 ack`).

### Channel-adapter convention (axis 3, TB-312)

Outbound delivery — auto-diagnose digests (`auto_diagnose_fired`),
pending-review reminders (`pending_review_reminder`), attention
immediate-pushes (`attention_pushed`) — flows through registered
`ChannelAdapter`s rather than calling Mattermost helpers directly.

**Contract.** Every adapter subclasses `ap2.channel.ChannelAdapter`
and implements:

    class MyChannelAdapter(ChannelAdapter):
        name = "my-channel"

        def post(self, text: str, **meta) -> dict | None:
            ...  # deliver text via the channel-specific transport

`name` is the short identifier the registry uses for ordering. The
`**meta` shape is intentionally open — today's call sites pass
`channel` (the resolved Mattermost channel id, when applicable) and
`thread_id` (for reply-targeting). Adapters that don't consume a
given key MUST ignore it, never raise — forward-compat as new
delivery channels join the list.

Return value: a small dict describing the delivery (typically
`{"adapter": name, "post_id": ...}`) on success, `None` for a
best-effort no-op when the adapter is unconfigured (e.g. webhook
adapter with `AP2_WEBHOOK_URL` unset). Raising signals a hard
failure; the caller's per-adapter try/except emits a `*_error`
audit event and continues iterating.

**Registration.** Components register their adapter under
`Manifest.hook_points["channel_adapter"]` (either a class — the
registry instantiates fresh per call — or a module-level
singleton). The registry walks enabled manifests and returns the
adapter list via `default_registry().channel_adapters(cfg)` in
deterministic component-name-sorted order so dispatch is
reproducible across daemon restarts.

**Core-shipped sibling adapters** (in `ap2/channel.py`, not bound
to any component manifest by default — operators can wire them via
a project-specific component or call `_deliver(...)` directly for
unit / smoke contexts):

- `StdoutChannelAdapter` — prints `[stdout] <text>` to stdout.
  Useful for `ap2 start --foreground` smoke runs.
- `FileAppendChannelAdapter` — appends `<text>\n` to the file at
  `AP2_CHANNEL_FILE_PATH` (default
  `<cwd>/.cc-autopilot/channel.log`). Parent dir auto-created.
- `WebhookChannelAdapter` — POSTs `{"text": text, **meta}` as JSON
  to `AP2_WEBHOOK_URL`. Slack incoming webhooks, Discord, generic
  HTTP collectors. 10s fixed timeout — a slow webhook must not
  hold up the watchdog tick.

**Mattermost.** The Mattermost adapter (`MattermostChannelAdapter`)
lives under `ap2/components/mattermost/__init__.py` because the
HTTP client, channel/team/bot env knobs, and the `mattermost_reply`
MCP tool all move together (goal.md L184-186). The adapter routes
through `ap2.tools._mm_post` (a backwards-compat shim that defers
to the component's `_mm_post`) so pre-TB-312 tests monkeypatching
`tools._mm_post` keep working unchanged. See the `AP2_MM_CHANNELS`
polarity note in `## Configuration knobs` → Mattermost above for the
enable / disable rules.

## Config keys (TOML)

The **structured-config (env → TOML)** focus (goal.md L266-403) is
migrating the per-project tunable surface from the flat
`AP2_*` env-knob set above to typed TOML keys declared on each
component's `Manifest.config_schema`. The reference below enumerates
every key TB-321/322 landed; `ap2 init` writes the same set to
`<project>/.cc-autopilot/config.toml` as commented-out lines (the
`ap2.init.CONFIG_TEMPLATE` constant, rendered at module-import time
from `aggregate_schemas(default_registry())` so the scaffold stays
in lock-step with the schema). The
`test_every_config_key_documented` gate in
`ap2/tests/test_docs_drift.py` fails CI if a new `ConfigKey` is
declared and not listed here OR in
`ap2.init._CONFIG_TEMPLATE_EXEMPT_KEYS`.

**TOML layout** (goal.md L307-310):

    [core.<field>]              # non-component tunables (verifier,
                                # ideation, watchdog, etc.) — typed by
                                # `ap2.core_config_schema.CORE_CONFIG_SCHEMA`
                                # (TB-337). `validate_config` rejects
                                # unknown `[core.*]` keys at daemon-start
                                # with a clear "did you mean ...?" hint.
    [components.<name>.<key>]   # per-component knobs declared on
                                # `Manifest.config_schema`.
    [agent_backends]            # per-agent-kind backend map (TB-358):
                                # `<kind> = "claude" | "codex"`. NOT a
                                # `ConfigKey` — stashed verbatim by
                                # `config_loader.from_toml`; see the
                                # dedicated subsection below.

**Precedence** (high → low, applied by
`ap2.config_compat.apply_env_overrides` at daemon-start):

    sectioned env (`AP2_<SECTION>_<KEY>`)
      > flat env  (`AP2_<FLAT>`, back-compat per `FLAT_TO_SECTIONED`)
      > this TOML file (`.cc-autopilot/config.toml`)
      > in-source defaults.

`ap2/config_compat.FLAT_TO_SECTIONED` is the operator-facing back-
compat map: every `AP2_*` flat name in `## Configuration knobs` above
that has a TOML counterpart routes through that map (and emits a
one-shot `env_deprecated` event so the audit trail surfaces the
migration). The flat surface stays read-supported indefinitely; the
TOML surface is the forward-canonical authoring shape.

The hot-reload watcher (TB-271 extended by TB-323) tracks
`.cc-autopilot/config.toml`'s mtime alongside `.cc-autopilot/env`,
so a bumped tunable propagates on the next tick (≤30s) for
hot-reloadable keys; non-hot keys still need `ap2 stop && ap2 start`.

### [core] — non-component cluster tunables (TB-337)

The `[core.*]` namespace carries the 21 non-component knobs the
daemon's runtime needs: tick intervals, per-call timeouts, retry
budgets, agent runtime (model + effort + max-turns), the ideation
cluster, project identity, and the web server's lifecycle pair.
Schema declared in `ap2/core_config_schema.py::CORE_CONFIG_SCHEMA`;
`validate_config` walks both `[core.*]` and `[components.<name>.*]`
sub-tables against the union at daemon-start and rejects unknown /
mistyped keys with a clear named-path error (e.g. `[core] web_prot
= 8080: unknown key (did you mean \`core.web_port\`?)`).

Hot-reloadability mirrors `env_reload.HOT_RELOADABLE_KNOBS` /
`FIXED_KNOBS`: lifecycle knobs (`core.web_port`, `core.web_disabled`)
wire a stateful resource at daemon-start and need `ap2 stop && ap2
start`; everything else propagates on the next tick after a
`.cc-autopilot/env` or `.cc-autopilot/config.toml` mtime bump.

- `core.agent_effort` — str, default `""` (hot-reloadable). Global
  reasoning-effort label (`low` | `medium` | `high` | `xhigh` |
  `max`) passed as `extra_args={"effort": <value>}` to the SDK
  options. Per-job sub-knobs (`AP2_STATUS_REPORT_EFFORT`,
  `AP2_VERIFY_JUDGE_EFFORT`, `AP2_JANITOR_JUDGE_EFFORT`) override for
  their respective agents. Mirrors `AP2_AGENT_EFFORT`.
- `core.agent_model` — str, default provider-neutral / unset (resolves to
  `None`, hot-reloadable). Model name passed to the agent backend for task /
  control / verifier / janitor agents. The default is unset, so each backend
  self-defaults (Claude's CLI default, codex's native default) and a
  codex-routed kind isn't handed a Claude id (TB-396). The dispatch sites build
  `cfg.get_core_value("agent_model") or None`, so an empty-string env coerces to
  `None` too. Pin a model via `AP2_AGENT_MODEL` / this key — it applies to
  whichever backend a kind resolves to, so it must be valid for every backend in
  your `[agent_backends]` map (see the **Model pin caveat** under **Agent
  backend selection**). Read fresh from `os.environ` at each SDK invocation so
  hot-reload propagates immediately.
- `core.auto_diagnose_cooldown_s` — int, default `21600` (hot-reloadable).
  Idle-watchdog re-fire cooldown in seconds (TB-71). After an
  `auto_diagnose_fired` post, `_maybe_auto_diagnose` suppresses further
  diagnostics for this window so a persistently-idle board doesn't spam
  the channel every tick. Default 21600 (6h). Mirrors
  `AP2_AUTO_DIAGNOSE_COOLDOWN_S`.
- `core.auto_diagnose_idle_threshold_s` — int, default `10800`
  (hot-reloadable). Idle-watchdog trigger threshold in seconds (TB-71).
  `_maybe_auto_diagnose` posts a diagnostic to Mattermost once the board
  has made no forward progress for this long, surfacing a wedged daemon
  the operator would otherwise miss. Default 10800 (3h). Mirrors
  `AP2_AUTO_DIAGNOSE_IDLE_THRESHOLD_S`.
- `core.control_max_turns` — int, default `15` (hot-reloadable). Max
  turns per control-agent (mattermost / cron) SDK query. Tighter
  than `core.task_max_turns` because control agents do focused
  decide-then-route work, not implementation. Mirrors
  `AP2_CONTROL_MAX_TURNS`.
- `core.control_timeout_s` — int, default `1200` (hot-reloadable).
  Per-control-agent (mattermost / cron / ideation) SDK query timeout
  in seconds. Default raised to 20min in TB-278 after xhigh-effort
  ideation cycles routinely blew the old 5-min wall. Mirrors
  `AP2_CONTROL_TIMEOUT_S`.
- `core.event_context_size` — int, default `50` (hot-reloadable).
  Number of most-recent events from `.cc-autopilot/events.jsonl` the
  daemon injects into each agent briefing as the `Recent events`
  context tail. Mirrors `AP2_EVENT_CONTEXT`.
- `core.ideation_cooldown_s` — int, default `7200` (hot-reloadable).
  Minimum seconds between ideation cron fires when the board stays
  empty. Throttles the cycle so the agent isn't hammered every tick
  on a quiet project. Mirrors `AP2_IDEATION_COOLDOWN_S`.
- `core.ideation_disabled` — bool, default `false` (hot-reloadable).
  Kill switch for the empty-board ideation cron. Truthy value opts
  the project out of automatic backlog refill. Mirrors
  `AP2_IDEATION_DISABLED`.
- `core.ideation_halt_disabled` — bool, default `false`
  (hot-reloadable). Kill switch for the ideation-exhaustion auto-halt;
  when truthy the daemon surfaces a decisions-needed bullet instead of
  auto-emitting `roadmap_complete`. Mirrors `AP2_IDEATION_HALT_DISABLED`
  (deprecated alias `AP2_FOCUS_AUTO_ADVANCE_DISABLED`).
- `core.ideation_halt_empty_cycles` — int, default `3`
  (hot-reloadable). Number of consecutive empty (0-proposal) ideation
  cycles since the last `goal_updated` before the daemon emits
  `roadmap_complete` and parks the ideation trigger; clamped to
  `[1,20]`. Mirrors `AP2_IDEATION_HALT_EMPTY_CYCLES` (deprecated alias
  `AP2_FOCUS_ADVANCE_EMPTY_CYCLES`).
- `core.ideation_max_turns` — int, default `100` (hot-reloadable).
  Max turns per ideation-agent SDK query. Default raised 30 → 100
  in TB-278 after a goal.md rewrite mid-cycle hit `error_max_turns`
  at 31. Mirrors `AP2_IDEATION_MAX_TURNS`.
- `core.ideation_scrub_model` — str, default
  `"claude-haiku-4-5-20251001"` (hot-reloadable). Model name for
  `ideation_scrub.py`'s post-write filter that strips
  exhaustion-asserting sentences from `ideation_state.md` after each
  ideation cycle. Mirrors `AP2_IDEATION_SCRUB_MODEL`.
- `core.ideation_trigger_task_count` — int, default `3`
  (hot-reloadable). Fire ideation when Ready+Backlog count is BELOW
  this threshold AND Active is empty. Doubles as the per-cycle
  proposal-slot budget. Set to 1 for the legacy "fire only when
  working queue is fully empty" behavior. Mirrors
  `AP2_IDEATION_TRIGGER_TASK_COUNT`.
- `core.max_retries` — int, default `3` (hot-reloadable). Number of
  retry attempts per task before it lands Frozen and routes through
  the auto-unfreeze / operator-ack path. Mirrors `AP2_MAX_RETRIES`.
- `core.mm_tick_interval_s` — int, default `10` (hot-reloadable).
  Mattermost polling tick interval in seconds. The `_mm_loop` runs
  in its own coroutine at a faster tempo than the main tick so
  operator pause / add / delete @bot mentions don't sit behind a
  30s `core.tick_interval_s`. Mirrors `AP2_MM_TICK_S`.
- `core.project_name` — str, default `""` (hot-reloadable).
  Operator-facing project name. Leads every status-report Mattermost
  headline (`**[<project_name>] Autopilot Status Report**`) so a
  multi-project operator can identify a post's source. Empty default
  falls back to `project_root.name`. Mirrors `AP2_PROJECT_NAME`.
- `core.status_report_effort` — str, default `""` (hot-reloadable).
  Per-site reasoning-effort label override for the status-report
  cron's control-agent SDK query. Same value space as
  `core.agent_effort` (`low` | `medium` | `high` | `xhigh` | `max`).
  Empty default = fall through to `core.agent_effort` at the call
  site; the per-site hardcoded fallback is `medium`. Mirrors
  `AP2_STATUS_REPORT_EFFORT`.
- `core.task_max_turns` — int, default `500` (hot-reloadable). Max
  turns per task-agent SDK query. Default raised 50 → 200 in TB-278
  after TB-122 hit the old wall at 51 turns, then 200 → 500 in
  TB-347 to match the validated operating value; bump further only
  for unusually heavy projects. Mirrors `AP2_TASK_MAX_TURNS`.
- `core.task_timeout_s` — int, default `3600` (hot-reloadable).
  Per-task SDK query timeout in seconds. Bumped 5min → 20min in
  TB-278 after xhigh-effort tasks routinely blew the wall, then to
  60min (3600s) in TB-347 to match the validated operating value.
  Mirrors `AP2_TASK_TIMEOUT_S`.
- `core.thinking_block_effort_drop_disabled` — bool, default `false`
  (hot-reloadable). Kill switch for the TB-356 graceful-degradation
  path: by default a task that fails with the bundled-CLI
  thinking-block-immutability 400 (`... thinking or redacted_thinking
  blocks in the latest assistant message cannot be modified`) has its
  effort stepped down one tier on the automatic retry
  (`xhigh`→`high`→`medium`→`low`, floored), emitting an
  `effort_downshift` event. Truthy restores constant-effort blind
  retry. Other failure classes never downshift. Mirrors
  `AP2_THINKING_BLOCK_EFFORT_DROP_DISABLED`.
- `core.tick_interval_s` — int, default `30` (hot-reloadable). Main
  daemon tick interval in seconds. The `_main_tick_loop` fires
  roughly once per `tick_interval_s` to walk cron, pipeline sweep,
  task dispatch, ideation, and watchdog. Lower values shorten
  reaction time at the cost of more loop overhead. Mirrors
  `AP2_TICK_S`.
- `core.verify_cmd` — str, default `""` (hot-reloadable).
  Project-wide regression gate shell command. Runs after every
  successful task-agent commit; failure routes the task through
  retry like any other crash. Empty default = no project-wide gate.
  Mirrors `AP2_VERIFY_CMD`.
- `core.verify_judge_effort` — str, default `""` (hot-reloadable).
  Per-site reasoning-effort label override for the verify-judge SDK
  query (the per-task verifier's optional LLM judge step). Same value
  space as `core.agent_effort` (`low` | `medium` | `high` | `xhigh` |
  `max`). Empty default = fall through to `core.agent_effort` at the
  call site; the per-site hardcoded fallback is `high`. Mirrors
  `AP2_VERIFY_JUDGE_EFFORT`.
- `core.verify_judge_max_turns` — int, default `20` (hot-reloadable).
  Max turns per verify-judge SDK query (the per-task verifier's
  optional LLM judge step). Default 20 — enough for a Read + verdict
  round-trip without runaway. Mirrors `AP2_VERIFY_JUDGE_MAX_TURNS`.
- `core.verify_timeout_s` — int, default `600` (hot-reloadable).
  Timeout in seconds for the `core.verify_cmd` regression gate.
  `ap2 doctor` warns when set below observed-typical successful
  verify duration. Mirrors `AP2_VERIFY_TIMEOUT_S`.
- `core.web_disabled` — bool, default `false`. Kill switch for the
  daemon-spawned web server. Truthy value skips the web task
  entirely (useful for headless / sandbox runs). Lifecycle knob
  (`env_reload.FIXED_KNOBS`): consulted once at daemon-start, so
  changes require `ap2 stop && ap2 start`. Mirrors `AP2_WEB_DISABLED`.
- `core.web_port` — int, default `8729`. TCP port the
  daemon-spawned web server binds to. Stable across restarts so
  bookmarks survive. Lifecycle knob (`env_reload.FIXED_KNOBS`):
  changes require `ap2 stop && ap2 start`. Mirrors `AP2_WEB_PORT`.

### `[components.attention]` — proactive operator-attention detector

The `attention/` component (TB-282 / TB-287 / TB-290 / TB-297)
surfaces `attention_raised` events for stuck / frozen / cost-cap-
approaching conditions. The status-report cron is the routine push
channel; `immediate_push` opts into per-event Mattermost posts.

- `components.attention.cost_approach_pct` — int, default `75`
  (hot-reloadable). Pre-trip `cost_cap_approach` detector threshold
  as percent of `AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP` (TB-290); fires
  when the rolling 24h auto-approved token sum is ≥ this percent of
  the cap. Mirrors `AP2_AUTO_APPROVE_COST_APPROACH_PCT`.
- `components.attention.debounce_s` — int, default `21600`
  (hot-reloadable). Per-(type, key) debounce window (seconds) for
  repeated `attention_raised` emissions (TB-282). Default ~6h so a
  still-stuck task re-fires roughly once per operator workday.
  Mirrors `AP2_ATTENTION_DEBOUNCE_S`.
- `components.attention.immediate_push` — bool, default `false`
  (hot-reloadable). Opt-in: post an immediate Mattermost message on
  each `attention_raised` event (TB-297). Default off so the
  status-report cron stays the routine push surface. Mirrors
  `AP2_ATTENTION_IMMEDIATE_PUSH`.
- `components.attention.task_frozen_recency_s` — int, default `86400`
  (hot-reloadable). Recency window (seconds) for `task_frozen`
  attention emission — a Frozen task whose most-recent
  `retry_exhausted` / `task_failed` event is within this window
  surfaces as a fresh attention condition (TB-287). Mirrors
  `AP2_TASK_FROZEN_RECENCY_S`.
- `components.attention.task_stuck_threshold_s` — int, default
  `14400` (hot-reloadable). Seconds an Active task may sit without
  progress before a `task_stuck` attention condition fires (TB-282).
  Mirrors `AP2_TASK_STUCK_THRESHOLD_S`.

### `[components.auto_approve]` — autonomous board-edit gate (TB-223)

Walk-away semantics: when enabled, ideation-authored backlog adds
auto-promote without operator review. The token caps + freeze
threshold provide the hard stop / cost ceiling pair.

- `components.auto_approve.cost_approach_pct` — int, default `75`
  (hot-reloadable). Pre-trip approach percentage for the rolling-24h
  auto-approved token window cap (TB-290). When the rolling-window
  sum reaches `cost_approach_pct / 100 * window_token_cap` (and
  `window_token_cap > 0`), the attention detector raises a
  `cost_cap_approach` bullet so the walk-away operator can react
  before the post-trip `auto_approve_paused` surface fires. Values
  >= 100 are clamped to 99 (the trip line is owned by the post-trip
  detector). Mirrors `AP2_AUTO_APPROVE_COST_APPROACH_PCT`.
- `components.auto_approve.dry_run` — bool, default `false`
  (hot-reloadable). Monitor-only mode (TB-232): runs the
  gate-evaluation path and emits `would_auto_approve` instead of
  applying the queued board-edit. Mirrors `AP2_AUTO_APPROVE_DRY_RUN`.
- `components.auto_approve.enabled` — bool, default `false`
  (hot-reloadable). Opt-in master switch for autonomous board-edit
  auto-approval (TB-223). Default off so a fresh install keeps
  operator-in-the-loop semantics. Mirrors `AP2_AUTO_APPROVE`.
- `components.auto_approve.freeze_threshold` — int, default `3`
  (hot-reloadable). Number of consecutive failed `task_complete`
  events that trips the auto-approve circuit-breaker (TB-223). 0 or
  negative disables the circuit breaker. Mirrors
  `AP2_AUTO_APPROVE_FREEZE_THRESHOLD`.
- `components.auto_approve.per_task_token_cap` — int, default `0`
  (hot-reloadable). Per-task token cap for auto-approved tasks
  (TB-224). 0 disables the cap; positive values trip the per-task
  halt path. Mirrors `AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP`.
- `components.auto_approve.window_token_cap` — int, default `0`
  (hot-reloadable). 24h rolling-window token cap across all
  auto-approved tasks (TB-224). 0 disables the cap; positive values
  trip the window halt path and require `ap2 ack
  auto_approve_window_resume`. Mirrors
  `AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP`.

### `[components.auto_unfreeze]` — briefing-shape auto-patch sweep (TB-225)

When a task lands Frozen with a `BriefingFix:` line whose shape sits
in the allowlist, the sweep patches the briefing in place and
re-dispatches without operator-manual `ap2 unfreeze`.

- `components.auto_unfreeze.disabled` — bool, default `false`
  (hot-reloadable). Kill switch for the auto-unfreeze sweep
  (TB-320). True short-circuits `_maybe_auto_unfreeze` entirely.
  Mirrors `AP2_AUTO_UNFREEZE_DISABLED`.
- `components.auto_unfreeze.dry_run` — bool, default `false`
  (hot-reloadable). Monitor-only on-ramp (TB-233). When True
  alongside a non-empty `fix_shapes`, the sweep runs the entire
  guard chain but emits `would_auto_unfreeze` instead of patching
  the briefing. Mirrors `AP2_AUTO_UNFREEZE_DRY_RUN`.
- `components.auto_unfreeze.fix_shapes` — str, default `""`
  (hot-reloadable). Comma-separated allowlist of fix-shape tokens
  (TB-225). Non-empty enables auto-unfreeze attempts for the listed
  shapes; default empty means the feature is off. Mirrors
  `AP2_AUTO_UNFREEZE_FIX_SHAPES`.
- `components.auto_unfreeze.max_per_day` — int, default `3`
  (hot-reloadable). Per-day cap on total auto-unfreeze applications
  across all tasks (rolling 24h window). Mirrors
  `AP2_AUTO_UNFREEZE_MAX_PER_DAY`; 0 disables the cap.
- `components.auto_unfreeze.max_per_task` — int, default `1`
  (hot-reloadable). Per-task cap on auto-unfreeze applications
  across the rolling 24h window (TB-225). A task that's been
  auto-unfrozen `max_per_task` times falls back to manual `ap2
  unfreeze`. Mirrors `AP2_AUTO_UNFREEZE_MAX_PER_TASK`; 0 disables
  the cap.

### Ideation-exhaustion halt — now core (TB-226 / collapsed TB-342 / merged to core TB-345)

Emits `roadmap_complete` to park the ideation trigger when ideation
has produced enough consecutive empty cycles to suggest exhaustion.
TB-342 collapsed the pre-existing multi-focus rotation pointer walk
into this single halt; TB-345 merged the former `focus_advance`
component into the core `ap2/ideation_halt.py` module
(`maybe_halt_on_exhaustion`), called directly from the daemon's
PRE_DISPATCH phase rather than via the registry. The two tunables
therefore live under `[core.*]` above (`core.ideation_halt_disabled`
and `core.ideation_halt_empty_cycles`), NOT under a
`[components.focus_advance.*]` sub-table. The operator's resume path
is editing `goal.md` via `ap2 update-goal`.

### `[components.janitor]` — daemon-housekeeping cron (TB-309)

Repo-state sweeps (stale lockfiles, oversized debug dirs, etc.). The
chore-judge LLM call runs against findings the heuristic walk
identifies.

- `components.janitor.disabled` — bool, default `false`. Kill
  switch for the janitor cron job. True suppresses every
  `run_janitor` invocation (CLI status block keeps showing the
  component as off). Mirrors `AP2_JANITOR_DISABLED`; TB-323 wires
  the env-override back-compat map so the env var keeps overriding
  the TOML value during the migration window.
- `components.janitor.judge_effort` — str, default `"high"`
  (hot-reloadable). Per-judge effort label passed as
  `extra_args={"effort": <value>}` to the SDK options for each
  finding's judge call (TB-178). Falls back to `AP2_AGENT_EFFORT`
  then to `"high"` when unset. Mirrors `AP2_JANITOR_JUDGE_EFFORT`;
  read fresh at each judge call via `cfg.get_component_value`
  (TB-330).
- `components.janitor.judge_max_turns` — int, default `12`
  (hot-reloadable). Per-judge `ClaudeAgentOptions.max_turns` cap
  for the per-finding judge call (TB-178). Operators who want a
  tighter or looser budget can override. Mirrors
  `AP2_JANITOR_JUDGE_MAX_TURNS`; read fresh at each judge call via
  `cfg.get_component_value` (TB-330).
- `components.janitor.max_findings_llm` — int, default `10`
  (hot-reloadable). Per-run cap on LLM judge calls (TB-178). A
  scan with N candidate findings issues at most `min(N, cap)` SDK
  calls; findings beyond the cap emit with `verdict="ambiguous"`.
  Set to 0 to disable the judge entirely (deterministic-only
  fallback). Mirrors `AP2_JANITOR_MAX_FINDINGS_LLM`; read fresh at
  each janitor cron run via `cfg.get_component_value` (TB-330).

### `[components.communication]` — channel surface (inbound + outbound)

The `communication` component (TB-389) owns the channel surface in
both directions: `@bot`-mention polling on the inbound side and
routing outbound digests / status-reports / attention pushes on the
outbound side. Mattermost was demoted from a top-level component to
a channel adapter the communication component holds in an internal
registry, so these Mattermost channel knobs are now channel-level
config under `[components.communication]`. Channel + bot identity
are authentication-bearing; they sit on the env-only side per
goal.md L356-358 (the `_KNOBS_STAYING_ENV_ONLY` partition).

- `components.communication.bot_user_id` — str, default `""`.
  Mattermost user ID for the bot account; used to filter the bot's
  own posts out of the inbound poll. Mirrors `AP2_MM_BOT_USER_ID`;
  not in `HOT_RELOADABLE_KNOBS`.
- `components.communication.channels` — str, default `""`.
  Comma-separated Mattermost channel IDs the communication component
  polls for inbound mentions and posts outbound messages to.
  Unset/empty leaves the Mattermost channel inactive (channel-level
  activation, TB-389). Mirrors `AP2_MM_CHANNELS`; listed in
  `env_reload.FIXED_KNOBS` so a change requires `ap2 stop && ap2 start`.
- `components.communication.mention` — str, default `"@claude-bot"`.
  Mention token (e.g. `@claude-bot`) the bot recognizes as
  addressing it in poll content. Mirrors `AP2_MM_MENTION`; not in
  `HOT_RELOADABLE_KNOBS`.

> **Note (TB-386):** the briefing dep-coherence judge (TB-235, Haiku
> check that identifies hard-predecessor dependencies a briefing's prose
> implies but its `@blocked:` codespan omits) was demoted out of the
> `validator_judge` component back into the core briefing-validation
> runner (`ap2/briefing_validators.py`). It is a sub-step of
> `_validate_briefing_structure`, not a loop-level component, so it no
> longer has a `[components.validator_judge]` TOML block. Its off-switch
> and tunables survive as plain env-only knobs —
> `AP2_VALIDATOR_JUDGE_DISABLED`, `AP2_VALIDATOR_JUDGE_TIMEOUT_S`,
> `AP2_VALIDATOR_JUDGE_MAX_TURNS`, `AP2_VALIDATOR_JUDGE_MAX_TOKENS` —
> documented in the `## Configuration knobs` section above.

### `[agent_backends]` — per-agent-kind backend map (TB-358)

A top-level table (not `[core.*]` / `[components.*]`) mapping an agent
kind to its backend id, consumed verbatim by `config_loader.from_toml`
into `Config.agent_backends_config`. Keys are the `AGENT_KINDS`
(`task`, `ideation`, `status_report`, `cron`, `mattermost`,
`verifier_judge`, `ideation_scrub`, `validator_judge`,
`janitor_judge`); values are `"claude"` (default) or `"codex"`. Omit
the table entirely to keep every kind on claude.

    [agent_backends]
    task = "codex"          # run task agents on Codex …
    ideation = "claude"     # … while ideation stays on Claude

Resolution precedence (with the `AP2_AGENT_BACKEND_<KIND>` env override
winning over this table) and the per-backend credential requirement are
documented under **Agent backend selection** + **Daemon-start auth
gate** in `## Configuration knobs` above. Because `[agent_backends]` is
not a declared `ConfigKey`, it is exempt from the
`test_every_config_key_documented` drift gate and is not rendered into
the `ap2 init` config-template scaffold.


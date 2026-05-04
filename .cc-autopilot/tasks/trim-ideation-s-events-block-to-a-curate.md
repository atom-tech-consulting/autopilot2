# Trim ideation's `_events_block` to a curated allowlist of event types

## Goal

Sharpen ideation's prompt signal density by filtering the in-prompt events block to event types ideation actually uses, rather than the full unfiltered tail of `events.jsonl`.

Today `_events_block` (`ap2/prompts.py:292-296`) injects the last `AP2_EVENT_CONTEXT=50` events (default 50, capped at 6KB by `events.format_for_prompt`'s `max_chars`) into every control-agent prompt. Empirical observation from `events.jsonl`: `judge_call` events (TB-157) carry full per-call token-usage payloads (~2KB each — `usage`, `model_usage`, `cache_creation_input_tokens`, etc.), so 3-4 of them blow the 6KB budget and crowd out the lifecycle events ideation actually keys off in Step 1 (follow-up discovery from recent completes), Step 1.5 (failure review keyed on `verification_failed` / `retry_exhausted` / `verification_partial`), and the `cron_proposed`-surfacing rule.

Concrete useless-for-ideation event types in today's tail: `judge_call`, `status_report`, `cron_skipped`, `cron_complete`, `cron_start`, `mattermost_reply`, `daemon_start`/`stop`/`pause`/`resume`, `web_start`/`stop`, `operator_queue_append`/`drained`, `backlog_auto_promoted`, `task_run_usage` (post-TB-165), `control_run_usage` (post-TB-166), `pending_review_reminder`, `auto_diagnose_fired`, `ideation_state_updated`. Each is observability or daemon-plumbing — none of it changes ideation's proposals.

This change is **opt-in via kwargs** so status-report cron (which DOES summarize all activity) keeps the unfiltered view it needs.

Why now: this is the second of three independent ideation-prompt-trim TBs (TB-168 dropped board+commits from `_current_state_block`; this one trims `_events_block`; a third will drop CLAUDE.md from the read-order). All advance the goal.md "Current focus: ideation quality" — specifically the prompt-shape work the focus section calls out — and each is independently mergeable so a regression in any one doesn't block the others. Filing now (rather than as a bundle) keeps blast radius small per the operator's stated preference for incremental signal-density work.

## Scope

- `ap2/prompts.py::_events_block` — add `include_types: list[str] | None = None` kwarg. When `None` (default), no filter (existing behavior preserved for status-report). When a list, drop any event whose `type` field isn't in the allowlist BEFORE passing to `events.format_for_prompt`.
- `ap2/prompts.py::build_control_prompt` — accept and forward `include_types` to `_events_block`.
- `ap2/ideation.py` — define a module-level constant `IDEATION_RELEVANT_EVENT_TYPES: tuple[str, ...]` enumerating the allowlist (see Design). `_maybe_ideate` passes it through `build_control_prompt(..., include_types=IDEATION_RELEVANT_EVENT_TYPES)`.
- New tests in `ap2/tests/test_prompts.py` and `ap2/tests/test_ideation*.py`.

## Design

### Allowlist contents

Allowlist (not denylist) keeps the surface explicit and stable — new event types added in future TBs default to *exclusion* unless someone consciously adds them. New event types are typically observability/plumbing (the recent `task_run_usage` / `control_run_usage` are exactly that pattern), which is what we want excluded by default.

```python
IDEATION_RELEVANT_EVENT_TYPES = (
    # Task lifecycle — Step 1 follow-up discovery + Step 1.5 failure review
    "task_complete",
    "verification_failed",
    "verification_partial",
    "retry_exhausted",
    "task_state_violation",
    # Operator decisions — cross-cycle context for "what was approved/rejected/edited"
    "ideation_approved",
    "task_deleted",
    "task_updated",
    # Cron proposals — explicit surfacing rule in ideation.default.md
    "cron_proposed",
)
```

Rationale per entry:

- **`task_complete`** — primary signal for follow-up discovery (Step 1). Carries `status` field distinguishing clean complete vs. `verification_failed`/`retry_exhausted` outcomes.
- **`verification_failed`** — carries the per-bullet `criteria` array (TB-158); ideation uses it in Step 1.5 to decide edit-briefing / split / follow-up / abandon.
- **`verification_partial`** — explicitly named in Step 1.5 (`unverified` prose bullets pattern).
- **`retry_exhausted`** — failure-review trigger (Frozen tasks).
- **`task_state_violation`** — rare but high-signal: a task agent tried to mutate a fenced file, indicating a briefing/scope problem.
- **`ideation_approved`** — operator approved a prior ideation proposal; chronological context for "which of my prior proposals shipped."
- **`task_deleted`** — operator deletion of a task (pre-TB-152 implicit rejection; post-TB-152 `reject` path emits this AND writes operator_log.md).
- **`task_updated`** — operator updated a briefing (signal that ideation's original proposal needed edits).
- **`cron_proposed`** — explicit ideation.default.md rule: "If you see one or more unadopted `cron_proposed` events in the recent-events block, SURFACE them in your per-cycle assessment."

NOT in the allowlist (notable omissions, in case the implementer second-guesses):

- `task_start` — TASKS.md `## Active` section already shows in-flight tasks; events.jsonl line is redundant.
- `task_run_usage`, `control_run_usage`, `judge_call` — pure cost/token observability; ideation has no input on cost-per-task.
- `ideation_empty_board`, `ideation_timeout`, `ideation_error`, `ideation_state_updated` — meta about prior ideation runs; the agent's cross-cycle memory is `ideation_state.md`, not events.jsonl.
- `mattermost_reply`, `mattermost_thread_read`, `pending_review_reminder` — chat-side; nothing for ideation to act on.
- `cron_start` / `cron_complete` / `cron_skipped` — cron lifecycle; ideation cares about `cron_proposed` (above), not the cron job's own runs.
- `daemon_*`, `web_*`, `auto_diagnose_*`, `backlog_auto_promoted`, `operator_queue_*` — daemon plumbing.
- `status_report` — control-agent's own output.

### Window semantics

Filter applies to the tailed window, not before it. Today `events.tail(cfg.events_file, n=50)` returns the last 50 events; with the filter, ideation sees only matching events from those 50 (typically 10-25 out of 50 in current daemon traffic). This is intentional v1 simplicity — if "20 events isn't enough recency for failure review" turns out to be a real complaint, a follow-up TB can either (a) widen `n` for ideation specifically, or (b) walk back further until N matching events are gathered. Don't pre-optimize.

### Backwards compatibility

- `_events_block` and `build_control_prompt` take a new keyword arg with default `None` (no filter). All existing callers (`daemon.run_cron`, `status_report.run_status_report`, any future control agents) get unchanged behavior.
- Ideation is the only opt-in caller this round.

### Pairing with TB-168

TB-168 trims `_current_state_block` (drops `board:` + `recent commits` for ideation). This TB trims `_events_block` (filters by type for ideation). Both touch `build_control_prompt`'s signature additively (new kwargs, default-preserve-behavior); they don't conflict and can land in either order.

## Verification

- `uv run pytest -q ap2/tests/` — full regression gate passes.
- `grep -nE "include_types" ap2/prompts.py` — kwarg is wired in `_events_block` AND `build_control_prompt`.
- `grep -nE "IDEATION_RELEVANT_EVENT_TYPES" ap2/ideation.py` — constant is defined.
- `python3 -c "from ap2.ideation import IDEATION_RELEVANT_EVENT_TYPES; assert 'task_complete' in IDEATION_RELEVANT_EVENT_TYPES; assert 'judge_call' not in IDEATION_RELEVANT_EVENT_TYPES; assert 'verification_failed' in IDEATION_RELEVANT_EVENT_TYPES"` — allowlist contains expected entries and excludes the noisy ones.
- prose: a test in `test_prompts.py` synthesizes a small `events.jsonl` with one event of every IDEATION_RELEVANT_EVENT_TYPES kind PLUS three noise kinds (`judge_call`, `status_report`, `cron_complete`); calls `_events_block(cfg, include_types=IDEATION_RELEVANT_EVENT_TYPES)`; asserts the rendered string contains EVERY relevant kind's `type=` rendering and NONE of the noise kinds.
- prose: a test in `test_prompts.py` calls `_events_block(cfg)` (no kwarg, defaults) against the same fixture; asserts ALL events render (status-report behavior unchanged).
- prose: a test in `test_ideation*.py` invokes `_maybe_ideate` with a stubbed SDK that captures the prompt sent — assert the captured prompt's `## Recent events` block contains at least one `task_complete` line and zero `judge_call` lines, given a fixture seeded with both.
- prose: an empty-after-filter case — when no events of relevant types exist in the tail, the rendered block is `## Recent events\n(none yet)\n` (the existing empty-tail fallback behavior, reused).

## Out of scope

- Trimming `_current_state_block` (TB-168 covers this).
- Removing CLAUDE.md from ideation's read-order (separate follow-up TB).
- Widening `events.tail`'s `n` for ideation specifically. v1 keeps `n=50` (or `AP2_EVENT_CONTEXT`); if filtered window is too sparse, a follow-up TB widens.
- Walk-back-until-N-matching-events strategy (alternative window semantic). v1 filters within the existing `n`-window only.
- Per-event-kind sub-formatters (e.g. compact `judge_call` rendering instead of full payload). Filtering it OUT for ideation is sufficient; status-report doesn't care about per-event size since it doesn't pay per token at the same scale.
- Making the allowlist operator-tunable via env. The set is code-level — a new event type either matters to ideation's instructions or it doesn't, and that decision belongs in the codebase, not in `.cc-autopilot/env`.
- Renaming or restructuring the existing `## Recent events` heading. The block boundary stays the same; only the contents change.
- Updating `ap2/ideation.default.md` body to explain the filtering. The agent doesn't need to know — it just sees a smaller, more relevant tail. Mention it in the constant's docstring for future maintainers.

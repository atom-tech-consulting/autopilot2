# TB-289 — `auto_approve_paused` attention detector (closes "pending decision" leg of Progress signal #3)

## Goal

Add an attention detector to `ap2/attention.py` — `_detect_auto_approve_paused` — that returns a single `AttentionCondition` when `collect_auto_approve_state(cfg).pause_reason` is non-None (today: `consecutive_freezes` or `validator_judge_noisy`; any future siblings registered via `_PAUSE_REASON_ACK_VERB` land here too). Closes the "pending decision" leg of Current focus: operator-legible reporting and monitoring Progress signal #3 ("Attention-needing conditions ... surfaced proactively in operator-legible terms, distinct from routine progress updates"), which TB-282 deliberately deferred via its Out-of-scope clause naming `decisions_needed_new` as one of the obvious follow-ups (see `ap2/attention.py` L29-32).

Why now: today an active auto-approve pause appears only as one line in the TB-228 automation-digest sub-block (`auto-approve: disabled (paused: <reason>)`) positioned AFTER the body bullets in the status-report, and as `auto-approve: disabled (paused: ...)` text in `ap2 status`. The operator must scroll past the headline + 4-8 routine bullets + automation digest header to find the pause line — and must already know to run `ap2 ack auto_approve_unfreeze` to resume. The Progress signal explicitly contrasts "distinct from routine progress updates" — a paused auto-approve IS a pending decision (the operator's `ack` is the only path back to dispatch). This is exactly the "buried in periodic report" failure mode goal.md L207-209 names.

## Scope

- `ap2/attention.py`: add `_detect_auto_approve_paused(cfg, *, tail, now)` returning `list[AttentionCondition]` (zero or one element). Calls `collect_auto_approve_state(cfg)` and reads `.pause_reason` / the matching ack verb from `_PAUSE_REASON_ACK_VERB` in `ap2/automation_status.py`. Wire into `detect_attention_conditions` via a fourth `out.extend(...)` call.
- `ap2/tests/test_tb289_attention_auto_approve_paused.py`: no-fire when `pause_reason is None`, fires-on-consecutive-freezes (with the consecutive-freeze count surfaced in the summary), fires-on-validator-judge-noisy (with the noisy count surfaced), per-reason dedup key (`auto_approve_paused:<reason>` so a sequential reason transition both surface), no-fire when auto-approve is disabled via `AP2_AUTO_APPROVE=0` (disabled is not paused — distinct states), debounce respected across consecutive ticks within `AP2_ATTENTION_DEBOUNCE_S`.
- `ap2/howto.md` and `ap2/architecture.md`: add `auto_approve_paused` to the attention-detector inventory line(s).

## Design

- `type="auto_approve_paused"`, `key=f"auto_approve_paused:{pause_reason}"` — per-reason dedup so a transition from `consecutive_freezes` to `validator_judge_noisy` surfaces both (distinct conditions, distinct keys).
- `summary` shape: `f"auto-approve paused: {pause_reason}; resume via `ap2 ack {ack_verb}`"`. The `ack_verb` resolves via `_PAUSE_REASON_ACK_VERB[pause_reason]` (today both reasons map to `auto_approve_unfreeze`; this preserves operator muscle-memory per TB-272's design choice at `ap2/auto_approve.py` L69-83).
- `extras={"pause_reason": pause_reason, "ack_verb": ack_verb, "consecutive_freezes": consecutive, "validator_judge_fail_count_24h": fail, "validator_judge_timeout_count_24h": timeout}` — extras reuse the count fields already populated by `collect_auto_approve_state` so the event-stream reader has the diagnostic context inline.
- The detector is read-only against `automation_status.collect_auto_approve_state`; it does NOT replace TB-272's pause logic or TB-228's automation-digest line — both remain. The Attention bullet is additive: a paused state today appears (a) in `ap2 status`, (b) in the TB-228 sub-block, (c) on the web automation card. After this lands, (d) appears as a `## Attention needed` bullet at the TOP of the status-report.

## Verification

- `uv run pytest -q ap2/tests/test_tb289_attention_auto_approve_paused.py` — new test module passes (≥6 tests covering the scenarios above).
- `uv run pytest -q ap2/tests/` — full suite passes (no regressions; TB-272 pause logic and TB-228 automation digest remain intact).
- `grep -q "_detect_auto_approve_paused" ap2/attention.py` — detector function present.
- `grep -q "auto_approve_paused" ap2/howto.md` — detector named in the inventory.
- `grep -q "auto_approve_paused" ap2/architecture.md` — detector named in the architecture map.
- `grep -rq "_detect_auto_approve_paused" ap2/tests/` — test references the detector by name (drift-gate coverage).

## Out of scope

- Adding new pause_reasons (e.g., `cost_cap_approach`) — requires upstream cost-cap threshold infrastructure that does not yet exist; deferred in this cycle's `ideation_state.md` until the cost-tracking surface declares its operating envelope.
- Auto-resume from pause without operator ack — operator owns the trust-restoration decision per goal.md L253-256 "Unconditional automation" non-goal.
- MM-handler chat verb plumbing — `ap2 ack` already exists as an operator-queue verb (operator_log.md shows regular use); chat-side parity covered by existing TB-176-shape verb mapping.
- Changing the TB-228 automation-digest line or the `ap2 status` pause text — both remain; this task is purely additive to the proactive surface.

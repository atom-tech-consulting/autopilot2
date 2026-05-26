# TB-288 — `validator_judge_noisy` attention detector (promote axis-1 noisy threshold to the Attention surface)

## Goal

Add a second attention detector to `ap2/attention.py` — `_detect_validator_judge_noisy` — returning a single `AttentionCondition` when the rolling 24h count of `validator_judge_fail` + `validator_judge_timeout` events is ≥ `AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD` (the existing knob `validator_judge_noisy_threshold()` resolves at `ap2/automation_status.py` L169-173, default 5). Closes the "validator-judge anomalies" leg of Current focus: operator-legible reporting and monitoring Progress signal #3 ("Attention-needing conditions ... surfaced proactively in operator-legible terms, distinct from routine progress updates"), which TB-282 deliberately deferred via its Out-of-scope clause naming `validator_judge_noisy` as one of the obvious follow-ups (see `ap2/attention.py` L29-32).

Why now: TB-243 surfaced 24h fail-counts in `ap2 status` text/JSON + web automation card; TB-245 forwards a bottom-of-digest sub-block into the status-report; TB-272 added `validator_judge_noisy` as an auto-approve pause_reason. All three are pull/sub-block surfaces — the noisy state is buried inside the digest near the end of the post, NOT in the daemon-rendered `## Attention needed` block that TB-282 places ABOVE the body bullets. The Progress signals bullet explicitly contrasts "distinct from routine progress updates" — today the noisy state IS tied to routine sub-blocks. Promoting to Attention closes the visual-hierarchy gap without changing the underlying count infrastructure.

## Scope

- `ap2/attention.py`: add `_detect_validator_judge_noisy(cfg, *, tail, now)` returning `list[AttentionCondition]` (zero or one element). Reuse the existing 24h-window walk from `ap2/automation_status.py::collect_auto_approve_state` (or call a small helper that shares the count logic) so the detector and the automation-card surface never drift.
- `ap2/tests/test_tb288_attention_validator_judge_noisy.py`: count-below-threshold (no fire), count-at-threshold (one condition), count-above-threshold (still one condition — singleton, not per-event), debounce respected across consecutive ticks within `AP2_ATTENTION_DEBOUNCE_S`, threshold-override-via-env (`AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD=1` fires on a single event).
- `ap2/howto.md` and `ap2/architecture.md`: add `validator_judge_noisy` to the attention-detector inventory line(s) alongside `task_stuck`.

## Design

- `type="validator_judge_noisy"`, `key="validator_judge_noisy"` — singleton condition (one such state at a time across the daemon). Per-(type,key) debounce in the existing `should_suppress` helper (attention.py L302-326) handles "still noisy at next tick" without re-firing.
- `summary` shape: `f"validator-judge noisy: {fail_count}+{timeout_count}={total} fails+timeouts in last 24h (threshold {threshold}); see /usage or `ap2 status`"`.
- `extras={"fail_count_24h": fail_count, "timeout_count_24h": timeout_count, "threshold": threshold, "window_s": 86400}`.
- The detector does NOT modify TB-272's pause_reason or TB-243's `ap2 status` text — both surfaces remain. The new Attention bullet is additive: a noisy state today appears (a) as `[noisy]` suffix in `ap2 status`, (b) as TB-245 sub-block in the status-report, (c) as TB-243 row in the web automation card. After this lands, (d) appears as a `## Attention needed` bullet at the TOP of the status-report post.

## Verification

- `uv run pytest -q ap2/tests/test_tb288_attention_validator_judge_noisy.py` — new test module passes (≥5 tests covering the scenarios above).
- `uv run pytest -q ap2/tests/` — full suite passes (no regressions; TB-272/TB-243/TB-245 surfaces remain intact).
- `grep -q "_detect_validator_judge_noisy" ap2/attention.py` — detector function present.
- `grep -q "validator_judge_noisy" ap2/howto.md` — detector named in the inventory.
- `grep -q "validator_judge_noisy" ap2/architecture.md` — detector named in the architecture map.
- `grep -rq "_detect_validator_judge_noisy" ap2/tests/` — test references the detector by name (drift-gate coverage for the new detector identifier).

## Out of scope

- Recalibrating `AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD` default — separate cycle; requires the post-TB-269/270 ≥7d window of production data (deferred in this cycle's ideation_state.md per the carry-over from prior cycles).
- Changing TB-243's automation-card row or TB-245's status-report sub-block — both remain; this task is additive.
- Modifying TB-272's pause_reason behavior — auto-approve pausing on noisy state is a separate axis-3 safety floor and the Attention bullet is independent (a noisy state can fire even when auto-approve is disabled).
- Auto-suppression / auto-disable of the validator-judge gate on extreme noise — operator-judgement territory.

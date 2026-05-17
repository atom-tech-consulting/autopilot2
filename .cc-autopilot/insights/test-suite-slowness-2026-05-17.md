---
title: Test-suite slowness investigation — 2026-05-17
tags: [tests, performance, investigation, code-quality]
status: investigation (TB-253 deliverable; no fixes applied)
---

# Test-suite slowness investigation — 2026-05-17

Measurement window: `uv run pytest -q ap2/tests/ --durations=20` against
HEAD on 2026-05-17.

**Suite total: 1734 passed in 1336.06s (~22:16).**

Captured because the 2026-05-17 n=5 retry-exhaust cascade (TB-245, 246,
247, 249, 250) all hit the 600s `AP2_VERIFY_TIMEOUT_S` wall. The
operator bumped the knob to 1800s to unblock, but the per-task
verification cost remains the silent overhead paid on every dispatch
under `AP2_AUTO_APPROVE=1`. This file is the TB-253 investigation
artifact: a categorized top-20 with first-pass rationale per test so
follow-up TBs have data to scope against. **No fixes applied here.**

## Category legend

The four buckets (briefing's Scope §2):

- **essential-slow** — test genuinely needs the wall-clock (real SDK
  call per `AP2_REAL_SDK=1`, e2e end-to-end task lifecycle, heavy
  fixture setup that can't be amortized).
- **fixable-slow** — concrete refactor would reduce duration: excessive
  `time.sleep`, redundant per-test fixture setup, missed
  `@pytest.fixture(scope="module")` opportunity, expensive subprocess
  that could be mocked, accidental real-SDK call inside a unit test
  that should be stubbed.
- **candidate-for-removal** — redundant with other coverage (overlapping
  unit + e2e pair, deprecated path under test, low-signal regression-pin
  that doesn't pay rent per goal.md's delete-test rule).
- **investigate-further** — agent couldn't categorize confidently;
  flagged for operator decision.

Operator can re-classify any row during follow-up review.

## Headline finding

**18 of the top-20 slowest tests are slowed by the same root cause:
they exercise `tools.do_board_edit({"action": "add_*"})` or
`tools.do_operator_queue_append({"op": "add_*"})` in unit-test files
that do NOT set `AP2_VALIDATOR_JUDGE_DISABLED=1`.** The `add_*` path
runs `_validate_briefing_structure → _check_dependency_coherence`,
which dispatches a real Haiku-4.5 SDK call per add (timeout default
15s; in practice 10–18s per call). Tests that iterate over the three
add_* variants (`add_ready` / `add_backlog` / `add_frozen`) pay
~30–40s of cumulative judge latency before any of the real assertions
run. Tests that seed two auto-approved Backlog tasks pay ~20–32s.

`ap2/tests/e2e/conftest.py` (line 66) sets
`AP2_VALIDATOR_JUDGE_DISABLED=1` for the entire e2e directory,
exactly to avoid this fail-mode (the conftest docstring spells it
out: "would dispatch a real Haiku-4.5 SDK call (slow, costly, and
potentially makes a live API call from CI)"). The unit-test files at
`ap2/tests/test_*.py` have no equivalent shield — each
`do_board_edit({add_*})` call goes live to the SDK. The
test_dep_validator_judge.py and test_tb243_validator_judge_surface.py
modules disable it locally because they explicitly stub the judge;
every other module accidentally inherits the production default.

The likely follow-up shape (separate TBs per operator's call):

  1. Add a top-level `ap2/tests/conftest.py` that
     `monkeypatch.setenv("AP2_VALIDATOR_JUDGE_DISABLED", "1")` by
     default, leaving e2e's conftest + the two explicit
     judge-exercising modules unchanged. Mirrors the e2e shield exactly
     and is the smallest blast-radius fix.
  2. OR: replace each top-20 test's `cfg` fixture body with one that
     calls `monkeypatch.setenv("AP2_VALIDATOR_JUDGE_DISABLED", "1")`.
     Larger diff, narrower scope.
  3. OR: pass an explicit `dep_judge_fn` stub through the
     `do_board_edit` call sites in tests (the function signature already
     supports it — see `_check_dependency_coherence`'s `judge_fn` kwarg
     at `ap2/tools.py:1083`).

Option 1 is most surgical; the artifact's value is in identifying the
class, not picking the fix.

Projected savings if all 18 fixable-slow tests drop ~10–30s each:
**~300–500s shaved off the 1336s suite total** (roughly the same magnitude
as the operator's 1800s timeout bump headroom). The aggregate-percentages
table below quantifies this per category.

The two non-conforming rows in the top-20:

- One `test_tb135_verification.py` entry is a near-duplicate of the
  `test_tools.py::test_board_edit_non_empty_briefing_payload_unaffected_for_daemon_callers`
  entry — same three add_* iterations, same assertions about
  task_id-shape + section landing. Flagged **candidate-for-removal**
  (keep test_tools.py's row; TB-135's verification module pinned this
  shape historically but the test_tools.py row is the durable home).
- All other rows are **fixable-slow** under the same root cause.

## Top-20 slowest tests

Raw pytest `--durations=20` output (duration-prefixed, matching the
captured invocation verbatim so the operator can `diff` against a
future re-run):

```
37.81s call     ap2/tests/test_tools.py::test_operator_queue_append_non_empty_briefing_payload_succeeds
33.41s call     ap2/tests/test_tb135_verification.py::test_tb135_tools_do_board_edit_non_empty_briefing_payload_still_succeeds
32.27s call     ap2/tests/test_tb224_token_caps.py::test_window_cap_only_counts_auto_approved_tasks
31.68s call     ap2/tests/test_tb224_token_caps.py::test_unset_window_cap_does_not_halt
28.04s call     ap2/tests/test_tools.py::test_board_edit_non_empty_briefing_payload_unaffected_for_daemon_callers
20.70s call     ap2/tests/test_tb224_token_caps.py::test_task_error_single_event_halts_auto_promote
20.02s call     ap2/tests/test_tb223_auto_approve.py::test_tick_halts_auto_promote_when_freeze_active
20.01s call     ap2/tests/test_ideation_proposals.py::test_record_round_trips_mixed_blocker_csv
19.65s call     ap2/tests/test_tb224_token_caps.py::test_ack_window_resume_clears_task_error_halt
19.17s call     ap2/tests/test_tb233_auto_unfreeze_dry_run.py::test_a_dry_run_emits_would_event_and_leaves_state_untouched
19.00s call     ap2/tests/test_tb223_auto_approve.py::test_custom_gate_tag_list_overrides_default
18.66s call     ap2/tests/test_operator_queue.py::test_drain_failure_in_one_op_doesnt_halt_others
18.56s call     ap2/tests/test_cli.py::test_add_with_blocked_writes_codespan_not_description
18.41s call     ap2/tests/test_operator_queue.py::test_drain_emits_drained_event_with_count
18.18s call     ap2/tests/test_tb224_token_caps.py::test_ack_window_resume_clears_window_cap_halt
17.67s call     ap2/tests/test_operator_queue.py::test_cmd_status_surfaces_pending_queue_depth
17.10s call     ap2/tests/test_operator_queue.py::test_drain_compacts_queue_file_after_apply
16.99s call     ap2/tests/test_tools.py::test_board_edit_add_frozen_still_honors_blocked_on
16.76s call     ap2/tests/test_tb224_token_caps.py::test_unset_per_task_cap_does_not_halt
16.53s call     ap2/tests/test_tools.py::test_board_edit_add_backlog_honors_blocked_on
```

Same data, joined with category + rationale for operator review:

| # | duration | test | category | rationale |
|---|----------|------|----------|-----------|
| 1 | 37.81s call | `ap2/tests/test_tools.py::test_operator_queue_append_non_empty_briefing_payload_succeeds` | fixable-slow | Iterates 3 add_* via `do_operator_queue_append`; each triggers `_check_dependency_coherence` → real Haiku SDK call (~12s × 3). Disable via top-level conftest or local `AP2_VALIDATOR_JUDGE_DISABLED=1`. |
| 2 | 33.41s call | `ap2/tests/test_tb135_verification.py::test_tb135_tools_do_board_edit_non_empty_briefing_payload_still_succeeds` | candidate-for-removal | Near-duplicate of row 5 below (test_tools.py's `…unaffected_for_daemon_callers`). Same 3-action iteration, same shape assertions. TB-135 pinning is historical; durable home is test_tools.py. |
| 3 | 32.27s call | `ap2/tests/test_tb224_token_caps.py::test_window_cap_only_counts_auto_approved_tasks` | fixable-slow | `_seed_auto_approved_task` twice → 2× validator-judge calls (~16s each). Tighten `cfg` fixture to disable judge. |
| 4 | 31.68s call | `ap2/tests/test_tb224_token_caps.py::test_unset_window_cap_does_not_halt` | fixable-slow | `_seed_auto_approved_task` twice → 2× validator-judge. Same fix shape as row 3. |
| 5 | 28.04s call | `ap2/tests/test_tools.py::test_board_edit_non_empty_briefing_payload_unaffected_for_daemon_callers` | fixable-slow | Iterates 3 add_* via `do_board_edit`; 3× validator-judge. (See row 2 — keep this one as durable home, drop row 2.) |
| 6 | 20.70s call | `ap2/tests/test_tb224_token_caps.py::test_task_error_single_event_halts_auto_promote` | fixable-slow | 1× `_seed_auto_approved_task` (validator-judge) + stubbed tick. Disable judge → drops to a few seconds. |
| 7 | 20.02s call | `ap2/tests/test_tb223_auto_approve.py::test_tick_halts_auto_promote_when_freeze_active` | fixable-slow | 1× `do_board_edit({add_backlog})` (validator-judge) + 3× completion seeding + tick. Disable judge in cfg fixture. |
| 8 | 20.01s call | `ap2/tests/test_ideation_proposals.py::test_record_round_trips_mixed_blocker_csv` | fixable-slow | 1× `do_board_edit({add_backlog})` (validator-judge) + JSON read of proposal record. Disable judge → drops to ms. |
| 9 | 19.65s call | `ap2/tests/test_tb224_token_caps.py::test_ack_window_resume_clears_task_error_halt` | fixable-slow | Same pattern as row 6 — `_seed_auto_approved_task` + tick. |
| 10 | 19.17s call | `ap2/tests/test_tb233_auto_unfreeze_dry_run.py::test_a_dry_run_emits_would_event_and_leaves_state_untouched` | fixable-slow | `_add_and_freeze` → `do_operator_queue_append({add_backlog})` + drain triggers validator-judge. Full dry-run logic is fast; judge dominates. |
| 11 | 19.00s call | `ap2/tests/test_tb223_auto_approve.py::test_custom_gate_tag_list_overrides_default` | fixable-slow | 2× `do_board_edit({add_backlog})` (one per gate-tag variant) → 2× validator-judge. Disable in cfg fixture. |
| 12 | 18.66s call | `ap2/tests/test_operator_queue.py::test_drain_failure_in_one_op_doesnt_halt_others` | fixable-slow | 2× `do_operator_queue_append({add_backlog})` → 2× validator-judge before bogus-op drain logic runs. Disable judge. |
| 13 | 18.56s call | `ap2/tests/test_cli.py::test_add_with_blocked_writes_codespan_not_description` | fixable-slow | `cmd_add` → operator-queue append + drain → validator-judge fires on add_backlog. The TB-132 codespan assertion has nothing to do with the judge; disable it. |
| 14 | 18.41s call | `ap2/tests/test_operator_queue.py::test_drain_emits_drained_event_with_count` | fixable-slow | 2× `do_operator_queue_append({add_backlog})` → 2× validator-judge. Same fix as row 12. |
| 15 | 18.18s call | `ap2/tests/test_tb224_token_caps.py::test_ack_window_resume_clears_window_cap_halt` | fixable-slow | Same `_seed_auto_approved_task` + tick pattern as rows 6/9. |
| 16 | 17.67s call | `ap2/tests/test_operator_queue.py::test_cmd_status_surfaces_pending_queue_depth` | fixable-slow | 2× `do_operator_queue_append({add_backlog})` → 2× validator-judge before the `ap2 status` capsys assertion. |
| 17 | 17.10s call | `ap2/tests/test_operator_queue.py::test_drain_compacts_queue_file_after_apply` | fixable-slow | 2× `do_operator_queue_append({add_backlog})` → 2× validator-judge. The queue-compaction logic itself is fast. |
| 18 | 16.99s call | `ap2/tests/test_tools.py::test_board_edit_add_frozen_still_honors_blocked_on` | fixable-slow | 1× `do_board_edit({add_frozen})` → 1× validator-judge dominates the wall-clock; the TB-132 blocked-codespan assertion is sub-ms. |
| 19 | 16.76s call | `ap2/tests/test_tb224_token_caps.py::test_unset_per_task_cap_does_not_halt` | fixable-slow | 1× `_seed_auto_approved_task` + stubbed tick. Same fix shape as rows 6/9/15. |
| 20 | 16.53s call | `ap2/tests/test_tools.py::test_board_edit_add_backlog_honors_blocked_on` | fixable-slow | 1× `do_board_edit({add_backlog})` → 1× validator-judge. Same TB-132 codespan-pin pattern as row 18. |

## Aggregate counts

Sum of top-20 durations: **440.62s** (33.0% of the 1336.06s suite total —
so the remaining 1700+ tests share the other 67% of wall-clock).

| category | count | total seconds | % of suite (1336.06s) | % of top-20 (440.62s) |
|----------|-------|---------------|-----------------------|-----------------------|
| essential-slow       |  0 |   0.00s |  0.0% |  0.0% |
| fixable-slow         | 19 | 407.21s | 30.5% | 92.4% |
| candidate-for-removal |  1 |  33.41s |  2.5% |  7.6% |
| investigate-further  |  0 |   0.00s |  0.0% |  0.0% |
| **total**            | **20** | **440.62s** | **33.0%** | **100%** |

## Implications for follow-up TBs

A single per-test conftest change (Option 1 in the headline finding —
add `ap2/tests/conftest.py` with `AP2_VALIDATOR_JUDGE_DISABLED=1` as
the default for unit tests) would, conservatively, drop each
fixable-slow test by ~10–15s — the lower bound, since some rows hit
two judge calls and would save ~20–30s each. Projected suite-total
reduction: **~250–400s**, which would put the suite around ~950s
(15–16 min) and restore comfortable headroom under the operator's
1800s `AP2_VERIFY_TIMEOUT_S` ceiling.

Removing the candidate-for-removal duplicate (row 2) yields an
additional ~33s but is a delete-test decision per goal.md's "earn its
rent" rule; the test_tools.py row 5 covers the same surface so the
TB-135 pinning module would lose nothing operationally.

Whether the validator-judge ought to be opt-out (current) vs opt-in
for unit tests is a policy call this artifact deliberately does not
make — it's a separate decision per the briefing's "Out of scope"
list. The investigation surfaces the cost; the operator picks the
fix.

## What's NOT in this artifact

Per the TB-253 briefing's Out-of-scope list:

- Applying any fixes. This file is the deliverable; per-category fix
  TBs follow.
- Changes to pytest configuration (pyproject.toml, conftest.py,
  pytest.ini). Even though the headline finding strongly suggests a
  conftest fix, that lands as a separate TB.
- Splitting the suite or adopting `pytest-xdist`. Orthogonal
  structural decision.
- Modifying `AP2_REAL_SDK=1` default-on policy. Separate operator
  policy decision.

Re-run cadence: this is a one-shot snapshot. If suite runtime grows
further the operator runs another TB-253-shaped investigation; the
2026-05-17 datestamp keeps this artifact useful as a historical
baseline rather than something that gets overwritten.

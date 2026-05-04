# Tier-1 token tuning: diff trim + per-agent effort lowering

## Goal

Three small token-saving changes, bundled because each is one or two lines and they share the same testing surface. Combined estimated saving: ~50% of judge tokens + ~30-50% of status-report cron tokens, with no architectural change.

(1) **Trim verifier diff truncation** in `verify._judge_prose_bullet` from 100KB to 30KB. The judge has Read/Glob/Grep tools (TB-136) and the prompt instructs it to verify against HEAD when the diff is ambiguous, so dropping the worst-case-defensive cap is pure upside on the average case (most diffs are 5-30KB).

(2) **Lower the prose-bullet judge's effort** from `xhigh` to `high`. The judge's job is "read a diff, optionally Grep/Read for confirmation, emit a one-line JSON verdict" — not the multi-step reasoning xhigh is sized for. `high` is plenty.

(3) **Lower the status-report cron's effort** from `xhigh` to `medium`. Pure summarization (read events tail, render markdown, post to Mattermost). `medium` is more than enough.

Explicitly out of this task: lowering MM handler effort. The handler may bear task-ideation work (operator asking for design help, scoping decisions, brainstorming) where the higher reasoning budget pays off. Stays on `xhigh`.

## Scope

Files to touch:

- `ap2/verify.py` — `_judge_prose_bullet`: change `diff_text[:100_000]` → `diff_text[:30_000]`. Add a new env-overridable knob `AP2_VERIFY_JUDGE_EFFORT` (default `"high"`) that takes precedence over `AP2_AGENT_EFFORT` for this specific call site.
- `ap2/status_report.py` (or wherever TB-144 hoisted the shared status-report routine; if it lives in `daemon.py`, the call site is there) — wire a new `AP2_STATUS_REPORT_EFFORT` env (default `"medium"`) into the `extra_args` for the status-report SDK call.
- Tests in `ap2/tests/test_verify_retry_diff.py` (or wherever the judge options are pinned) and `ap2/tests/test_status_report_skip.py` (or wherever the status-report SDK options are pinned).

## Design

### Per-call-site effort override pattern

The current shape — `extra_args={"effort": os.environ.get("AP2_AGENT_EFFORT", "xhigh")}` — bakes one value across all SDK call sites. To let specific call sites lower the budget without affecting task agents, introduce per-site env vars that fall back to the global default:

```python
# verify.py
effort = os.environ.get(
    "AP2_VERIFY_JUDGE_EFFORT",
    os.environ.get("AP2_AGENT_EFFORT", "high"),  # judge default = high, not xhigh
)
extra_args = {"effort": effort}
```

```python
# status_report.py (or daemon.py status-report branch)
effort = os.environ.get(
    "AP2_STATUS_REPORT_EFFORT",
    os.environ.get("AP2_AGENT_EFFORT", "medium"),  # status default = medium
)
extra_args = {"effort": effort}
```

Note the per-site default ≠ the global default: even when neither env var is set, judge gets `high` and status-report gets `medium` (rather than the global `xhigh`). Operators can still override globally or per-site via env if they want.

Task agents and MM handler stay on the global `AP2_AGENT_EFFORT` (currently `xhigh` per `.cc-autopilot/env`) — no change.

### Why a separate env var per site rather than a single override map

Each site already has its own SDK call construction. Per-site env vars compose with the existing `AP2_AGENT_MODEL` / `AP2_AGENT_EFFORT` pattern operators already know. A map (e.g. `AP2_AGENT_EFFORT_BY_KIND="judge:high,status-report:medium"`) would be more flexible but is harder to discover and harder to validate. Two env vars at known paths beat one map operators have to memorize.

### Diff truncation cap

Drop from 100,000 to 30,000 chars in `verify._judge_prose_bullet`. The constant doesn't need to be env-overridable — TB-136's working-tree-as-authoritative instruction means the cap is a soft limit (judge can Grep what it needs); operators wanting a different cap can edit the source.

## Verification

- `uv run pytest -q ap2/tests/` — full regression gate passes (gating)
- `grep -qE "diff_text\[:30_000\]" ap2/verify.py` — diff truncation lowered to 30KB.
- `grep -qE "AP2_VERIFY_JUDGE_EFFORT" ap2/verify.py` — per-judge effort env knob present.
- `grep -qE "AP2_STATUS_REPORT_EFFORT" ap2/` — per-status-report effort env knob present (in status_report.py or daemon.py status-report branch, depending on where TB-144 placed the shared routine).
- New unit test pinning the judge's default effort: with neither `AP2_VERIFY_JUDGE_EFFORT` nor `AP2_AGENT_EFFORT` set, the SDK options handed to `_judge_prose_bullet` carry `extra_args["effort"] == "high"`. With `AP2_VERIFY_JUDGE_EFFORT="medium"` set, they carry `"medium"`. With only `AP2_AGENT_EFFORT="xhigh"` set, they fall back to `"xhigh"` (per-site var takes precedence; absence falls through to the global, then to the per-site default).
- New unit test pinning the status-report's default effort: same shape — neither env set → `"medium"`; `AP2_STATUS_REPORT_EFFORT="high"` → `"high"`; `AP2_AGENT_EFFORT="xhigh"` only → `"xhigh"`.
- New unit test for diff truncation: a synthetic 50KB diff handed to `_judge_prose_bullet` is truncated to 30KB in the prompt sent to the SDK.
- The MM handler's effort path is untouched: existing tests that pin its `extra_args["effort"]` (if any) continue to read from `AP2_AGENT_EFFORT` and pass.

## Out of scope

- Lowering the MM handler's effort. It may bear task-ideation / scoping / brainstorming workloads where the higher reasoning budget pays off; stays on `xhigh`.
- Lowering task agents' effort. They do the actual implementation work — `xhigh` is intentional and not a target for this task.
- Adding a generic per-kind effort map (`AP2_AGENT_EFFORT_BY_KIND=...`). Two named env vars cover today's needs; broader knob is unnecessary until a third call-site wants different effort.
- Prompt caching / cache_control breakpoints (Tier 2 work — bigger refactor, file separately if Tier 1 isn't enough).
- Batching the per-prose-bullet judge into one SDK call (Tier 2 — bigger design, file separately).
- Trimming the `## Current state` block's git-log size or making `_STATUS_REPORT_CONTRACT` injection conditional (Tier 2 — file separately).
## Attempts

### 2026-05-04 — verification_failed
(no summary)
- **kind:** per_task
- **failed_criteria:** [fail] `grep -qE "AP2_STATUS_REPORT_EFFORT" ap2/` — per-status-report effort env knob present (in status_report.py or daemon.py
- **Debug dumps:** `prompt: .cc-autopilot/debug/20260504T071522Z-TB-156.prompt.md`, `stream: .cc-autopilot/debug/20260504T071522Z-TB-156.stream.jsonl`, `messages: .cc-autopilot/debug/20260504T071522Z-TB-156.messages.jsonl`

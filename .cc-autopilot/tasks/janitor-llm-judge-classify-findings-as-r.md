# Janitor LLM judge — classify findings as real-strand vs. operator-draft

## Goal

This task is anchored in goal.md's `## Done when` criterion: "An operator can point ap2 at a fresh project, paste a goal.md (with Mission + `## Done when`), and walk away for a week without intervention." The walk-away promise depends on signal, not just surfacing — a janitor that flags every operator-WIP file as "stranded" generates noise that operators learn to ignore, breaking the promise just as badly as silence did. Detection and classification need to ship together for the operator-returns-after-a-week experience to actually save them attention.

TB-177 (blocking) ships a deterministic Python detector for stranded git state — staged-uncommitted, working-tree-modified, untracked-non-gitignored files. It's a candidate generator. But "stranded" vs. "operator's draft" can't be distinguished by file-system inspection alone — both look identical to `git status`. A staged file could be the post-TB-22 detritus (real strand) OR the operator running `git add` to review staging before committing (deliberate WIP).

This follow-up adds an LLM judge step on top of TB-177's deterministic detection. The judge reads task history (recent `task_complete` / `task_pipeline_pending` / `verification_failed` events), file-path-to-TB-N correlation, and finding age, then classifies each finding as `real_strand`, `operator_draft`, or `ambiguous`. Per the operator's directive, **classified findings emit only to events.jsonl** — no operator_log.md write — so the operator-decision-log surface stays curated for genuine operator actions, not auto-emitted janitor noise.

Why now: filing concurrent with TB-177 (and blocked on it) means the cadence operators experience is "janitor surfaces classified, useful findings" from the moment janitor first runs — not "first generation of janitor floods operator_log with raw noise, then gets fixed in v2." The post-train TB-22 case (May 5, 2026) is a fresh, concrete real-strand fixture to validate the classifier against; the autopilot2 repo's own goal-draft.md (untracked, deliberately operator-edited working notebook) is a fresh operator-draft fixture for the contrasting case.

## Scope

- `ap2/janitor.py::run_janitor` (from TB-177) — extend the detection pass with a per-finding judge step. After collecting candidate findings, invoke a new internal `_judge_finding(cfg, sdk, finding) -> JudgedFinding` for each. The judge wraps an SDK call, returns `verdict ∈ {"real_strand", "operator_draft", "ambiguous"}` plus a one-sentence `reasoning` field.
- `ap2/janitor.py` — `janitor_finding` event payload gains `verdict` (string) and `reasoning` (string, ≤200 chars) fields. The original `paths`, `subkind`, `age_s`, `hint` fields stay; `verdict` is additive.
- `ap2/janitor.py` — REMOVE the `operator_log.md` summary-line write from TB-177's design. The user explicitly directed events-only emission; operator_log.md is reserved for genuine operator actions (acks, queue ops, rejections), not auto-emitted janitor noise.
- `ap2/cli.py::cmd_status` — adjust the surfacing logic to count only `verdict=real_strand` findings as urgent in the `janitor:` line; `operator_draft` findings are summarized (e.g. `"janitor: 1 strand, 2 drafts"`) but don't drive the urgency tone.
- `ap2/status_report.py` — same surfacing adjustment in the periodic cron-driven status post.
- `ap2/tests/test_janitor.py` (extend from TB-177's tests) — synthesize fixtures for each verdict class with a stubbed SDK, assert classification routing.

## Design

### Why LLM, not deterministic heuristics

The signal a human uses to distinguish strand from draft is contextual: "Is this file path mentioned in a recent TB-N's briefing or commit? Was a pipeline subprocess running recently that would have produced it? Is the file age consistent with operator-active editing or with pipeline-completion timing? Is the operator on Mattermost actively discussing this work?" These are heuristics that compose poorly in deterministic code — every project's signal mix is slightly different — but compose naturally for an LLM reading the events tail + briefing + task lifecycle.

The judge's input is small and bounded:
- The candidate finding (paths + subkind + age)
- Last 50 events from events.jsonl (filtered to lifecycle types — task_complete, task_pipeline_pending, verification_failed, ideation_approved)
- The most recent operator_log.md tail (last 20 lines, READ-only — confirms the user's events-only emission rule)
- Recent commits (last 10) for file-path correlation
- A list of TB-N's currently in Active / Backlog / Pipeline-Pending with their briefing paths, so the judge can `Read` a briefing if a finding's file paths appear scope-relevant

The judge has Read / Glob / Grep tools (same toolset as the verify-prose-bullet judge per TB-136), no Bash, no MCP-write tools. Read-only by construction.

### Verdict semantics

- **`real_strand`** — high confidence the file is unintended detritus. Examples: staged file matches a recently-completed pipeline-task's expected output paths AND the pipeline log shows a `_git_commit` failure; OR untracked file in a directory whose siblings are gitignored and no recent operator activity touches the path.
- **`operator_draft`** — high confidence the file is deliberate operator work. Examples: untracked file in repo root with operator-style filename pattern (`draft_*.md`, `notes-*.md`, `scratch.*`) AND no TB-N references it; OR working-tree-modified file the operator has been actively touching (file mtime within last hour).
- **`ambiguous`** — judge couldn't make a confident call. Falls back to surfacing as a low-urgency finding — operator decides.

The judge's prompt anchors verdict choice with examples drawn from the post-train TB-22 case (real_strand fixture) and autopilot2's own goal-draft.md untracked file (operator_draft fixture).

### Cost shape

One janitor cron run with N findings runs N+1 SDK calls (one detection-summary call to set the context + one per finding for classification). At default cadence (every 6h) and a healthy project (typically 0-2 findings per scan), expected cost is ~$0.05-0.20 per scan. The `control_run_usage` event from TB-166 captures this; operator can monitor via the existing token-tracking surface and tune cadence via `ap2 cron edit` if cost grows.

For projects with chronically-many findings (>5 per scan), the cost grows linearly. A simple cap: skip the LLM judge entirely if findings count > `AP2_JANITOR_MAX_FINDINGS_LLM` (default 10), emit findings with `verdict="ambiguous"` for the overflow. Same threshold env knob lets operators disable the judge entirely (set to 0) and fall back to TB-177's deterministic-only behavior.

### NO operator_log.md write

Per the operator's explicit directive: this task REMOVES TB-177's operator_log.md summary line. operator_log.md stays curated for genuine operator decisions (`ap2 ack`, `applied operator-queued <op>`, `rejected ideation proposal`); janitor findings — even classified — do not write there. They live in events.jsonl, surfaced via `ap2 status` and the status-report cron post.

This is a deliberate design choice. operator_log.md is read by ideation Step 0 as authoritative ground truth on operator decisions; flooding it with auto-emitted janitor noise would dilute the signal ideation calibrates against (TB-152, TB-163). Janitor findings are observability, not operator decisions.

## Verification

- `uv run pytest -q ap2/tests/` — full regression gate passes.
- `grep -nE "_judge_finding|verdict.*real_strand|verdict.*operator_draft" ap2/janitor.py` — judge function + verdict vocabulary present.
- `grep -nE "operator_log_append|operator_log.md" ap2/janitor.py` — should return ZERO matches (regression check: TB-177's operator_log.md write is gone).
- prose: a test in `test_janitor.py` synthesizes a fixture project with a staged-uncommitted file whose path matches a recently-completed `task_pipeline_pending` event in events.jsonl. Stubs the SDK to return a `verdict=real_strand` reasoning. Calls `run_janitor`. Asserts (a) one `janitor_finding` event lands with `verdict=real_strand` and a non-empty `reasoning` field, (b) NO line is appended to operator_log.md.
- prose: a test pins the operator-draft path — synthesize a fixture with an untracked `draft_tasks.md` in repo root + no TB-N reference. Stubs SDK to return `verdict=operator_draft`. Asserts the finding is emitted but with the lower-urgency verdict; `ap2 status` rendering counts it under "drafts" not "strands".
- prose: a test pins the cost-cap fallback — synthesize a fixture with 12 candidate findings (above the default `AP2_JANITOR_MAX_FINDINGS_LLM=10`). Asserts that the SDK is called at most 10 times AND the overflow findings emit with `verdict=ambiguous`.
- prose: a test pins the disabled-judge case — `AP2_JANITOR_MAX_FINDINGS_LLM=0` falls back to TB-177's deterministic behavior. NO SDK calls, all findings emit with `verdict=ambiguous` (or no verdict field), and the existing test fixtures from TB-177 pass unchanged.
- prose: a test pins the events-only emission rule — running janitor against a fixture with one real_strand finding produces exactly one `janitor_finding` event in `events.jsonl` AND zero new lines in `operator_log.md`. Pin BOTH file states.

## Out of scope

- Auto-remediation. Still deferred to a future TB; this task only adds classification, not action. The verdict labels make a future remediation TB easier (it can opt-in only on `real_strand` findings).
- Other detection kinds beyond TB-177's `git_stranded_state`. The judge framework is extensible to other kinds (dead-blocker, stale-debug-dumps), but each is a separate TB.
- Multi-finding LLM aggregation ("here are 5 findings, classify them as a batch"). v1 judges per-finding; aggregation is a future optimization.
- Confidence-score field on the verdict. v1 has three discrete labels; a `confidence: 0.0-1.0` could come later if needed.
- Custom verdict labels per project. The three labels are codebase-fixed.
- Re-judging a finding on subsequent scans (memoization across cron runs). Each scan judges fresh; if the same finding persists, the operator sees it persist in events.jsonl and decides.
- Web UI surface for findings. CLI + status-report cron + events.jsonl is enough for v1.
## Attempts

### 2026-05-06 — verification_failed
(no summary)
- **kind:** per_task
- **failed_criteria:** [fail] `grep -nE "operator_log_append|operator_log.md" ap2/janitor.py` — should return ZERO matches (regression check: TB-177's
- **Debug dumps:** `prompt: .cc-autopilot/debug/20260506T000052Z-TB-178.prompt.md`, `stream: .cc-autopilot/debug/20260506T000052Z-TB-178.stream.jsonl`, `messages: .cc-autopilot/debug/20260506T000052Z-TB-178.messages.jsonl`

# Janitor cron job — detect stranded git state in ap2 target projects (and surface for review)

## Goal

This task is anchored in goal.md's `## Done when` criterion: "An operator can point ap2 at a fresh project, paste a goal.md (with Mission + `## Done when`), and walk away for a week without intervention." Stranded git state — staged-uncommitted files, working-tree modifications older than a tick boundary, untracked-non-gitignored files lingering across cycles — silently breaks the walk-away promise. The operator returns after a week, runs `git status`, finds detritus from a pipeline-script bug or a half-finished ack, and has to reconstruct what happened.

The TB-22 case in `~/repos/post-train` (May 5, 2026) is the canonical example: `reeval_tb22.py`'s `_git_commit` swallowed an exit-1 from `git add` (the tracked-in-gitignored-dir quirk), the staged `notes.md` sat in the index for ~32 minutes until the operator manually noticed and committed it. The script bug is real and was fixed once observed — but the daemon never surfaced the issue. The class-of-bug pattern (any pipeline script that mishandles its own commit) will repeat across projects.

This task adds a `janitor` cron job that periodically scans for stranded git state and emits findings via events.jsonl + a `janitor_finding` line in `operator_log.md`. The janitor is **report-first** in v1: detection without auto-remediation. Operators see findings on `ap2 status` and the next status-report cron post; they decide whether to commit, discard, or ignore. Auto-remediation (a future `--apply` mode, separate TB) is deliberately out of scope this round — the safe-cases-vs-risky-cases distinction needs operator-eyes-on for at least one shipping cycle before automating.

Why now: the TB-22 case demonstrates the failure mode in production; the script bug was operator-fixable but the daemon's silence is what made it invisible until manual `git status` discovery. Without a janitor, every future pipeline-script error of this shape repeats the silence-then-manual-fix cycle. The walk-away promise is the project's most load-bearing claim and "stranded files in working tree" is precisely the shape of "intervention required" that a returning operator should not have to discover by accident.

## Scope

- `ap2/janitor.py` (new module) — deterministic checks (no LLM). Public entry: `run_janitor(cfg) -> JanitorReport`. Returns a structured report listing findings; emits a `janitor_finding` event for each finding; appends a single summary line to `operator_log.md` when at least one finding fires.
- `ap2/daemon.py::run_cron` (or wherever the cron-job dispatch table lives) — register `janitor` as a cron-job kind; route to `janitor.run_janitor` instead of through the standard `_run_control_agent` path (no LLM, no SDK call — pure Python).
- `.cc-autopilot/cron.yaml` (per-project) — operators add a `janitor` job entry with their preferred cadence. Suggested default in docs: `0 */6 * * *` (every 6 hours). NOT seeded automatically — the cron schedule remains operator-edited via `ap2 cron edit`.
- `ap2/cli.py::cmd_status` — surface the janitor-findings count alongside the existing pending-review count (TB-151) and pending-queue-ops count, when the latest `janitor_finding` event is recent (within the last cron interval).
- `ap2/status_report.py` — include janitor findings in the cron-driven status report when present.
- `ap2/tests/test_janitor.py` (new) — synthesize stranded-state fixtures and assert each check fires correctly + emits the expected event.

## Design

### v1 detection scope: stranded git state only

One check kind in v1: `git_stranded_state`. Finer detection types within it:

1. **Staged-but-uncommitted** (`git diff --cached --name-only` returns paths). The TB-22 case.
2. **Working-tree-modified-not-staged** older than the tick interval × 10 (configurable; default 5 minutes ≈ 10 ticks at 30s). Excludes `.cc-autopilot/`-internal files the daemon manages.
3. **Untracked-non-gitignored** files in the working tree. Operator scratch work or pipeline detritus.

Each finding is a `{type, paths: [...], age_s, hint}` record. The hint is a one-line operator suggestion, e.g., `"git status to inspect; commit with operator's intent or git restore --staged to unstage"`.

### Out of scope for v1 — explicitly listed for clarity

The janitor framework is designed to grow more checks; v1 is intentionally narrow.

- **Dead-blocker detection** (TB-X blocked on TB-Y where TB-Y is Frozen) — separate TB.
- **Pipeline-pending with dead pid + verification not run** — separate TB; daemon recovery edge case.
- **Stale debug dumps** older than N days — separate TB; cleanup-with-retention.
- **TASKS.md / CLAUDE.md drift detection** — separate TB.

### Report-only, not auto-remediate

v1 does NOT auto-commit, auto-stash, or auto-discard. The reasons:

- Auto-committing arbitrary staged changes risks committing operator work-in-progress that was deliberate and uncommitted.
- Auto-stashing is operator-surprising and creates a parallel review surface (the stash list).
- Auto-discarding (`git checkout`) is destructive and never appropriate without explicit consent.

The right v1 behavior: surface, suggest, let the operator decide. A future `ap2 janitor apply` CLI can offer guided remediation per finding, with explicit operator confirmation per type (similar to `deploy-skills.sh`'s dry-run-default + `--apply` opt-in pattern).

### Event shape

`janitor_finding` event per detection (one event per kind+paths-set, NOT one per file — keeps the events.jsonl tail readable):

```json
{
  "ts": "<iso>",
  "type": "janitor_finding",
  "kind": "git_stranded_state",
  "subkind": "staged_uncommitted | modified_not_staged | untracked_non_ignored",
  "paths": ["..."],
  "age_s": <int>,
  "hint": "..."
}
```

Plus one summary line in `operator_log.md` per janitor run that found anything:

```
- 2026-05-05T22:00:00Z — janitor: 1 stranded-state finding (3 paths). See events.jsonl.
```

When the run finds nothing, NO operator_log.md line is written (avoid noise on healthy projects).

### Excluded paths

The janitor's working-tree-modified check skips known daemon-managed paths under `.cc-autopilot/` that legitimately churn between commits (`events.jsonl`, `cron_state.json`, `mm_state.json`, `daemon.pid`, `paused`, `auto_diagnose_state.json`, `retry_state.json`, `operator_queue.jsonl`, `operator_queue_state.json`, `pipelines/*.log`, `debug/*.{prompt.md,stream.jsonl,messages.jsonl}`). These are the daemon's working state; surfacing them as stranded would be noise.

The `.cc-autopilot/insights/_index.md` IS scanned (it's content the operator may want to commit) but its detection routes to `subkind=modified_not_staged` with a hint that ideation regenerates it lazily.

### No LLM in v1

The janitor's checks are deterministic Python (`subprocess.run(["git", "status", "--porcelain"], ...)` parsing). No LLM call, no SDK cost, no `_run_control_agent` plumbing. Cheaper to run frequently; deterministic findings; trivial to test.

A future v2 could add an LLM-driven "interpret what the operator probably meant" pass for ambiguous findings — but that's optimization, not the v1 line.

## Verification

- `uv run pytest -q ap2/tests/` — full regression gate passes.
- `grep -nE "def run_janitor|JanitorReport|janitor_finding" ap2/janitor.py` — module + entry function + event-type are wired.
- `python3 -c "from ap2.janitor import run_janitor; from inspect import signature; assert 'cfg' in signature(run_janitor).parameters"` — public entry signature stable.
- `grep -nE "janitor" ap2/daemon.py ap2/cli.py ap2/status_report.py` — janitor is wired into cron-job dispatch, status CLI, and status-report cron.
- prose: a test in `test_janitor.py` synthesizes a fixture project with (a) a staged-but-uncommitted file, (b) a tracked file modified 10 minutes ago, (c) an untracked file outside gitignore, AND (d) a `.cc-autopilot/events.jsonl` mtime within the last second. Calls `run_janitor(cfg)`. Asserts:
  - exactly three `janitor_finding` events appear in `events.jsonl` after the call (one per subkind from a-c)
  - the `events.jsonl` modification (d) does NOT trigger a finding (excluded path)
  - `operator_log.md` gains exactly one summary line containing "janitor:" + "3" or "stranded"
- prose: a test pins the no-findings case — clean working tree → `run_janitor` returns an empty report, NO `janitor_finding` events emitted, NO `operator_log.md` line appended.
- prose: a test pins the cron-job dispatch path — adding a `janitor` job to a fixture `cron.yaml` and calling the daemon's cron-tick routine routes through `janitor.run_janitor` (NOT through `_run_control_agent`), and emits a `cron_complete` event for the `janitor` job kind alongside.
- prose: `ap2 status` rendering — when at least one recent `janitor_finding` event exists in `events.jsonl`, the status output contains a `janitor:` line with the finding count and a hint to inspect via `ap2 logs`. Pin via `ap2/tests/test_cli.py` against a fixture events.jsonl.

## Out of scope

- Auto-remediation. v1 surfaces findings only; a future TB can add `ap2 janitor apply` with per-finding-type opt-in.
- Other detection kinds beyond git-stranded-state. The framework is extensible; v1 ships with one kind to keep scope tight and validate the pattern.
- LLM-driven interpretation of findings. Deterministic Python is sufficient; a v2 could add an interpretive layer if findings get noisy.
- Multi-project janitor (one daemon scanning multiple projects). Each project has its own daemon + cron; cross-project rollups are a different problem.
- A web-side `/janitor` page. CLI + status-report cron + events.jsonl is enough surface for v1; a dedicated page can come later if findings volume warrants.
- Auto-seeding the `janitor` cron entry on `ap2 init`. Operator-controlled — they add it via `ap2 cron edit` when they want it.
- Renaming or replacing existing `git status` checks elsewhere in the codebase (`ap2 doctor`, `ap2 check`). Janitor is its own concern; doctor/check stay focused on schema validation.
- Configurable per-finding age thresholds via env. Defaults are baked in for v1; if operator-tunable thresholds become a real ask, a separate TB.

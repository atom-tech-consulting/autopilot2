# TB-128 — Status-report cron emits stale text; not reflecting current state

## Goal

Status-report cron has been emitting reports with stale embedded timestamps and unchanging body text. On 2026-04-30 the 12:48Z and 14:49Z reports both carried '2026-04-30T08:56Z' in their headline despite being generated hours later, and the bullet content was nearly identical across the afternoon reports. Likely cause: the status-report agent reads from progress.md or some cached briefing rather than current daemon state + event tail. Fix: the cron prompt should mandate re-reading events.jsonl tail, current ap2 status board counts, and git log --oneline -10 from HEAD at the moment the report runs; the timestamp in the report body should come from datetime.utcnow() formatted right before posting, not from any prior context. Also worth gating: skip the post entirely if nothing has changed since last report (prevents Mattermost noise).

## Scope

- (file / module to change)

## Design

(filled in by /tb prep or by the ideation agent)

## Verification

Concrete acceptance criteria the daemon's per-task verifier (TB-69)
runs after the agent's commit. Shell-command bullets (backtick-fenced
at the start of the bullet) are run automatically; prose bullets are
judged by an SDK call against the diff.

- `uv run pytest -q` — full suite passes
- (additional shell or prose bullets)

## Out of scope

- (filled in)

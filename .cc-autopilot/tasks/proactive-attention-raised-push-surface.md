## Goal

Build a proactive, distinct push surface for attention-needing
conditions so the multi-project operator the new focus targets can
triage at a glance which project needs them and why. Closes the
third failure mode goal.md names under `Current focus: operator-legible reporting and monitoring`
("Shallow monitoring: the periodic report is the only push surface;
attention-needing conditions — a task stuck / looping, repeated
verification failures, validator-judge noise, cost-cap approach, a
pending decision — are buried in it or only visible by pulling
`ap2 status`") and directly satisfies the Done-when bullet
"Attention-needing conditions (stuck / failed / frozen tasks,
decisions-needed, cost or validator-judge anomalies) are surfaced
proactively in operator-legible terms, distinct from routine
progress updates."

Why now: today the periodic 2h status-report post is the ONLY push
surface; conditions that warrant immediate operator attention land
buried inside it (no `ap2/attention.py` module exists; no
`attention_raised` event type registered in `ap2/events.py`; no
distinct "Attention needed" section in `STATUS_REPORT_PROMPT`). A
stuck Active task at minute 5 of a 2h window waits up to 2h to
surface, and routine progress bullets visually outweigh the
embedded attention signal when it does. The shipped surfaces
(`ap2 status`'s `[noisy]` suffix, web `/automation` card) are
pull-only — they require the operator to remember to look, which
contradicts a walk-away monitoring promise.

## Scope

(1) New module `ap2/attention.py` exposing
`detect_attention_conditions(cfg) -> list[AttentionCondition]`
where `AttentionCondition` is a small dataclass with fields
`type: str`, `key: str`, `summary: str`, `ts: str`,
`extras: dict`. Seeds with ONE detector: `task_stuck` — flags any
task in the `Active` board section whose most recent `task_start`
event in `events.jsonl` is older than `AP2_TASK_STUCK_THRESHOLD_S`
(default 14400 / 4h) AND has no intervening terminal event
(`task_complete`, `task_failed`, `verification_failed`,
`retry_exhausted`). `key` is `f"task_stuck:{task_id}"` so debounce
is per-task, not per-condition-type.

(2) Per-tick wire-up in `daemon._tick`: call
`detect_attention_conditions`, debounce against the most recent
`attention_raised type=<x> key=<y>` events in the tail
(suppress when last fire was within `AP2_ATTENTION_DEBOUNCE_S`,
default 21600 / 6h), and for each fresh condition emit
`attention_raised type=<...> key=<...> summary=<...>` with the
extras blob inlined under the standard event-payload contract.

(3) Register `attention_raised` in `ap2/events.py`; add it to
`IDEATION_RELEVANT_EVENT_TYPES` (TB-169 allowlist — so ideation
sees fresh attention events in the prompt tail and can reason
against them next cycle) AND to
`_STATUS_REPORT_AUTOMATION_INTERESTING_TYPES`
(`ap2/status_report.py` L912) so a fresh attention fire un-skips
the dedup/idle gate, parallel to the TB-244 / TB-245 pattern of
extending the interesting-types set as new push-surface event
classes ship.

(4) New helper `render_attention_section(cfg, *, since_event_idx, tail)`
in `ap2/status_report.py` — returns a Markdown section headed
`## Attention needed` with one bullet per still-active condition
(omit-on-empty parity with the other digest helpers). Each
bullet uses operator-legible phrasing, e.g.
`- ⚠ **TB-N** — <title> Active for <h>h since <ts>` (title looked
up via `Board.find`). Wire into the snapshot block; extend
`STATUS_REPORT_PROMPT` to forward the section VERBATIM (same
contract as TB-228 / TB-244 sub-blocks) BEFORE the routine
progress bullets — visually distinct + positionally prominent
for the walk-away operator's first glance.

(5) Env knobs `AP2_TASK_STUCK_THRESHOLD_S` (default 14400) and
`AP2_ATTENTION_DEBOUNCE_S` (default 21600) wired through
`ap2/config.py` + `ap2/env_reload.py` (hot-reload eligible —
they tune detection, not lifecycle).

(6) Regression-pin module `ap2/tests/test_tb282_attention_stuck_task.py`
covers: detector finds a stuck task at threshold+1s; detector
misses a fresh task at threshold-1s; debounce suppresses re-fire
within the window; `attention_raised` event emitted with the
documented payload shape; renderer produces the documented
bullet shape on synthetic input; omit-on-empty when no
conditions are active; skip-gate treats `attention_raised` as
interesting (parallel to the TB-244 / TB-245 interesting-types
tests); env-knob default + override + invalid-value fallback.

## Design

Module layout follows the existing axis-by-axis split that TB-263
established (one cohesive responsibility per sibling file); the
detector signature returns structured records rather than emitting
events itself so the daemon wire-up owns event emission +
debounce — keeps `detect_attention_conditions` pure for testing.
Debounce by `(type, key)` rather than per-type ensures a second
stuck task doesn't get suppressed because a first one fired
recently. The renderer pre-renders the bullet so the agent can
forward verbatim under the existing daemon-renders /
agent-forwards contract; the agent never has to look up titles or
compute durations.

## Verification

- `test -f ap2/attention.py` — new module exists.
- `grep -Eq "detect_attention_conditions|attention_raised" ap2/attention.py` — detector + event symbol present.
- `grep -q "attention_raised" ap2/events.py` — event type registered.
- `grep -Eq "AP2_TASK_STUCK_THRESHOLD_S|AP2_ATTENTION_DEBOUNCE_S" ap2/config.py` — env knobs wired.
- `grep -Eq "render_attention_section|Attention needed" ap2/status_report.py` — section renderer + heading wired.
- `test -f ap2/tests/test_tb282_attention_stuck_task.py` — regression-pin module exists.
- `uv run pytest -q ap2/tests/test_tb282_attention_stuck_task.py` — module passes.
- `uv run pytest -q` — full suite passes.

## Out of scope

- Additional detector kinds beyond `task_stuck` (validator-judge
  noisy, cost-cap approach, decisions-needed-new, frozen-task
  recency are valuable follow-ups — each is its own scoped task
  to keep this one focused).
- Posting to Mattermost immediately on detector fire (this task
  surfaces via the existing status-report cron + the event log;
  an out-of-band immediate push has its own rate-limit + dedup
  concerns and belongs in a follow-up).
- Web `/attention` page (pull-surface evolution belongs in a
  separate task once the event vocabulary lands and accrues
  data).
## Attempts

### 2026-05-23 — verification_failed
(no summary)
- **kind:** project_wide
- **verify_command:** uv run pytest -q ap2/tests/
- **exit_code:** 1
- **stderr_tail:** 
- **Debug dumps:** `prompt: .cc-autopilot/debug/20260523T022116Z-TB-282.prompt.md`, `stream: .cc-autopilot/debug/20260523T022116Z-TB-282.stream.jsonl`, `messages: .cc-autopilot/debug/20260523T022116Z-TB-282.messages.jsonl`

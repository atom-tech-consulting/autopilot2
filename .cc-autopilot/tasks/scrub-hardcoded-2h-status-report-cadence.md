# Scrub hardcoded `2h` status-report cadence references (TB-306 follow-up)

Tags: #autopilot #docs #cleanup #status-report #regression-pin

## Goal

TB-306 bumped the default `ap2/cron.default.yaml` status-report
interval from 2h → 8h but explicitly deferred the prose scrub as
out-of-scope. ~28 hardcoded `2h` references remain across user docs,
source docstrings, and test docstrings — all describe the
status-report digest as a "2h cron post" / "every 2h" / "next 2h"
push surface that arrives every two hours. These are runtime-inert
(the actual cadence is whatever `.cc-autopilot/cron.yaml` says, now
8h) but they materially mislead operators reading goal.md's "Current
focus: operator-legible reporting and monitoring" walk-away
contract: someone scanning `ap2/howto.md ## Configuration knobs` or
the inline docstrings to learn the digest cadence sees "2h" and
forms a wrong mental model.

For each occurrence, pick whichever option ages best:
- **Reference the source-of-truth.** Phrase the prose around "the
  status-report cron interval" (with `.cc-autopilot/cron.yaml` as
  the operator-tunable anchor) rather than naming a specific
  duration. Best for docstrings and comments where the cadence is
  incidental to the explanation.
- **Update to `8h`** if a concrete cadence aids the reader (e.g.
  "lands in the next 8h cron post" still parses if the operator
  later changes their cron.yaml — they understand the wall-clock
  intent).
- **Delete** the cadence reference entirely if it doesn't carry the
  paragraph's meaning. Historical-context lines ("pre-TB-X the 2h
  cron post was the ONLY push surface") often read fine as "pre-TB-X
  the status-report cron was the ONLY push surface."

The bias should lean toward source-of-truth references where the
prose tolerates it — that fixes the drift class once instead of
chasing each cadence change.

Why now: TB-306's bump landed but its briefing's Out-of-scope
deferred this scrub explicitly ("a follow-up prose-sweep task can
clean them"). Doing it now keeps the doc-drift gates honest (TB-305
covers env-knob coverage but not arbitrary prose) and keeps the
operator-legible pull surfaces aligned with the actual cadence
before the gap compounds. Independently surfaced from operator
audit on 2026-05-27 immediately after TB-306 close.

## Scope

User-facing docs (8 mentions):
- `ap2/architecture.md:218` — the "2h Mattermost digest" annotation
  on the `status_report.py` line of the tree-style file map.
- `ap2/howto.md` — lines 662, 821, 1050, 1295, 1864, 1868, 2099,
  2110. Mix of prose paragraphs and one table cell.

Source docstrings + comments (16 mentions):
- `ap2/status_report.py` — lines 386, 392, 491, 499, 570, 1489,
  1645, 1678. Mostly `# TB-XXX` history blocks above sub-section
  renderers + the dedup-gate's "interval (~2h) of staleness can
  bleed through" docstring.
- `ap2/config.py` — lines 61, 105, 112. The `AP2_ATTENTION_DEBOUNCE_S`
  and `AP2_ATTENTION_IMMEDIATE_PUSH` defaults' rationale prose.
- `ap2/attention.py:7` — module-level docstring "Pre-TB-282 the
  periodic 2h status-report cron post was the ONLY push…"
- `ap2/automation_status.py:883` — comment above the digest
  sub-section.
- `ap2/cli_daemon.py:301` — comment in the diagnose surface.
- `ap2/events.py:283` — comment in the attention event docstring.
- `ap2/web_attention.py:9` — module docstring.

Test docstrings (8 mentions):
- `ap2/tests/test_tb241_status_dry_run_surface.py:14`
- `ap2/tests/test_tb243_validator_judge_surface.py:15`
- `ap2/tests/test_tb244_status_report_focus_rotation_digest.py:8,13`
- `ap2/tests/test_tb245_status_report_validator_judge_digest.py:8,16`
- `ap2/tests/test_tb258_audit_count_surface.py:8`
- `ap2/tests/test_tb282_attention_stuck_task.py:11`
- `ap2/tests/test_tb297_attention_immediate_push.py:7`
- `ap2/tests/test_status_report_skip.py:866`

## Design

- **Per-occurrence judgment.** Don't sed-replace blindly. Read each
  paragraph and choose source-of-truth / update / delete based on
  what the surrounding prose is trying to communicate.

- **Keep historical references intact when the year/TB is part of
  the framing.** A test docstring saying "the 2h status-report
  Mattermost post was the ONLY push surface" is describing the
  pre-TB-282 *state of the world* — that's history. Re-cast as
  "the status-report Mattermost cron post" (drop the cadence
  qualifier), preserving the pre-vs-post-TB-282 distinction.

- **Howto.md table cell (line 662).** The `ap2 audit` row mentions
  "the 2h cron status-report Mattermost post carries…" inside a
  multi-sentence cell. Update to "the status-report Mattermost
  cron post" — the cadence wasn't load-bearing for the audit
  surface description.

- **Source comments referencing the digest as a "push surface" or
  "operator's primary walk-away channel"** — drop the cadence
  qualifier; what matters is the channel identity, not its tick
  rate.

- **One exception worth keeping cadence-explicit:** if a comment
  says "without this, the silent-degradation hazard had no
  push-channel observability — waiting 2h for surfacing" the "2h"
  IS the operator-impact claim. Replace with "waiting up to the
  next status-report cron tick" — same impact framing, no stale
  number.

## Verification

- `uv run pytest -q ap2/tests/` — full suite passes after the
  scrub (regression pin — many of the touched docstrings are above
  tests whose behavior shouldn't change, but the file must still
  parse).

- `! grep -rqE "\\b2h status[ -]report\\b" ap2/` — phrase "2h
  status-report" (with space or hyphen variant) no longer appears
  anywhere in `ap2/`. Anchors the most common stale shape.

- `! grep -rqE "\\b2h cron post\\b" ap2/` — phrase "2h cron post"
  no longer appears.

- `! grep -rqE "\\b(per|next|every)[- ]2h\\b" ap2/` — phrase
  variants "per-2h", "next 2h", "every 2h", "every-2h" no longer
  appear in the cadence-claim sense.

- `grep -c "2h" ap2/cron.py` returns at least 1 — generic
  `parse_interval("2h")` docstring example survives (whitelisted as
  out-of-scope).

- `grep -c "2h" ap2/tests/test_cron.py` returns at least 1 —
  `assert cron.parse_interval("2h") == 7200` parser test survives.

- `grep -c "2h" ap2/tests/test_web_home.py` returns at least 1 —
  ideation-cooldown test uses `AP2_IDEATION_COOLDOWN_S=7200` (=2h),
  a different knob, survives.

- `ap2/howto.md` Prose: no paragraph in `## Configuration knobs`
  or the status-report section describes the digest cadence as
  "every 2h" or "the 2h cron post". Either drops the cadence
  qualifier or references "the status-report cron interval"
  (operator-tunable in `.cc-autopilot/cron.yaml`). Judge confirms
  via Read.

- `ap2/architecture.md` Prose: the file-map annotation for
  `status_report.py` no longer says "the 2h Mattermost digest" —
  drops the cadence number or refers to "the status-report
  Mattermost digest". Judge confirms via Read.

- `ap2/status_report.py` Prose: the eight TB-XXX history blocks
  + the dedup-gate docstring use cadence-agnostic phrasing (no
  literal "2h" in any updated comment). Judge confirms via Read.

## Out of scope

- `ap2/cron.py:26` — the `parse_interval('30m', '2h', '45s', '1d')`
  docstring example. "2h" is a generic illustrative input string,
  not a cadence claim.

- `ap2/tests/test_cron.py:13` — `cron.parse_interval("2h") == 7200`
  is a parser unit test, not a status-report claim.

- `ap2/tests/test_web_home.py:492` — "cooldown=2h" refers to
  `AP2_IDEATION_COOLDOWN_S=7200` (ideation cooldown), a different
  knob entirely. Not affected by the status-report bump.

- The 4-space-vs-2-space `cron.yaml` indent footgun from TB-306's
  4th-attempt notes (PyYAML safe_dump emits 2-space; cron.default.yaml
  ships 4-space). Separate fix — either pin `indent=4` in
  `ap2/cron.py:save_jobs` or document the asymmetry. File a
  distinct task.

- Adding a docs-drift gate for "no hardcoded cadence numbers in
  cadence-descriptive prose". That would require a per-token
  whitelist with carve-outs for the legitimate "2h" survivors
  above; over-engineered for a class-of-drift that appears once
  per cadence bump. The TB-305 docs-drift gate pattern covers env
  knobs, MCP tools, event types, CLI verbs — all enumerable; arbitrary
  cadence prose isn't.

- Bumping the cadence further (e.g. 8h → 12h) is a separate operator
  decision; this task only closes the existing 2h→8h gap.
## Attempts

### 2026-05-27 — verification_failed
(no summary)
- **kind:** per_task
- **failed_criteria:** [fail] `! grep -rqE "\\b2h status[ -]report\\b" ap2/` — phrase "2hstatus-report" (with space or hyphen variant) no longer appea; [fail] `! grep -rqE "\\b(per|next|every)[- ]2h\\b" ap2/` — phrasevariants "per-2h", "next 2h", "every 2h", "every-2h" no longer
- **Debug dumps:** `prompt: .cc-autopilot/debug/20260527T210715Z-TB-307.prompt.md`, `stream: .cc-autopilot/debug/20260527T210715Z-TB-307.stream.jsonl`, `messages: .cc-autopilot/debug/20260527T210715Z-TB-307.messages.jsonl`
### 2026-05-27 — state_violation
(no summary)
- **fenced_files:** TASKS.md
- **pre_run_head:** 72476d84
- **Debug dumps:** `prompt: .cc-autopilot/debug/20260527T212257Z-TB-307.prompt.md`, `stream: .cc-autopilot/debug/20260527T212257Z-TB-307.stream.jsonl`, `messages: .cc-autopilot/debug/20260527T212257Z-TB-307.messages.jsonl`

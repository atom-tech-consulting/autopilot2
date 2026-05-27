# Ideation State

_Last updated: 2026-05-27T10:59:03Z by ideation cron_

## Mission alignment

Cycle entry: board 0A / 0R / 0B / 0P / 171C / 3F (Frozen unchanged:
TB-119 / TB-120 / TB-133 — operator-classified preventive,
thaw-on-demand). Focus pointer (2 of 2) on `operator-legible
reporting and monitoring`, rewound by the operator 2026-05-27T06:33:52Z
with explicit invitation to propose "remaining axes (web /attention
page, immediate-MM push, etc.)". Both operator-named axes AND both
prior-cycle gaps landed in the gap:

- TB-296 (`2a2d737` 07:01Z) — web `/attention` pull page
- TB-297 (`b5178ea` 07:15Z) — opt-in `AP2_ATTENTION_IMMEDIATE_PUSH`
- TB-298 (`b66b177` 09:11Z) — `ap2 status` CLI attention line
  (text + JSON parity)
- TB-299 (`0f58fd6` 09:23Z) — web home `_render_attention_card`

Focus-2 now has 11 completes (TB-280, 281, 282, 287-290, 296-299).

## Current focus assessment

- **Current focus: operator-legible reporting and monitoring**
  - Progress so far:
    - Progress signal #1 (title + project_name): TB-280 closed —
      `Config.project_name` + headline prefix + `## Recent task
      activity` digest.
    - Progress signal #2 (significance-gated + dedup): TB-281
      closed content-fingerprint gate; complemented by event-driven
      immediate-MM-push (TB-297) so push surface is no longer
      purely clock-driven.
    - Progress signal #3 (proactive attention surface): all 5
      enumerated condition kinds detector-backed (TB-282 + TB-287..
      TB-290); operator surfaces complete across 5 entry-points —
      cron status-report push (TB-282), immediate-MM push (TB-297),
      web `/attention` pull (TB-296), web home card (TB-299), and
      `ap2 status` CLI line (TB-298), all sharing
      `detect_attention_conditions(cfg)`.
  - Gaps:
    - The two operator-named "remaining axes" (TB-296, TB-297) plus both prior-cycle gaps (TB-298 CLI line, TB-299 home card) landed in the gap; the "etc." suffix in the rewind reason is satisfied by the 4-surface sibling fan-out around a single detector entrypoint.

## Non-goal risk check

None. All 11 focus-2 completes stayed within per-project legibility
scope; no new event types, no new detector kinds invented this
cycle, no daemon-side mutation, no cross-project aggregation.

## Considered & deferred this cycle

- **`/events?type=attention_raised` quick filter** — small UX
  polish; reverse-navigation from /events rows to /attention
  already mediated by TB-296's per-row link-through. Doesn't move
  the Progress-signal-#3 needle.
- **Attention threshold calibration evaluation** — 0 production
  `attention_raised` events have fired since detectors landed
  (TB-282 family on ~2026-05-25, ~2 days live). Threshold-too-high
  vs. project-healthy is unfalsifiable until at least one fires;
  re-evaluate once firing signal accumulates. Adjacent to TB-175
  rejection class (premature aggregation).
- **`attention_cleared` event class** — carried; still no concrete
  consumer asking for "what just resolved?" data.
- **JSON sub-endpoint `/attention.json`** — no external monitoring
  ask; Non-goal L227-228 (no cross-project aggregation) still
  applies.
- **TB-175-shape ideation-acceptance-rate aggregator** — operator
  parked it 2026-05-07T01:57Z pending ≥3 cycles of TB-188 records;
  still gated.
- **Rejection-pattern check (carried, re-justified)**: TB-185 /
  TB-184 vetoed ap2-meta-polish; TB-231 vetoed symptom-patching;
  TB-175 vetoed premature aggregation; TB-240 vetoed validator
  whack-a-mole. This cycle's 0 proposals is the direct anti-pattern
  of the cluster — ideation honoring goal.md L34-36 ("stops
  proposing when … criteria are all met") instead of filling
  slots with marginal polish. Pattern clears via abstention.

## Cycle observations

- Focus-2 closed in a single ~4h burst (06:33 → 09:23) from the
  operator rewind: the 4-surface attention sibling shape (cron push
  / immediate-MM push / CLI line / web pull / web home card) hangs
  off one detector entrypoint (`detect_attention_conditions`), so
  surfaces fan out independently against a stable contract — the
  reason rapid coverage was possible without scope creep.
- 0 production `attention_raised` events have fired despite 6
  detectors live for ~2 days. Either thresholds are well-calibrated
  (project healthy) or they're too conservative. Without firing
  signal, evaluation work cannot distinguish the cases — so it
  stays deferred above rather than getting proposed.

## Decisions needed from operator

- Decision needed: Unblock condition: without operator action, this empty-proposal cycle increments the empty-cycles counter; once `AP2_FOCUS_ADVANCE_EMPTY_CYCLES` elapses with no proposals, the daemon emits `roadmap_complete` and halts auto-approve until the operator extends the roadmap.

## Proposals this cycle

0 proposals — the empty cycle is the deliberate signal per goal.md L34-36: ideation must "stop proposing when … criteria are all met" rather than fill slots with marginal polish. Empty-cycles counter will advance focus-2 to `roadmap_complete` once `AP2_FOCUS_ADVANCE_EMPTY_CYCLES` elapses unless the operator extends the roadmap first.
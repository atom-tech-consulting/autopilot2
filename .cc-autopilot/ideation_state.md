# Ideation State

_Last updated: 2026-05-27T08:50:20Z by ideation cron_

## Mission alignment

Cycle entry: board 0A / 0R / 0B / 0P / 169C / 3F (Frozen unchanged:
TB-119 / TB-120 / TB-133 — all operator-classified preventive,
thaw-on-demand). Focus pointer (2 of 2) on `operator-legible
reporting and monitoring`, just rewound by the operator at
2026-05-27T06:33:52Z. Both operator-named axes in that rewind
reason — web `/attention` pull page and immediate-Mattermost push
on attention_raised — landed in the gap:

- TB-295 (`6081f96` 06:33Z) — `ap2 rewind-focus` CLI + synthetic
  `focus_advanced trigger=operator_rewind` (used by operator
  immediately to re-engage focus-2; closed the empty-cycles
  counter-cutoff hole exposed when the prior false-advance was
  recovered).
- TB-296 (`2a2d737` 07:01Z) — `/attention` web page (sibling
  `web_attention.py`, nav-bar link, `/events` row link-through;
  shares `detect_attention_conditions(cfg)` with the push surface).
- TB-297 (`b5178ea` 07:15Z) — opt-in `AP2_ATTENTION_IMMEDIATE_PUSH`
  knob; daemon helper posts `[<project>] ⚠ <summary>` to
  `AP2_MM_CHANNELS[0]` with sticky no-destination flag, debounce
  reuses TB-282 contract, `attention_pushed` event un-skips cron
  dedup gate.

## Current focus assessment

- **Current focus: operator-legible reporting and monitoring**
  - Progress so far:
    - Progress signal #1 (title + project_name): TB-280 closed.
    - Progress signal #2 (significance-gated + dedup): TB-281
      closed.
    - Progress signal #3 (proactive attention surface): all 5
      enumerated condition kinds detector-backed (TB-282 +
      TB-287..TB-290); push surfaces are the status-report cron
      `## Attention needed` section (TB-282), the optional
      immediate Mattermost push (TB-297); pull surface is the
      `/attention` web page (TB-296).
  - Gaps:
    1. **`ap2 status` CLI does not surface active attention
       conditions.** Verified by reading
       `ap2/cli_daemon.py:cmd_status` (lines ~200-580) — it
       collects `auto_approve_state`, `audit_state`,
       `env_staleness`, `_focus_item`, `janitor_counts`, and
       `operator_decisions`, but never imports `attention` or
       calls `detect_attention_conditions`. Grep confirms: 4
       call sites for the detector (daemon, status_report,
       web_attention, web) and none in CLI. The walk-away
       operator polling via CLI sees board counts + auto-approve
       state but not what's drawing attention; only the web page
       (browser required) or the 2h cron post (in chat) surfaces
       it.
    2. **Web home page has no attention summary card.** Verified
       by `grep -i attention ap2/web_home.py` → no matches. The
       home page composes `_render_focus_card` (TB-242),
       `_render_automation_card` (TB-227), `_render_pending_queue`,
       `_render_operator_decisions`, `_render_ideation_status_block`,
       and `_render_env_stale_warning` — but the attention surface
       (a sibling axis of focus / automation / decisions) appears
       only as a nav link to `/attention`. An operator landing on
       `/` who has not yet learned the nav has no visual cue that
       attention is firing.
  - Status: `in-progress`
  - Reasoning: both remaining gaps are concrete one-file
    additions, both extend the SAME `detect_attention_conditions`
    detector entrypoint to two operator entry-points where it is
    currently absent.

## Non-goal risk check

Both proposals stay strictly within per-project legibility scope
(goal.md focus-2 L227-228 — "per-project legibility, NOT
cross-project aggregation"). Both are additive read-only consumers
of `detect_attention_conditions` (no new detector kinds, no new
event types, no daemon-side mutation). The CLI surface mirrors
TB-258's existing `audit:` / TB-260's `env stale` cluster pattern
(omit-on-empty, JSON parser-stability key always present). The
home card mirrors TB-242 / TB-227 (omit-on-empty card). Neither
modifies the detector module.

## Considered & deferred this cycle

- **`attention_cleared` event class** — still no concrete consumer
  asking for "what just resolved?" data. Carried from prior cycle;
  defer condition unchanged.
- **JSON sub-endpoint `/attention.json`** — no external monitoring
  ask; scope-guard L227-228 (no cross-project aggregation) still
  applies. Defer.
- **TB-175-shape ideation-acceptance-rate aggregator** — operator
  parked it 2026-05-07T01:57Z pending ≥3 cycles of TB-188 records;
  still gated.
- **Rejection-pattern check (carried, re-justified)**: TB-185 /
  TB-184 vetoed ap2-meta-polish; TB-231 vetoed symptom-patching;
  TB-175 vetoed premature aggregation; TB-240 vetoed validator
  whack-a-mole. This cycle's two proposals extend the SAME
  detector entrypoint (TB-282 contract) to existing
  operator-facing surfaces (CLI status, web home) — neither is
  meta-polish, neither invents new detector logic, neither
  aggregates across projects, neither whack-a-moles a validator
  gate. Pattern clears.

## Cycle observations

- 0 production `attention_raised` events have fired despite 6
  detectors live; both proposals must render an empty-state
  cleanly so the quiet-project default is "no attention line"
  rather than a noisy zero-state.

## Decisions needed from operator

(none this cycle — both proposals are concrete one-file
additions covering operator-poll surfaces the rewind reason's
"etc." suffix implicitly invites.)

## Proposals this cycle

Two proposals queued behind `@blocked:review`:

- TB-298 — `ap2 status`: surface active attention conditions
  in the CLI text + JSON output (per-condition bullet line in
  text, parser-stable `attention` key in JSON; omit-on-empty
  in text only).
- TB-299 — Web home page: `_render_attention_card` sibling
  (operator-legible per-condition bullets with link-through to
  `/attention`; omit-on-empty card).
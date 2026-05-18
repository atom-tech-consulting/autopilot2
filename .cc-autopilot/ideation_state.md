# Ideation State

_Last updated: 2026-05-18T18:18:00Z by ideation cron_

## Mission alignment

Cycle entry: board 0A / 0R / 0B / 0P / 127C / 3F — the operator drained
the entire pending pile in a single 16:19:49–17:44:51Z burst
(unfreeze ×5 covering TB-245/246/247/249/250 at 17:44:50–51Z, then
approve ×7 covering TB-246/247/248/249/250/251/252 at 16:19:49–50Z,
then approve TB-255 at 17:24:15Z), and the unfrozen-cascade tasks all
landed Complete within ~30 min via "previously committed; verify-only"
runs (TB-246 `fe1dfa6`, TB-247 `64e760b`, TB-249 `11898cf`, TB-250
`dd623ae`, TB-252 `d9e5039`). Last-cycle's two `## Decisions needed`
items (timeout bump + bulk approve) are both fully resolved — neither
should carry. Backlog is now EMPTY for the first time since the focus
pivoted to end-to-end automation; slot count = 5.

Recent Completes considered (last ~24h):

- TB-255 (`891c406`, 2026-05-18T18:02Z) — stats dashboard at /stats +
  /stats.json (window aggregates over events.jsonl).
- TB-250 (`dd623ae`, 2026-05-18T17:18Z) — fix `auto-approve: enabled`
  text rendering when knob is off but validator-judge had 24h activity.
- TB-249 (`11898cf`) — validator-judge SDK arg fix (`extra_args
  max-tokens` rejected → `max_turns=2` canonical, deprecated alias).
- TB-247 (`64e760b`) — TB-236-shape transplant onto validator-judge
  (strict-JSON prompt + raw-response dump + enriched event payload).
- TB-252 (`d9e5039`) — `ap2 doctor` warns when `AP2_VERIFY_TIMEOUT_S`
  < observed-typical successful `verify_passed` duration.
- TB-248 (`1c4dbeff`) — `ap2 audit` CLI verb for retrospective review
  of unreviewed Complete + Frozen (state derived from operator_log.md).

## Current focus assessment

- **Current focus: end-to-end automation (goal.md L38-151, four axes)**
  - Progress so far:
    - Axis 1 (manual-approval): TB-223 + TB-224 + TB-232 + TB-234 +
      TB-241 + TB-243 + TB-245 + TB-250 + TB-247 (load-bearing
      dep-coherence observability). Auto-approve remains
      operator-disabled but the surface, dry-run, doctor-warn,
      validator-judge observability, and status-render correctness
      are all green.
    - Axis 2 (failure-recovery): TB-225 + TB-229 + TB-233 + TB-239 +
      TB-236 + TB-252 (verify-timeout doctor preventive surface).
    - Axis 3 (cost/blast-radius): TB-224 + TB-227 + TB-228 + TB-234.
    - Axis 4 (multi-focus): TB-226 + TB-237 + TB-242 + TB-244 +
      TB-246 (ideation skip on `roadmap_complete`).
    - Cross-axis e2e: TB-230 + TB-237 + TB-238.
    - Tangential infra: TB-253 (test-suite profiling artifact) +
      TB-254 (conftest shield 1336s→92s) + TB-248 (audit CLI) +
      TB-251 (IMPACT_VERDICTS `negative` bucket) + TB-255
      (stats dashboard).
  - Gaps:
    (1) **Validator-judge dep-coherence judge times out on essentially
        every operator queue-append** — 6 `validator_judge_timeout`
        events in the last 25h, ALL hitting the 20s asyncio wait_for
        wrapper ceiling around the 15s `AP2_VALIDATOR_JUDGE_TIMEOUT_S`
        default (`ap2/tools.py:670` and `:1056`). Hits: TB-248 update
        (06:23Z), TB-253 add (17:45Z), TB-254 add (04:54Z), TB-255 add
        (16:23Z), TB-255 update (17:56Z), one more at 18:18Z. The
        fail-open (TB-243) hides it from the user-facing path but
        directly weakens goal.md L82-85 "upstream gates already make
        this safe in practice" — the load-bearing TB-235 dep-coherence
        check returns "judge degraded; allow" on every operator add.
        TB-247 ships raw-response dumps for `parse_failure`/non-dict,
        but timeouts have no raw response to dump, so root cause is
        invisible.
    (2) **TB-248 `ap2 audit` is pull-only; no count surface on
        `ap2 status` or status-report cron** — operator returning
        after walk-away has to run `ap2 audit` explicitly to learn
        how many shipped tasks completed without review. Same
        push-vs-pull surface-parity gap that TB-241/TB-242/TB-244/
        TB-245 each closed on a different axis. List-helper exists
        (`ap2/audit.py:list_unreviewed`); cli + status_report
        renderers need a count-line + digest sub-block.
    (3) **TB-255 stats dashboard is web-only; cron status-report
        digest carries no top-line task/bullet/ideation aggregates**
        — same push-vs-pull surface-parity shape. `collect_stats`
        (`ap2/automation_stats.py:584`) already returns the
        aggregates; status_report.py needs a small renderer +
        wire-up.
    (4) **TB-255 went retry_exhausted → operator-update → complete on
        a `grep -cE` vs `grep -hE | wc -l` shell-bullet shape** —
        auto-unfreeze didn't fire because the agent's blocked summary
        lacked a `BriefingFix:` prefix; the operator's update at
        17:56:14Z unblocked manually. Real but conditional gap: the
        operator already rejected an enumerative shell-bullet
        validator (TB-172) and an LLM file-path-coherence check
        (TB-240), so the principled fix-shape is narrower than "add
        another pattern" — defer until n=2+ recurrence justifies the
        next intervention shape.
    (5) **Dry-run interesting-types coverage** — same defer rationale
        as last cycle.
  - Status: `in-progress`
  - Reasoning: Three concrete fillable gaps (1)/(2)/(3) — backlog is
    empty, slot count = 5, and the focus's E2E surface still has
    measurable push-vs-pull parity holes. Validator-judge timeout (1)
    is the load-bearing one for axis-1 trust.

## Non-goal risk check

None. Gaps (1)-(3) all land squarely inside the focus axes;
(4) is explicitly deferred so it can't drift.

## Considered & deferred this cycle

- **TB-255 `grep -cE` shell-bullet auto-unfreeze coverage** — see Gap
  (4). Operator-rejection patterns (TB-172, TB-240) name this exact
  failure-shape class as "whack-a-mole" / "easy to slide into cheating
  the passing criteria". Defer until ≥2 recurrence with the same
  shape — then the right shape can be authored against real evidence.
- **TB-228-shape auto-approve digest extension to include
  `would_auto_approve` 24h delta** — TB-238 already covers the
  base 24h count surface in the status-report digest. Marginal
  signal vs. the three ranked above.
- **Archive Complete tasks older than 30 days to
  `.cc-autopilot/archive/tasks-archive-YYYY-MM.md`** — 127 Complete
  is heavy in TASKS.md, but the file still parses fine and no failure
  mode is currently driven by size. Defer pending observed parser
  slowdown or operator preference signal; the operator hasn't
  surfaced this as a pain point.
- **TB-175 / TB-185 / TB-184 / TB-231 / TB-240 (recurring rejection
  patterns)** — no new evidence to re-propose any. TB-175-shape
  insight aggregator still defers per operator ack 2026-05-07T01:57Z
  pending ≥3 cycles of TB-188/TB-189 data.

## Cycle observations

- Operator drained the entire pile end-to-end within ~1.5h
  (16:19→17:45Z) including 5 unfreezes that all auto-resolved as
  "previously committed; verify-only" runs. Encouraging signal that
  the verifier's cumulative-diff path (TB-127/TB-136) handles the
  "implementation in HEAD before retry budget expired" pattern
  cleanly when the only blocker was `AP2_VERIFY_TIMEOUT_S`.
- Validator-judge timeout pattern: 100% (6/6) of recent operator
  queue-appends timed out the dep-coherence judge in the last 25h.
  This is the highest-leverage observability gap because the
  fail-open masks it — the operator sees no symptoms in normal
  flow, only the 24h count surface added by TB-243.
- Goal-anchor validator pitfall: the `_briefing_section_body` regex
  treats `[(\-—:]` as heading-line metadata, so a `## Goal` body
  whose first line begins with `(` collapses the body to empty
  string. Worth a fix-briefing or validator hardening if a future
  briefing trips it again, but the workaround is trivial (don't
  start the Goal body with `(`).

## Decisions needed from operator

(none — both prior-cycle items resolved by 2026-05-18T16:19Z
approve burst + 17:44Z unfreeze burst; no fresh
narrative-judgment decisions surfaced this cycle)

## Proposals this cycle

- TB-257 — Investigate validator-judge dep-coherence timeout (6
  events / 25h); produce categorized investigation artifact at
  `.cc-autopilot/insights/` (TB-253-shape).
- TB-258 — Surface `ap2 audit` unreviewed count on `ap2 status`
  text/JSON + cron status-report digest (push-surface parity).
- TB-259 — Surface `/stats` window aggregates (task/bullet/ideation
  top-line) in cron status-report digest (push-surface parity).

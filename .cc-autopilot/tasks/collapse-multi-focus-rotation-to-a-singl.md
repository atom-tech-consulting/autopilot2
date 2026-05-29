# Collapse multi-focus rotation to a single ideation-exhaustion halt

Tags: #autopilot #focus-advance #ideation #refactor #simplification #operator-ux

## Goal

The multi-focus rotation machinery is mostly dead weight. Ideation
does NOT scope itself to the "active" focus: `ap2/ideation.py` never
reads `active_index` / `active_title` / the focus pointer (its only
pointer interaction is the `roadmap_exhausted(cfg)` park gate), the
ideation prompt is never told which focus is active, and the
goal-anchor validator (`_goal_md_anchors_from_text`) accepts a
proposal citing ANY `## Current focus:` heading. So advancing the
pointer from focus 1 → focus 2 changes nothing about what ideation
proposes — the rotation is theatre. The one genuinely load-bearing
function buried in `focus_advance` is the **empty-cycles →
exhaustion → halt** detector: after N consecutive 0-proposal
ideation cycles, park ideation and surface a decision to the
operator. Without it, ideation burns full SDK cycles forever against
an exhausted goal.

Collapse the multi-focus rotation state machine into a single
ideation-exhaustion detector, keeping multi-focus headings in
`goal.md` purely as operator-authored prose/priority hints (the
agent already reads the whole file; the goal-anchor validator
already accepts all headings). Observable behavior is preserved —
ideation works the goal, halts after N dry cycles, the operator
resumes by editing `goal.md` (`ap2 update-goal`) or kicking
`ap2 ideate --force` — only the rotation theatre (pointer walk,
per-focus labeling, `(N of M)` display, `rewind-focus`) is removed.

Why now: confirmed dead machinery (the focus pointer doesn't steer
ideation), it sharpens the codebase right before the downstream OSS
cut (external readers shouldn't have to reverse-engineer a
non-functional rotation engine), and it directly evolves the
`roadmap_exhausted` surface TB-340 just simplified — better to land
the conceptual collapse now than carry the multi-focus pointer into
OSS. Operator-directed 2026-05-29; meta-infra simplification, no
active focus → `--skip-goal-alignment`. Builds on TB-340's
pure-predicate `roadmap_exhausted`, so `@blocked:TB-340`.

## Scope

Internals-only rewrite — the component directory name
(`ap2/components/focus_advance/`) and the env knob name
(`AP2_FOCUS_ADVANCE_EMPTY_CYCLES`) are PRESERVED to bound blast
radius (a cosmetic rename to `ideation_halt` is an explicit
follow-up, out of scope here).

- `ap2/components/focus_advance/__init__.py` — rewrite
  `_maybe_advance_focus(cfg, sdk)` into a single
  exhaustion-detection pass:
  - Count consecutive 0-proposal ideation cycles via the existing
    `_ideation_empty_against_focus`-style cycle walker, but reset
    the counting window at the most recent `goal_updated` event
    (operator edited the goal file → fresh start) instead of the
    now-removed `focus_advanced to=<title>` event.
  - When the count reaches the `AP2_FOCUS_ADVANCE_EMPTY_CYCLES`
    threshold, emit `roadmap_complete` once (keep the event name to
    avoid churning the TB-test event-type docs-drift gate + the
    TB-244 status-report digest) and set the pointer's
    `roadmap_complete_emitted` flag. Below threshold → no-op.
  - Delete the active-index advance loop, the `focus_advanced`
    rotation emission, the `active_title` sync, and the
    pointer-past-last branch (there is no pointer walk anymore).
  - The kill-switch (`AP2_FOCUS_AUTO_ADVANCE_DISABLED`, the
    component's `env_flag`) now disables the auto-halt: when set,
    the detector counts but does not emit the halt; instead surface
    the existing decisions-needed bullet so the operator halts
    manually. Preserve the env-knob name + the component manifest's
    `env_flag` wiring (TB-320) verbatim.
- `ap2/goal.py`:
  - `roadmap_exhausted(cfg, foci=None)` — redefine from
    "pointer `active_index >= len(foci)`" (TB-340) to "the
    exhaustion halt is active": `roadmap_complete_emitted` is True
    AND not superseded by a later `goal_updated`. Pointer-index
    logic goes.
  - `roadmap_complete_notice_dismissed(cfg, ...)` (added by TB-340)
    — keep; it still gates the operator nag vs the always-on parked
    state.
  - `reset_pointer_on_roadmap_extension` — repurpose to
    "reset on goal_updated": clear `empty_cycles`,
    `roadmap_complete_emitted=False`, and the dismissal marker so a
    goal-file edit resumes ideation. Called from the `update_goal`
    operator-queue drain handler.
  - Pointer schema: `active_index`, `active_title`,
    `exhausted_titles` become vestigial — drop them from the
    `load_pointer` default + the save path. `read_focus_list` /
    `parse_focus_list` STAY (multi-focus headings are still parsed
    for the agent's reading + the goal-anchor validator).
- Remove the `rewind-focus` CLI verb + its operator-queue op +
  the synthetic `focus_advanced` it emitted — its sole purpose was
  multi-focus pointer manipulation. Resume paths are now
  `ap2 update-goal` (resets the counter via the repurposed handler)
  and `ap2 ideate --force` (bypasses the gate). Update the
  CLI-verb docs-drift gate's expected verb set accordingly.
- `ap2/cli_daemon.py` (`ap2 status`), `ap2/web_home.py`,
  `ap2/status_report.py` — drop the `(N of M)` focus-position
  display and the focus-rotation activity sub-block; the focus
  line shows the goal's focus headings as a plain list plus the
  ideation state (`active` / `parked — ideation exhausted; extend
  goal.md to resume`). Keep the dismissal-aware nag suppression
  from TB-340.
- `ap2/ideation.py` — the `roadmap_exhausted(cfg)` park gate at
  L1140 is unchanged in call shape (the predicate's meaning
  changes underneath it). Fix any comment referencing the
  multi-focus pointer / `focus_advanced` reset cutoff.
- Tests — rework `ap2/tests/test_tb226_focus_rotation.py` and any
  `focus_advanced`/rotation pins to the collapsed model; keep the
  TB-340 ack-semantics pins; add a pin that `goal_updated` resets
  the exhaustion counter + clears the halt.

## Design

- **Behavior preserved, theatre removed.** Before: ideation works
  the whole goal; after K×threshold empty cycles the pointer walks
  off the end and halts. After: ideation works the whole goal;
  after threshold empty cycles it halts directly. Since the pointer
  walk never steered ideation, the only observable delta is that
  exhaustion is detected in `threshold` empty cycles instead of
  `num_foci × threshold` — strictly less wasted spend, same end
  state. Call this out as the one intentional behavior change.
- **Multi-focus stays expressive, just unmechanized.** The operator
  can still list several `## Current focus:` headings; the ideation
  agent reads them all as prose guidance and the goal-anchor
  validator accepts any of them. They are documentation +
  priority-intent, not a state machine. `goal.md`'s own
  "topmost active focus" framing should be reworded to match (the
  daemon does not sequence foci; it works the goal and halts when
  dry) — update the relevant `goal.md` authoring guidance in
  `ap2/howto.md`, not `goal.md` itself (operator-owned).
- **Resume model.** `goal_updated` is the reset signal: editing
  `goal.md` (adding/clarifying foci) clears the halt and the
  empty-cycles counter, so ideation re-engages with fresh material.
  `ap2 ideate --force` remains the immediate manual kick. This
  removes the conceptually-muddled `rewind-focus` (which only made
  sense against a pointer).
- **Event-name continuity.** Keep `roadmap_complete` as the halt
  event name despite "roadmap" no longer implying a focus sequence —
  renaming it churns the event-type docs-drift gate, the digest, and
  the attention surfaces for no functional gain. A rename is a
  cosmetic follow-up.
- **Insulation.** The running daemon imports the component at start;
  the rewrite only takes effect on the next `ap2 stop && ap2 start`.
  The pytest gate catches a broken collapse before commit.

## Verification

- `uv run pytest -q ap2/tests/` — full suite passes (scoped to
  `ap2/tests/`, the project's canonical `AP2_VERIFY_CMD`).
- `! grep -rnE "active_index|active_title|exhausted_titles" ap2/components/focus_advance/__init__.py ap2/goal.py` — the multi-focus pointer fields are gone from the collapsed detector + goal pointer logic.
- `! grep -rnE "rewind.focus" ap2/cli.py ap2/cli_daemon.py` — the rewind-focus CLI verb is removed from the parser + dispatch.
- `grep -rnE "goal_updated" ap2/components/focus_advance/__init__.py ap2/goal.py` — the exhaustion counter / reset now keys off the goal_updated signal.
- `uv run python -m ap2 --project . status 2>&1 | grep -qE "."` — `ap2 status` still renders (didn't crash on the collapsed pointer); run with the global `--project` flag BEFORE the `status` subcommand.
- `ap2/components/focus_advance/__init__.py` Prose: `_maybe_advance_focus` no longer walks `active_index` or emits `focus_advanced`; it counts consecutive empty ideation cycles since the last `goal_updated` and emits `roadmap_complete` once at the `AP2_FOCUS_ADVANCE_EMPTY_CYCLES` threshold. The `AP2_FOCUS_AUTO_ADVANCE_DISABLED` kill-switch + the component manifest `env_flag` wiring are preserved. Judge confirms via Read.
- `ap2/goal.py` Prose: `roadmap_exhausted` returns the active-halt state (`roadmap_complete_emitted` not superseded by a later `goal_updated`), with no `active_index`/`len(foci)` comparison; `read_focus_list` / `parse_focus_list` are retained for the agent + goal-anchor validator. Judge confirms via Read.
- `ap2/howto.md` Prose: the goal.md authoring guidance no longer claims the daemon sequences foci ("topmost active focus"); it states multi-focus headings are operator prose/priority hints the ideation agent reads, and that the daemon works the goal and halts after N dry ideation cycles. Resume is documented as `ap2 update-goal` / `ap2 ideate --force`; `rewind-focus` is no longer referenced. Judge confirms via Read.

## Out of scope

- Renaming the component (`focus_advance` → `ideation_halt`) or the
  env knob (`AP2_FOCUS_ADVANCE_EMPTY_CYCLES` →
  `AP2_IDEATION_EXHAUSTION_*`). Cosmetic; would churn the config
  schema + docs gates. Follow-up.
- Renaming the `roadmap_complete` event. Follow-up (churns the
  event-type docs-drift gate + digest + attention surfaces).
- Changing the empty-cycle threshold default or the cycle-detection
  semantics (entry/exit markers, proposal-recorded reset) — the
  counting primitive is reused as-is; only its reset cutoff
  (`goal_updated` instead of `focus_advanced`) changes.
- Editing `goal.md` itself (operator-owned, fenced) — the framing
  correction lands in `ap2/howto.md`'s authoring guidance only.
- The downstream OSS-distribution focus + the `ap2/core/` symmetry
  and component-body-out-of-`__init__.py` polish — separate.

# Fix `ack roadmap_complete` semantics: dismiss-the-notice, never resume-ideation

Tags: #autopilot #bug #roadmap-complete #focus-advance #ideation #operator-ux

## Goal

`ap2 ack roadmap_complete` has backwards semantics and a latent
stale-state bug. Operator intuition for "ack roadmap_complete" is
*"I acknowledge the roadmap is done — quiet the nag, leave ideation
parked."* The implementation does the opposite: the ack flips
`goal.roadmap_exhausted(cfg)` to **False**, which **un-parks
ideation** — so it resumes running full ~$1-3 SDK cycles every
cooldown against an already-exhausted roadmap, finding nothing.

Worse, `roadmap_exhausted()` (`ap2/goal.py:554-627`) folds the ack
check into the gate predicate via a forensic
`roadmap_complete_ack_idx` fallback (L624-626: `ack_idx >= total →
return False`) that has **no recency check**. After an
extend→re-exhaust cycle where the focus count is unchanged, a stale
`ack_idx` from the PRIOR extension's ack makes `roadmap_exhausted()`
return False even with a fresh, un-acked `roadmap_complete` event on
disk — so ideation auto-resumes the wasteful cycling with no operator
action at all. Observed live 2026-05-29: ack_idx=2 set by the
2026-05-28T20:34 ack, a fresh roadmap_complete emitted
2026-05-29T18:10, and ideation ran full cycles at 20:09/20:12 instead
of cheap-skipping.

The root design error: `roadmap_exhausted()` conflates two
independent questions — (1) "is the pointer past the last focus?"
(the gate that should park ideation) and (2) "has the operator
dismissed the notice?" (a UX/surfacing concern). Resuming ideation
is properly a *pointer move* — `ap2 rewind-focus <title>` (re-work an
exhausted focus) or `ap2 update-goal` (extend with a new focus, which
resets the pointer). The ack should touch neither the gate nor the
pointer.

Fix: make `roadmap_exhausted()` a pure function of the pointer
(`active_index >= len(foci)`); make `ack roadmap_complete` set a
dismissal marker that only suppresses the operator-facing nag, never
the ideation park; re-arm the nag when a fresh `roadmap_complete`
emits; and correct all the operator-facing hint text + docs that
currently tell the operator `ack` "resumes" (it should say `rewind-focus`
/ `update-goal` resume; `ack` dismisses-and-stays-parked).

Why now: confirmed live cost leak — ideation burns a full SDK cycle
(~$1-3) every `AP2_IDEATION_COOLDOWN_S` window (~2h) against an
exhausted roadmap, indefinitely, because the stale-ack_idx fallback
defeats the cheap-skip. It recurs on every future extend→re-exhaust.
And the backwards ack semantics actively mislead the operator (the
`ap2 status` hint says `ack ... to dismiss`, but ack un-parks). This
is a correctness + UX + cost bug in one. Operator-directed fix
(2026-05-29); meta-infra work with no active focus, so
`--skip-goal-alignment`.

## Scope

- `ap2/goal.py` `roadmap_exhausted(cfg, foci=None)` — reduce to a
  pure predicate: `total == 0 → False`; else `pointer["active_index"]
  >= total`. Delete the events-scan (L586-620) and the forensic
  `ack_idx >= total` fallback (L621-626) entirely. The ack no longer
  participates in this predicate.
- `ap2/goal.py` — add a sibling helper
  `roadmap_complete_notice_dismissed(cfg, foci=None) -> bool` that
  returns True iff `roadmap_exhausted` AND the pointer's dismissal
  marker matches the current foci count (the "operator has dismissed
  THIS exhaustion episode" check). This is the ONLY consumer of the
  dismissal marker; it gates surfacing, not parking.
- `ap2/components/focus_advance/__init__.py` `_maybe_advance_focus`
  — in the branch that emits `roadmap_complete` (the
  `active_idx >= len(foci) and not roadmap_complete_emitted` block,
  ~L318-363), also clear the dismissal marker
  (`pointer["roadmap_complete_ack_idx"] = None`) so each fresh
  exhaustion episode re-nags exactly once even if a prior episode at
  the same focus count was dismissed. This is the core stale-state fix.
- `ap2/operator_queue.py` — the ack drain handler (L265-277) keeps
  setting the dismissal marker on the `roadmap_complete` token, but
  update the comment to state the corrected semantics: this DISMISSES
  the recurring notice, it does NOT resume ideation (resume is
  `rewind-focus` / `update-goal`).
- `ap2/cli_daemon.py` (`ap2 status` focus line, ~L351 + L555-572),
  `ap2/web_home.py` (roadmap-complete card, ~L580-619),
  `ap2/status_report.py` (digest roadmap-complete line, ~L416 +
  L476) — three changes each: (a) only show the "decision needed:
  extend the roadmap" nag when `roadmap_complete_notice_dismissed`
  is False; (b) rewrite the hint text from
  "`ap2 update-goal` to resume or `ap2 ack roadmap_complete` to
  dismiss" to distinguish the three operations:
  `ap2 update-goal` (extend roadmap → resume on new focus),
  `ap2 rewind-focus <title>` (resume on an exhausted focus),
  `ap2 ack roadmap_complete` (dismiss this notice; ideation stays
  parked); (c) keep the always-on ROADMAP_COMPLETE state indicator
  regardless of dismissal (dismissal quiets the nag, not the state).
- `ap2/goal.py` module docstring (L22-24) + `roadmap_exhausted`
  docstring + `ap2/components/focus_advance/__init__.py` module
  docstring (L40-49) — correct the workflow description: ack =
  dismiss-stay-parked; resume = repoint.
- `ap2/howto.md` — update the operator-playbook / roadmap-complete
  recovery section to describe the corrected three-verb model.
- `ap2/ideation.py` — fix the stale comment at L1126-1128 / L1162-1163
  that describes "`ap2 ack roadmap_complete && ap2 update-goal`" as
  the recovery path; the ack is not part of resume.

## Design

- **`roadmap_exhausted` becomes pure pointer state.** Resume is a
  pointer move: `rewind-focus` sets `active_index` back inside range
  → predicate returns False naturally; `update-goal` calls
  `reset_pointer_on_roadmap_extension` (resets active_index +
  `roadmap_complete_emitted=False`) → False naturally. No code path
  needs the ack to flip the gate.

- **Dismissal marker reuses the existing `roadmap_complete_ack_idx`
  field** (no new pointer field / schema bump): semantics change
  from "halt cleared at N foci" to "notice dismissed at N foci."
  `roadmap_complete_notice_dismissed` returns
  `roadmap_exhausted(cfg) and pointer.get("roadmap_complete_ack_idx")
  == len(foci)`. focus_advance resets it to None on each fresh
  `roadmap_complete` emit, so dismissal is per-episode and can't go
  stale across an extend→re-exhaust.

- **Why reset-on-emit rather than recency-scan.** The deleted
  events-scan (L592-620) tried to compare ack-vs-halt ordering in
  the tail; it was correct in isolation but the forensic fallback
  silently overrode it. Resetting the marker at emit time makes the
  single forensic field authoritative and removes the dual-source
  ambiguity that caused the bug — one writer (focus_advance clears),
  one writer (ack sets), one reader (the dismissed predicate).

- **Surfacing vs state.** The `ap2 status` focus line should still
  show `ROADMAP_COMPLETE` whenever `roadmap_exhausted` (so the
  operator always knows the daemon is parked); only the *actionable
  nag* ("decision needed: extend the roadmap") is suppressed by
  dismissal. Same split for the web card and the cron digest line.

- **No behavior change to task dispatch or auto-approve.**
  `roadmap_exhausted` already gates only ideation + display (verified:
  callers are `ideation.py:1140`, `cli_daemon.py:351`,
  `web_home.py:609`; `daemon.py:2165` is a comment). Operator-added
  Backlog tasks continue to auto-promote and dispatch during the
  halt, unchanged.

## Verification

- `uv run pytest -q` — full suite passes.
- `uv run pytest -q ap2/tests/test_tb226_focus_rotation.py` — the
  existing focus-rotation pins pass (adjust expectations for the
  corrected semantics where a pin asserted ack-clears-the-gate;
  document any such expectation flip in the test's docstring).
- New `ap2/tests/test_roadmap_ack_semantics.py` with these pins:
  - `roadmap_exhausted` returns True when `active_index >= len(foci)`
    regardless of any `operator_ack[roadmap_complete]` in the events
    tail (ack does NOT clear the gate).
  - After a fresh `roadmap_complete` emit, a dismissal marker set by
    a PRIOR episode at the same foci count does NOT suppress the nag
    (the stale-state regression pin — this is the exact 2026-05-29
    bug).
  - `roadmap_complete_notice_dismissed` returns True only after an
    ack for the CURRENT episode, and False again after the next fresh
    `roadmap_complete` emit.
  - `rewind-focus` (pointer back in range) and a simulated
    `update-goal` pointer reset both make `roadmap_exhausted` return
    False without any ack.
- `! grep -nE "ack roadmap_complete.{0,40}(resume|&& ap2 update-goal)" ap2/ideation.py` — the stale "ack ... resume" recovery comment is gone from ideation.py.
- `grep -rnE "rewind-focus" ap2/cli_daemon.py ap2/status_report.py ap2/web_home.py` — the corrected hint text naming `rewind-focus` as a resume path is present in all three operator surfaces.
- `ap2/goal.py` Prose: `roadmap_exhausted` is a pure function of
  `pointer["active_index"]` vs `len(foci)` (plus the `total==0`
  guard) — no `events.tail` call, no `roadmap_complete_ack_idx` read
  inside it. The dismissal marker is read only by the separate
  `roadmap_complete_notice_dismissed` helper. Judge confirms via Read.
- `ap2/components/focus_advance/__init__.py` Prose: the
  `roadmap_complete`-emit branch clears
  `pointer["roadmap_complete_ack_idx"] = None` so a fresh exhaustion
  episode re-arms the nag. Judge confirms via Read.
- `ap2/howto.md` Prose: the roadmap-complete recovery section
  describes the three-verb model — `update-goal` (extend → resume on
  new focus), `rewind-focus` (resume on an exhausted focus), `ack
  roadmap_complete` (dismiss the notice; ideation stays parked) — and
  no longer says `ack` resumes ideation. Judge confirms via Read.

## Out of scope

- Renaming the `roadmap_complete_ack_idx` pointer field to something
  like `roadmap_complete_dismissed_idx`. The rename is cosmetically
  nicer but would churn the pointer schema + every test that
  references the field by name; the corrected semantics are captured
  in the helper names and comments. A follow-up cosmetic rename can
  land separately if desired.
- Changing the empty-cycles advance heuristic or
  `AP2_FOCUS_ADVANCE_EMPTY_CYCLES` (TB-283/292/300) — this fix is
  purely about the gate/ack/dismissal semantics, not when foci
  advance.
- Adding a brand-new `ap2 dismiss` verb distinct from `ap2 ack`. The
  token-in-note ack mechanism (`ap2 ack "roadmap_complete — ..."`)
  stays; only its effect is corrected.
- The structured-config TOML migration of any
  roadmap/focus knobs (separate focus, already shipped where
  applicable).

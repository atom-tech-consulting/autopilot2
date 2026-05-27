# Stop appending roadmap_complete bullet to ideation_state.md (focus line is already redundant)

Tags: #autopilot #focus-advance #ideation-state #priming #commit-hygiene #regression-pin #bug

## Goal

Remove the `_append_decisions_needed_bullet(cfg, "Roadmap complete: ...")` 
call from `ap2/focus_advance.py:_maybe_advance_focus`'s 
roadmap-complete branch (the code path that fires when 
`active_idx >= len(foci)` and `roadmap_complete_emitted` is False). 
Keep the sibling `events.append(cfg.events_file, "roadmap_complete", ...)` 
emission and `pointer["roadmap_complete_emitted"] = True` state update 
unchanged. Closes the goal.md `## Done when` failure mode 
"Ideation reliably proposes goal-aligned next steps that substantively 
advance the goal (not just goal-shaped pro-forma compliance)" — without 
this fix, the daemon's appended bullet acts as residual verdict-language 
priming for any subsequent ideation cycle (after operator extends the 
roadmap) AND accumulates uncommitted edits in `ideation_state.md` 
between cycles, breaking the single-writer invariant the file's 
post-cycle snapshot/commit pipeline assumes.

Why now: 2026-05-27 — investigation of `git status` showing 
uncommitted `ideation_state.md` changes traced the diff to a single 
line added by `_append_decisions_needed_bullet`: 
`- Roadmap complete: all 2 \`## Current focus:\` heading(s) in 
\`goal.md\` are exhausted. Ideation is parked (no active focus); 
extend the roadmap...`. Two coupled bugs surface from this single 
write:

(1) **Priming leak past the scrub.** The post-write scrub to remove 
exhaustion-asserting sentences from `ideation_state.md` runs INSIDE 
`_run_ideation` after the agent's `ideation_state_write` MCP call. 
The daemon's `_append_decisions_needed_bullet` runs in 
`_maybe_advance_focus` AFTER `_run_ideation` returns — bypassing the 
scrub. The bullet is exactly the verdict-language pattern the scrub 
catches (asserts exhaustion, names conditions of exhaustion, claims 
the operator should extend the roadmap), so the bypass directly 
undoes the priming guarantee the scrub was designed to provide.

(2) **Uncommitted working-tree drift.** The 
`ap2/daemon.py:_changed_state_paths` snapshot diff only captures 
edits that happen DURING `_run_ideation`. Edits from 
`_maybe_advance_focus` happen outside that window, so they never 
ride along in a `state: ideation` commit. The working tree 
diverges from committed state over time; `ap2 rollback`'s 
"walk back N commits" semantics doesn't restore working-tree 
parity; `git status` is noisy.

The fix is the smallest possible surgical change: drop the single 
`_append_decisions_needed_bullet` call. The roadmap-complete signal 
is already surfaced redundantly via FIVE other channels that do not 
have either bug: (a) `focus_advanced` event in events.jsonl, 
(b) `roadmap_complete` event in events.jsonl, (c) `focus_pointer.json` 
(`active_index past end`, `exhausted_titles`, 
`roadmap_complete_emitted=true`, empty `active_title`), 
(d) `ap2 status`'s focus line ("focus: ROADMAP_COMPLETE — 
ideation parked; `ap2 update-goal` to resume or `ap2 ack 
roadmap_complete` to dismiss") which is derived from 
focus_pointer.json and names both action verbs the operator needs, 
(e) the TB-244 focus-rotation digest in the cron status-report. The 
decisions-needed bullet duplicates surface (d) word-for-word. Other 
callers of `_append_decisions_needed_bullet` (`auto_unfreeze.py`'s 
daily-cap halt, `daemon.py:2296`'s TB-224 task_error halt) stay 
unchanged — they surface conditions that are NOT redundantly 
signaled elsewhere.

## Scope

(1) `ap2/focus_advance.py:_maybe_advance_focus`: remove the 
`_append_decisions_needed_bullet` call (currently around L148-165) in 
the `if active_idx >= len(foci):` branch. Keep the surrounding 
`events.append(cfg.events_file, "roadmap_complete", ...)` emission 
unchanged. Keep `pointer["roadmap_complete_emitted"] = True` and 
`goal.save_pointer(cfg, pointer)` unchanged. The try/except OSError 
wrapper around the bullet append can also be removed in the same 
edit.

(2) Module docstring update: the file-header comment in 
`ap2/focus_advance.py` documenting roadmap-complete behavior 
("emit `roadmap_complete` + a `## Decisions needed from operator` 
bullet so `ap2 status` and the web home page surface the 
parked-ideation state") needs revising — drop the "decisions-needed 
bullet" half. The pointer-driven focus-line surface is the canonical 
signal post-fix.

(3) `ap2/focus_advance.py` import cleanup: if 
`_append_decisions_needed_bullet` was imported only for the 
roadmap-complete call and not for the kill-switch path 
(`advance_disabled` branch around L232-252 still uses it for the 
operator-killed-but-criteria-met surface), keep the import. Verify 
by grep before removing.

(4) New regression-pin module 
`ap2/tests/test_roadmap_complete_no_bullet_append.py` covering: 
  - When `_maybe_advance_focus` advances past the last focus, the 
    `roadmap_complete` event IS emitted (events.jsonl has the entry).
  - `pointer["roadmap_complete_emitted"]` IS set to True.
  - `ideation_state.md` is NOT modified (no `Roadmap complete:` 
    bullet appended; file bytes unchanged across the call).
  - Subsequent ticks (still past-last-focus, 
    `roadmap_complete_emitted=true`) emit NO duplicate 
    `roadmap_complete` events and don't modify `ideation_state.md`.
  - The kill-switch path (`AP2_FOCUS_AUTO_ADVANCE_DISABLED=1` set 
    when criteria would advance) DOES still emit a `## Decisions 
    needed from operator` bullet via `_append_decisions_needed_bullet` 
    — that's a different code path with a different signal (criteria 
    met but advance blocked), and the operator genuinely needs to see 
    it on `ap2 status` to know to `ap2 update-goal` manually.

(5) Audit `_auto_diagnose_fired` watchdog and `ap2 status` text 
rendering to confirm neither requires the `Roadmap complete:` bullet 
specifically (vs reading from `focus_pointer.json` / `events.jsonl`). 
If either ONLY reads from the bullet, surface the regression in tests 
and plan a follow-up; but the expectation is that both already have 
direct pointer/event access (the focus line in `ap2 status` already 
pulls from `focus_pointer.json`, so the parser surface is 
established).

## Design

Single-line removal in a single branch. The roadmap_complete SIGNAL 
is preserved in five other surfaces, all of which are either 
authoritative state (the pointer file) or append-only audit trail 
(events.jsonl); none have the uncommitted-drift or unscrubbed-priming 
issues. The decisions-needed channel is preserved for OTHER callers 
that need it (halts that lack a dedicated surface line).

The split between "focus exhaustion: redundant via focus line" and 
"operator-action halts: need explicit bullet" is the right semantic 
boundary. Focus exhaustion is a naturally observable state (the 
pointer says so, the focus line displays it). Halts are mid-cycle 
conditions with no naturally observable surface; the bullet is the 
only push channel. Keeping `_append_decisions_needed_bullet` 
available for halts while removing the roadmap-complete redundancy 
aligns the helper's use with its semantic purpose.

The previous TB-275 already scoped roadmap-complete to an 
"ideation-trigger gate only" — task dispatch continues, operator 
surfaces the state, no full-stop side effects. This TB completes 
that scoping by stopping the daemon from writing to a file the 
agent owns. Post-fix, `ideation_state.md` becomes single-writer 
(only the ideation agent writes it via `ideation_state_write` MCP), 
restoring the snapshot/commit pipeline's invariant.

## Verification

- `! grep -A5 'roadmap_complete_emitted' ap2/focus_advance.py | grep -q '_append_decisions_needed_bullet'` — the call is removed from the roadmap-complete branch (no `_append_decisions_needed_bullet` reference within 5 lines after a `roadmap_complete_emitted` line).
- `grep -q 'events.append' ap2/focus_advance.py` — the `roadmap_complete` event emission stays.
- `grep -q 'roadmap_complete_emitted.*True' ap2/focus_advance.py` — the pointer-state mutation stays.
- `test -f ap2/tests/test_roadmap_complete_no_bullet_append.py` — regression-pin module exists.
- `uv run pytest -q ap2/tests/test_roadmap_complete_no_bullet_append.py` — module passes.
- `uv run pytest -q ap2/tests/test_tb226_focus_rotation.py` — pre-existing focus-rotation tests pass against the modified roadmap-complete branch.
- `uv run pytest -q` — full suite passes.

## Out of scope

- Restructuring `_append_decisions_needed_bullet` or its callers — 
  the helper stays as-is for halt-style callers that genuinely need 
  the bullet surface.
- Adding an explicit `ap2 status` line for the auto_unfreeze 
  daily-cap halt or the TB-224 task_error halt (those conditions 
  don't have a dedicated focus-line equivalent, so the bullet 
  surface is still load-bearing for them).
- Auto-clearing the bullet on `ap2 ack roadmap_complete` (no bullet 
  write means no auto-clear needed).
- Backfilling git history to commit the orphan `ideation_state.md` 
  edit — operator can `git checkout` it manually or let the next 
  ideation cycle overwrite (which it will).
- Changing the focus line message in `ap2 status` — the current 
  phrasing is already operator-actionable as-is.
- Removing or reshaping the `ideation_state.md` file format itself 
  — only one write path is being removed; the file's structure stays.

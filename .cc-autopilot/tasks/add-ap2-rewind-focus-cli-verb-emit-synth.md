# Add `ap2 rewind-focus` CLI verb; emit synthetic `focus_advanced` event for counter cutoff

Tags: #autopilot #operator-cli #focus-advance #empty-cycles #recovery #regression-pin #bug

## Goal

Add a first-class operator-CLI verb `ap2 rewind-focus <title>` that
atomically (a) updates `.cc-autopilot/focus_pointer.json` to re-engage
an exhausted focus, (b) emits a synthetic `focus_advanced
trigger=operator_rewind` event so the empty-cycles counter's cutoff
logic respects the rewind, and (c) writes an operator_log.md audit
line. Routes through the operator queue like other operator CLI verbs
(`ap2 approve` / `ap2 update-goal`) so the mutation lands at a tick
boundary, never mid-task-run. Closes the goal.md `## Done when` failure
mode "Ideation reliably proposes goal-aligned next steps that
substantively advance the goal (not just goal-shaped pro-forma
compliance)" — without this verb, operator recovery from a false
`focus_advanced` requires direct edit of the gitignored
`focus_pointer.json`, which produces NO event emission and leaves
pre-rewind empty cycles counting toward the rewound focus's counter;
the recovery path is itself buggy and lets the false-advance fire
again after just one truly-empty post-rewind cycle.

Why now: 2026-05-26 false-advance incident — operator (me) manually
edited `.cc-autopilot/focus_pointer.json` to recover the prematurely-
advanced focus. The edit succeeded as state mutation but emitted no
event. The empty-cycles counter
(`ap2/focus_advance.py:_ideation_empty_against_focus`) scans for the
most-recent `focus_advanced to=<focus_title>` event to set its
cutoff_idx; with no rewind event, `cutoff_idx = -1` and the counter
walks the entire 200-event tail, picking up pre-rewind
`ideation_empty_board` + `ideation_complete` pairs as if they belonged
to the rewound focus. Today's false trip required four compounding
bugs (bug 1: entry+exit double-count, bug 2: queue-path proposal
desync, bug 4: queue-drain missed auto-approve gate, bug 3: this
one). TB-291 / TB-292 / TB-293 closed bugs 1, 2, and 4. Bug 3
(this TB) remains the only path by which the counter can still
falsely trip after a manual operator recovery, leaving the recovery
path brittle. With the new verb, the operator gets a clean recovery
surface that respects the audit trail AND the counter's window
semantics; direct `focus_pointer.json` edits become a "don't" rather
than the documented recovery path.

## Scope

(1) `ap2/cli.py`: register `rewind-focus` verb in the argument parser
alongside the existing operator-CLI verbs (`approve`, `reject`,
`update-goal`, `ack`, etc.). Required argument: positional `<title>`
matching one of the `## Current focus:` headings in goal.md.
Optional: `--reason <text>` to capture operator intent (mirrors
`ap2 update-goal --reason`'s contract for audit-log clarity).

(2) `ap2/cli_board.py` (or whichever module hosts the analogous
`approve` / `reject` handlers — verify the existing layout): add a
`cmd_rewind_focus(cfg, args)` handler. The handler validates that
the title argument matches one of goal.md's `## Current focus:`
headings (rejects with non-zero exit + message naming the available
titles if not), looks up the target index, and submits a
`rewind_focus` op to the operator queue via the standard
`do_operator_queue_append` shape.

(3) `ap2/operator_queue.py`: add `"rewind_focus"` to the op-name
registry (the schema validation set near L325 + the apply map). In
`_apply_operator_op` (around L1285), add a new branch matching the
op name. The handler:
  - Re-reads goal.md to resolve the target title to a current index
    (`goal.read_focus_list(cfg)`). Reject with `RuntimeError` if the
    title no longer matches a current heading (operator may have
    edited goal.md between CLI invocation and drain).
  - Reads `focus_pointer.json` via `goal.load_pointer(cfg)`.
  - Captures the old `active_title` for the event payload.
  - Updates `pointer["active_index"]`, `pointer["active_title"]`,
    `pointer["exhausted_titles"]` (remove the target title if
    present), `pointer["roadmap_complete_emitted"] = False`,
    `pointer["empty_cycles"] = 0`, `pointer["updated_ts"]`.
  - Saves via `goal.save_pointer(cfg, pointer)`.
  - Emits `focus_advanced` event with payload
    `from=<old_title>`, `to=<target_title>`,
    `trigger="operator_rewind"`,
    `new_index=<target_index>`,
    `total_foci=<len(foci)>`,
    `reason=<operator_reason_or_empty>`.
  - Appends `<ts> — operator rewound focus pointer (<old> → <target>): <reason>` to `operator_log.md`.

(4) `ap2/events.py`: extend the `focus_advanced` event's documented
`trigger` set to include `operator_rewind` alongside the existing
`empty_cycles_heuristic` and `pointer_past_last`. Comment update only;
the event type itself doesn't change shape.

(5) `ap2/howto.md`: document the new verb in the operator-CLI section,
including: (a) when to use it (recovering from a stuck or
falsely-advanced focus state), (b) audit-trail behavior (event +
operator_log line), (c) explicit guidance that direct
`focus_pointer.json` edits are now a "do not" — the verb is the
canonical recovery path because it preserves counter-window
semantics.

(6) Regression-pin module `ap2/tests/test_rewind_focus.py` covers:
  - CLI verb registered and accessible via `ap2 rewind-focus`.
  - Handler validates target title against goal.md's
    `## Current focus:` headings; rejects unknown titles with
    non-zero exit.
  - Drain-side `_apply_operator_op` `rewind_focus` branch updates
    `focus_pointer.json` correctly: `active_index` matches the
    target, `active_title` matches, `exhausted_titles` no longer
    includes the target, `roadmap_complete_emitted = False`,
    `empty_cycles = 0`.
  - Drain emits `focus_advanced trigger=operator_rewind` with the
    documented payload fields (`from`, `to`, `new_index`,
    `total_foci`, `reason`).
  - `operator_log.md` receives the audit line.
  - Empty-cycles counter respects the synthetic cutoff: feed a
    seeded events tail with 2 pre-rewind empty cycles + 1
    `focus_advanced trigger=operator_rewind to=<title>` event + 1
    post-rewind empty cycle; assert
    `_ideation_empty_against_focus(tail, title) == 1` (not 3).
  - Title-resolution race: operator-edited goal.md drops the
    target title between CLI invocation and drain → drain rejects
    with a meaningful error; pointer left unmodified.

## Design

Route through the operator queue rather than direct mutation in the
CLI handler. Same rationale as `ap2 approve` / `ap2 update-goal`: the
operator queue is the canonical "operator intent → daemon-tick apply"
surface. The drain happens at a tick boundary, so the mutation
doesn't race against an in-flight task agent or ideation cycle.

Title-as-key, not index-as-key. Operators don't reliably remember
foci by index; titles are stable text in goal.md. The handler resolves
title → index at drain time (not CLI time), so an operator-edited
goal.md between invocation and drain produces a clean rejection rather
than silently rewinding to the wrong focus.

The synthetic `focus_advanced` event reuses the existing event type
(payload shape stays the same; only the `trigger` value is new).
Reuses the existing counter cutoff logic verbatim — the counter looks
for `focus_advanced to=<focus_title>` regardless of `trigger`, so the
cutoff fires correctly on the synthetic event without any counter-side
code change. That keeps this TB scoped to operator surface + event
emission; no `focus_advance.py` touch needed.

Operator-log line shape mirrors `ap2 update-goal`'s existing audit
shape (`<ts> — operator updated goal.md (<reason>)`) for consistency
across operator-CLI verbs.

Three policy choices worth flagging:

- **`empty_cycles` reset to 0 explicitly.** Even though the counter
  recomputes from the events tail each tick, the forensic field in
  `focus_pointer.json` should reflect the rewind so `ap2 status` and
  the web UI surface a consistent post-rewind state without waiting
  for the next tick.
- **`roadmap_complete_emitted = False`.** Rewinding past
  roadmap-complete clears the operator-decisions bullet eligibility
  on the next emit cycle. If the rewound focus then naturally
  exhausts, a fresh `roadmap_complete` event fires cleanly.
- **No automatic `ap2 ack roadmap_complete`.** The rewind verb does
  NOT clear the decisions-needed bullet. Operator should clear via
  `ap2 ack roadmap_complete` separately if desired. Keeps the two
  verbs orthogonal; a future operator may want to rewind while
  preserving the historical roadmap-complete notice for audit.

## Verification

- `ap2 --project /Users/claude-agent/repos/autopilot2 rewind-focus --help 2>&1 | grep -qi 'rewind'` — CLI verb registered.
- `grep -q '"rewind_focus"' ap2/operator_queue.py` — drain-side op handler registered.
- `grep -q 'operator_rewind' ap2/events.py` — trigger value documented.
- `grep -q 'rewind-focus' ap2/howto.md` — operator docs cover the verb.
- `test -f ap2/tests/test_rewind_focus.py` — regression-pin module exists.
- `uv run pytest -q ap2/tests/test_rewind_focus.py` — module passes.
- `uv run pytest -q` — full suite passes.

## Out of scope

- Auto-detect operator intent to rewind in `ap2 update-goal` (option
  2 from the design discussion). Rejected for clarity — operators
  should explicitly invoke the rewind verb, not have it inferred from
  a goal.md edit shape.
- Changing the counter's cutoff logic (option 3 from the design
  discussion). Rejected because reusing the existing
  `focus_advanced` event is cleaner than introducing a secondary
  cutoff mechanism (pointer mtime / pointer.updated_ts).
- A general-purpose `ap2 set-pointer` / `ap2 edit-pointer` verb that
  accepts arbitrary pointer-field mutations. Rejected — `rewind-focus`
  is the one legitimate operator operation on the pointer; arbitrary
  edits should remain a "don't, file a bug" path.
- Auto-acking `roadmap_complete` as part of the rewind. Operator
  controls that separately via `ap2 ack` (see Design note).
- Renaming or deprecating direct `focus_pointer.json` edits. The file
  stays as the canonical state; the verb is the canonical mutation
  path. Documentation update names direct edits as a non-supported
  recovery shape going forward.
- A `--dry-run` mode for `rewind-focus`. The verb is idempotent and
  reversible (run again to rewind to a different focus, or run
  through the natural advance flow); a dry-run flag adds surface for
  marginal value.

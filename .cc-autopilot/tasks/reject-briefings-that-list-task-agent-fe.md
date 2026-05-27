# Reject briefings that list TASK_AGENT_FENCED_PATHS in `## Scope`

Tags: #autopilot #briefing-validator #queue-append #regression-pin

## Goal

TB-306 burned ~5 dispatches and ~$7 in tokens because its `## Scope`
section listed `.cc-autopilot/cron.yaml` as agent-side work — but
that path lives in `TASK_AGENT_FENCED_PATHS` (`ap2/tools.py:1270`)
and the daemon's `_task_disallowed_tools` (`ap2/daemon.py:61`) wires
it into the SDK's `--disallowedTools` for both `Edit` and `Write`.
The agent correctly self-reported `blocked` on every retry, the
verifier kept marking the unsatisfiable bullet as fail, and the
daemon kept re-promoting the task until retry_exhausted — exactly
the goal.md "Current focus: operator-legible reporting and
monitoring" pull-surface failure mode that should be pre-empted
at queue-append time, not discovered via expensive dispatch loops.
It also violates the goal.md Done-when bullet "Failure recovery
(verification fails, retries exhaust, daemon restart, cron drift,
agent timeouts) is fully automatic; only genuine design forks
escalate": a fenced-path-in-Scope briefing is a preventable
structural error, not a design fork, and the current pipeline
silently routes it through retries-exhaust + manual-close.

Add a check to `_validate_briefing_structure` in
`ap2/briefing_validators.py` that scans the briefing's `## Scope`
body for backtick-fenced (codespan) path tokens, compares them
against `TASK_AGENT_FENCED_PATHS` (canonical list from
`ap2/tools.py`), and rejects the briefing at queue-append time if
any fenced path appears. Error message names the offending path,
points at the operator-CLI alternative when one exists (e.g.
`ap2 cron edit` for `.cc-autopilot/cron.yaml`,
`ap2 update-goal` for `goal.md`), and suggests moving the work
to `## Out of scope` otherwise. Mirrors the TB-171 manual-bullet
rejection pattern in shape and rationale.

Why now: TB-306 just demonstrated the failure mode with explicit
audit-trail evidence (operator_log entry at 21:30Z + briefing-shape
lesson committed). Without this gate, the same shape will recur
the next time an operator-or-ideation briefing lists a fenced path
— and the only correction surface today is post-mortem manual
close. Independently surfaced by operator audit 2026-05-27
immediately after TB-307.

## Scope

- `ap2/briefing_validators.py` — add a new helper
  `_validate_no_fenced_paths_in_scope(briefing_text)` that returns
  an error string or None, mirroring the shape of the existing
  per-check helpers. Wire it into `_validate_briefing_structure`
  after the TB-171 manual-bullet check (check #6) so its position
  in the seven-check pipeline is stable. Import
  `TASK_AGENT_FENCED_PATHS` from `ap2.tools` at module scope.

- `ap2/tests/test_briefing_validators.py` (or the existing
  per-validator test module) — add two pin tests:
  one positive (a briefing whose Scope lists
  `.cc-autopilot/cron.yaml` is rejected, error message names the
  path + suggests `ap2 cron edit`), one negative (a briefing whose
  Scope lists only agent-writable paths passes the check). Plus
  one regression-pin test asserting that a path listed in
  `## Out of scope` does NOT trip the check (only `## Scope` is
  scanned).

## Design

- **Path extraction.** Walk the `## Scope` section body with the
  same section-extraction helper the existing validators use
  (`_briefing_section_body` or equivalent), then regex-match
  backtick-fenced tokens. Use a single-or-double-backtick codespan
  pattern (`` `path` `` or `` `` `path` `` ``), then strip
  backticks to get the literal token. Tolerate prefix slashes
  (`/.cc-autopilot/cron.yaml` → match the trailing
  `.cc-autopilot/cron.yaml`).

- **Match semantics.** Substring-match each extracted token
  against each entry in `TASK_AGENT_FENCED_PATHS`. Exact match is
  safer than substring but loses some shapes (a Scope bullet
  saying "the .cc-autopilot/cron.yaml file" doesn't backtick the
  path — but operators usually do). Start with exact-codespan
  match; a fuzzier follow-up can land if false-negatives surface.
  For directory entries like `.cc-autopilot/tasks/` and
  `.cc-autopilot/ideation_proposals`, match any path that
  starts-with that prefix.

- **Suggested-fix map.** A small `dict[str, str]` co-located with
  the new helper maps each fenced path to its operator-CLI
  alternative when one exists:
    - `.cc-autopilot/cron.yaml` → `ap2 cron edit ...`
    - `goal.md` → `ap2 update-goal`
    - `TASKS.md` → use the operator queue (`ap2 add` / `ap2 unfreeze` / etc.)
    - `CLAUDE.md` → manual operator edit (no CLI; project-owned)
    - Default for paths without a CLI alternative: "move this
      work to `## Out of scope` and have an operator do it
      manually."

- **Error message shape.** Mirror the existing manual-bullet
  rejection message style:
    "briefing structure invalid: `## Scope` references `<path>`
    which is in TASK_AGENT_FENCED_PATHS (the task agent's SDK
    --disallowedTools includes Edit/Write on this path).
    <suggested-fix-line>. Move the agent-uncoverable work to
    `## Out of scope`."
  Include the path verbatim so the rejected operator/ideation can
  grep for it without disambiguation.

- **Why scan only `## Scope` (not Design / Verification / Why-now).**
  `## Scope` is the contract of what the agent will edit. Mentions
  of fenced paths in Design ("the daemon's cron.yaml ticks every
  N seconds") or Verification ("grep cron.yaml content") are
  legitimate — the agent reads but doesn't edit those. The
  manual-bullet check is similarly scoped to `## Verification`
  only.

- **Skip-goal-alignment interaction.** The TB-170 escape hatch
  bypasses goal-anchor + Why-now only. The fenced-path check
  runs regardless — there's no legitimate operator scenario
  where the task agent SHOULD edit a fenced path; the path lives
  in the fence precisely because the daemon owns it.

## Verification

- `uv run pytest -q ap2/tests/` — full suite passes after the
  new helper + tests land.

- `grep -q "def _validate_no_fenced_paths_in_scope" ap2/briefing_validators.py`
  — the new helper exists by name.

- `grep -q "TASK_AGENT_FENCED_PATHS" ap2/briefing_validators.py`
  — the canonical list is imported / referenced in the new code.

- `[ "$(grep -lE 'def test_.*fenced.*scope|def test_scope.*fenced' ap2/tests/ -r | wc -l)" -ge 1 ]`
  — at least one pin test for the new check exists in ap2/tests/.

- `ap2/briefing_validators.py` Prose: `_validate_briefing_structure`
  invokes `_validate_no_fenced_paths_in_scope` AFTER the existing
  manual-bullet check and returns its error string when non-None,
  consistent with the existing seven-check pipeline ordering.
  Judge confirms via Read of the helper + the call-site.

- `ap2/briefing_validators.py` Prose: the new helper's error
  message verbatim names the offending path token, references
  `TASK_AGENT_FENCED_PATHS` (the audit-trail anchor), and
  suggests an operator-CLI alternative where one exists in the
  suggested-fix map (`.cc-autopilot/cron.yaml` → `ap2 cron edit`,
  `goal.md` → `ap2 update-goal`, etc.). Judge confirms via Read.

- `ap2/tests/` Prose: the new test module (or amended existing
  module) carries (a) one positive test where a briefing with a
  fenced path in `## Scope` is rejected with an error message
  containing the path verbatim; (b) one negative test where a
  briefing with only agent-writable paths in Scope passes; (c)
  one regression-pin where a fenced path in `## Out of scope` is
  NOT scanned and the briefing passes. Judge confirms via Read.

## Out of scope

- Lint for fuzzy / unbackticked fenced-path mentions in `##
  Scope` ("the cron.yaml file" without backticks). Start with the
  high-precision codespan match; a fuzzier follow-up can land if
  false-negatives surface in real operator briefings.

- The `!`-prefix shell-bullet reliability issue (TB-307's
  separate concern). That's a verification-bullet lint, not a
  scope-path check; defer to a follow-up after observing whether
  the failure mode recurs.

- Surfacing fenced-path violations on the `ap2 check` lint
  surface (the non-fatal pre-flight). The queue-append validator
  is the load-bearing gate — `ap2 check` parity is a nice-to-have
  that can land later.

- Auto-rewriting a rejected briefing to move the fenced-path
  Scope item to Out of scope. Reject + operator-revise is the
  right shape; auto-rewrite invites silent scope changes the
  operator didn't intend.

- Extending the check to paths NOT in `TASK_AGENT_FENCED_PATHS`
  but daemon-managed via other means (e.g. `.cc-autopilot/sessions/`
  per-checkpoint). The canonical list is the single audit anchor;
  any future fence-path addition flows through the same constant.

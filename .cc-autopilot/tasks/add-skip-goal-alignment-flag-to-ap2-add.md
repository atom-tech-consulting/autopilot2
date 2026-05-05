# Add `--skip-goal-alignment` flag to `ap2 add` / `ap2 update` — bypass goal-cite + Why-now checks for operator-driven exceptions

## Goal

This task is anchored in goal.md's `## Done when` criterion: "An operator can point ap2 at a fresh project, paste a goal.md (with Mission + `## Done when`), and walk away for a week without intervention." Today's CLI gate runs counter to that promise — every operator-filed task, including legitimately-meta work (dependency bumps, doc fixes, infrastructure maintenance, prompt trims), must manufacture a goal.md citation + 40-char `Why now:` paragraph to clear the validator. The walk-away promise breaks the moment an operator has to fake alignment prose for a one-line typo fix.

Concretely: TB-161 and TB-164 (both shipped) extend `_validate_briefing_structure` with two goal-alignment checks: (1) `## Goal` section must cite a goal.md "Current focus" item title or `## Done when` bullet, and (2) `## Goal` must include a non-empty `Why now:` rationale (≥40 chars). These guards are designed for ideation, where the autonomous agent has no human-in-the-loop sanity check — without them, ideation drifts into meta-polish or "this would be cool" proposals.

But the same validators also fire on operator-filed CLI adds (`ap2 add` / `ap2 update`) because the operator queue's drain path is shared. That's wrong for operator-driven work: the operator IS the human-in-the-loop, has already decided the task is worth doing, and shouldn't have to manufacture rationale that the validators were designed to extract from autonomous proposals.

This task adds `--skip-goal-alignment` to `ap2 add` and `ap2 update`. When set, the queue payload carries a `skip_goal_alignment: true` field; the validator skips the TB-161 + TB-164 checks but ALL OTHER validations (TB-154 canonical Goal/Scope/Design/Verification/Out-of-scope, TB-138 auto-verifiable Verification bullets, TB-134 single-line title/description/tags, TB-135 non-empty briefing required) continue to apply. The bypass is operator-CLI-only — ideation, MM handler, and any future control agents do NOT get this flag (they go through the same queue but never set it).

Why now: TB-161 and TB-164 already landed earlier today; every operator-filed task since then must clear the goal-alignment guards even when the work is legitimately meta. Filing this escape hatch promptly closes the operator-friction window — without it, operators face friction on routine maintenance work that shouldn't require manufactured goal-alignment prose. Concretely linked to goal.md's `## Done when` walk-away criterion: forcing the operator to author goal-alignment text for a typo fix is exactly the kind of "intervention" the walk-away clock is supposed to count against.

## Scope

- `ap2/cli.py` — add `--skip-goal-alignment` (boolean flag, default False) to BOTH the `add` subparser and the `update` subparser. Flag is plumbed into the corresponding operator-queue payload (`add_*` and `update` ops).
- `ap2/tools.py::_validate_briefing_structure` — accept a `skip_goal_alignment: bool = False` kwarg. When True, skip the TB-161 (goal-cite) and TB-164 (Why-now) checks; run every other validation unchanged.
- `ap2/tools.py::do_operator_queue_append` and the drain-side handler — propagate the new field from the queue payload through to `_validate_briefing_structure`. Validate at queue-append time (rejecting structurally-broken briefings before TB-N allocation) AND at drain time (defense-in-depth for queue-edited payloads).
- `ap2/tools.py` — when the flag is set, the audit line in `operator_log.md` reflects it: `applied operator-queued add_backlog → TB-N <title> (goal-alignment check skipped)`. Future ideation cycles reading operator_log.md see the bypass and can decide whether to count the task toward "operator-validated work" vs. "operator-bypassed-validation work" — useful signal for the rejection-reasons loop (TB-152) without requiring a separate event type.
- `ap2/tests/test_cli.py`, `ap2/tests/test_tools.py`, `ap2/tests/test_operator_queue.py` — new tests for the flag plumbing and validator behavior.

## Design

### Why one flag, not two

The two checks (goal-cite, Why-now) are conceptually paired — both target the same "is this proposal goal-aligned and worth doing?" question. Splitting into `--skip-goal-cite` and `--skip-why-now` adds CLI surface area for a hypothetical case (operator skips one but not the other) that isn't a real workflow. One flag, one audit line, one mental model.

### Why operator-CLI-only

Ideation explicitly should NOT have the bypass — that's the whole reason the validators exist (TB-121 review gate + TB-161/164 mechanical guards together = "ideation can't ship work whose goal-alignment isn't articulated"). The MM handler's `operator_queue_append` MCP tool is more debatable — chat-driven adds are operator-driven, but with less ceremony. Default for v1: only the CLI exposes the flag; chat-path adds always get the validators applied. If chat-path operators hit friction, a follow-up TB can extend the MCP tool's args.

### Validator-side implementation

`_validate_briefing_structure(briefing: str, *, skip_goal_alignment: bool = False) -> str | None` — the kwarg is additive, defaults preserve current behavior. When True, the function skips the goal-cite check (TB-161 logic) and the Why-now check (TB-164 logic) but runs every other check. Returns None on pass, an error string on first failure (existing semantics).

Callers:
- Queue-append validation site (`do_operator_queue_append` and `do_board_edit` add-* branch — `ap2/tools.py:432` today): pass `skip_goal_alignment=args.get("skip_goal_alignment", False)` from the payload.
- Drain-side site: same plumbing.
- Ideation / MM handler / migrate-to-ap2: never pass the kwarg, always run all checks.

### Audit trail

The drain handler's existing `applied operator-queued <op> → TB-N` line gets a `(goal-alignment check skipped)` suffix when the flag was set. Concrete shape:

```
- 2026-05-04T22:15:30Z — applied operator-queued add_backlog → TB-N <title> (goal-alignment check skipped)
```

Without the flag, the audit line is unchanged from today. The suffix is grep-able by future ideation cycles ("which operator-filed tasks bypassed alignment?") and by the operator looking back at the log.

### Why also at queue-append time

TB-154 already validates at `do_operator_queue_append`'s boundary (rejects malformed briefings before TB-N is allocated). The flag must propagate to that validation site OR the queue-append fails with a TB-161/164 error before the operator even gets to the drain. Two options:

1. **Validate at queue-append time with the flag** — fail-fast if the briefing is structurally bad even with the bypass; still allocate TB-N if structurally OK with the bypass.
2. **Defer goal-alignment to drain-side only** — queue-append always passes (no goal-alignment check there), drain-side applies the flag.

Option 1 is more consistent with TB-154's queue-append-time philosophy and gives the operator immediate feedback. Implementer should pick (1).

## Verification

- `uv run pytest -q ap2/tests/` — full regression gate passes.
- `grep -nE '"--skip-goal-alignment"' ap2/cli.py` — flag is wired in BOTH the `add` AND `update` subparsers (verify with `grep -c` ≥ 2).
- `grep -nE "skip_goal_alignment" ap2/tools.py` — kwarg is plumbed in `_validate_briefing_structure` and the queue-append + drain paths.
- `python3 -c "import inspect; from ap2.tools import _validate_briefing_structure; assert 'skip_goal_alignment' in inspect.signature(_validate_briefing_structure).parameters"` — validator exposes the kwarg.
- prose: a test in `test_tools.py` exercises `_validate_briefing_structure` with a briefing missing both the goal-cite AND Why-now content, but otherwise canonically-shaped; with `skip_goal_alignment=False` the validator returns a non-None error string; with `skip_goal_alignment=True` the validator returns None.
- prose: a test pins the OTHER validators still fire when `skip_goal_alignment=True` — a briefing without `## Verification`, OR with multi-line title, OR with a `Manual:` Verification bullet, fails even with the flag set. (Pin at least the missing-Verification-section case.)
- prose: a test in `test_cli.py` runs `cmd_add` with `--skip-goal-alignment` and a briefing lacking goal-cite + Why-now (otherwise valid); asserts the operator-queue payload carries `skip_goal_alignment: true`, the queue drains, and TASKS.md contains the new task. Without the flag, `cmd_add` exits non-zero with a structural error.
- prose: a test pins the operator_log.md audit line — when `skip_goal_alignment=True` is applied at drain time, the audit line for that op contains `(goal-alignment check skipped)`; without the flag, the audit line is the standard `applied operator-queued <op> → TB-N` shape (no suffix).
- prose: a test pins the bypass scope — ideation's `do_board_edit({"action": "add_backlog", ...})` call site does NOT accept the kwarg (or accepts it but the queue-append validator ignores it from non-CLI sources). Either implementation is fine; the test asserts ideation-proposed tasks ALWAYS run goal-alignment checks regardless of payload.

## Out of scope

- Adding the flag to MM handler chat verbs. Defer until friction observed; CLI is enough surface for v1.
- Per-check granularity (`--skip-goal-cite` separate from `--skip-why-now`). One flag, paired semantics.
- Web UI surface for the flag. CLI / chat is enough; web stays read-only.
- Changing the default behavior — default stays "all checks apply." The flag is opt-in.
- Auto-detecting "this is meta work" and skipping checks heuristically. Operator-explicit only; no magic.
- Renaming `ap2 add` / `ap2 update` to make the bypass more discoverable. The flag's `--help` text is the discoverability surface.
- Surfacing "tasks with goal-alignment-skipped" in `ap2 status` or web. Future TB if useful; the operator_log.md grep + a chronological event scan are sufficient today.
- Retroactively marking already-on-board tasks as goal-alignment-skipped. The flag is forward-looking only.

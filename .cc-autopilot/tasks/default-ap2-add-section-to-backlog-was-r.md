# Default `ap2 add` section to Backlog (was Ready)

## Goal

`ap2 add` currently defaults the target section to **Ready** when `-s SECTION` is omitted (per the events: every recent operator-queued add I've made shows `op="add_ready"`). This is wrong for two reasons:

1. **Mental-model mismatch.** Backlog is the canonical "queued, not yet started" state; Ready is "prioritized, ready to dispatch next." Operator-filed tasks are typically triage candidates, not next-up. Ideation-proposed tasks already land in Backlog (correctly). Today's default forces operator-filed work to skip triage.

2. **Pending-review surfacing breaks** for tasks added with `--blocked review` from the operator side. TB-151's `ap2 status` "review:" line only counts Backlog + `@blocked:review`; Ready + `@blocked:review` is invisible to the surface and the daemon's own dispatcher (auto-promotion treats `@blocked:review` correctly in Backlog but a Ready task with the token is in a half-state). Concrete recent example (TB-166): an `ap2 add --blocked review` landed in Ready and didn't show up in `ap2 status` review-pending until manually moved with `ap2 backlog TB-N` — extra friction, easy to miss.

This task changes the default to **Backlog**. Explicit `-s Ready` / `-s Frozen` continue to work for callers that want the prior behavior.

## Scope

- `ap2/cli.py` — the `add` subparser's `-s/--section` argument default. Today the argument has no `default=` (per `ap2/cli.py:1133-1135` for the `logs` subparser shape; the `add` subparser is around line 1090-1130 — verify exact line during impl). Set `default="Backlog"` and update the `help=` string accordingly.
- `ap2/cli.py` — `cmd_add`'s section-resolution logic: confirm the `-s` value flows to the queue op shape (`add_backlog` vs `add_ready` vs `add_frozen`) — the existing branching is already there; only the default changes.
- `ap2/tests/test_cli.py` — extend the existing `cmd_add` tests so the default-section path lands the task in Backlog. Pin the explicit `-s Ready` and `-s Frozen` paths still work (regression).
- `skills/ap2/SKILL.md`, `ap2/README.md` — documentation update if either references the default section.

## Design

### Why Backlog, not Ready

Three signals point the same way:

- **Ideation-proposed tasks land in Backlog.** Operator-filed tasks should match (uniform "to be triaged" semantics regardless of who filed them).
- **Backlog → Ready/Active auto-promotion already exists.** The daemon's `_tick` auto-promotes Backlog items when capacity opens (`backlog_auto_promoted` events). Adding to Backlog doesn't *block* a fast-track — when the board is empty, the new task gets promoted on the next tick.
- **`--blocked review` only works in Backlog** (TB-151's surfacing + ideation's review gate both key on Backlog). Default-to-Backlog removes a footgun.

### What changes for callers that don't pass `-s`

- Operator-filed adds → land in Backlog, get auto-promoted by the daemon when capacity opens. Net latency change: at most one tick (~30s) before the task starts running, vs. zero today. Acceptable for the consistency win.
- MM-handler chat-driven adds (`operator_queue_append({"op":"add_*", ...})`) — these explicitly pick a section in the payload; the CLI default doesn't affect them.

### Backwards compatibility

Any operator scripts or aliases that rely on `ap2 add` (no flags) landing tasks in Ready will see a behavior change. Mitigation: the `-s Ready` flag has been available since TB-77-era; scripts wanting prior behavior add the flag explicitly. The change is announced via a one-line note in CLAUDE.md's "Autopilot" section if helpful (out of scope for this task — operators read the next-task-id line, not a changelog).

## Verification

- `uv run pytest -q ap2/tests/` — full regression gate passes.
- `grep -nE 'default="Backlog"' ap2/cli.py` — the new default is wired into the `add` subparser.
- prose: a test in `test_cli.py` exercises `cmd_add` with no `-s` flag (and a stubbed briefing file) and asserts the queued op is `add_backlog` (not `add_ready`); the resulting drain places the task in the Backlog section.
- prose: a test pins explicit `-s Ready` still routes through `add_ready` (regression — old behavior available on demand).
- prose: a test pins the interaction with `--blocked review`: `ap2 add --briefing-file <path> --blocked review` (no `-s`) lands in Backlog with the `@blocked:review` codespan, AND `ap2 status` (or its underlying review-counting helper) reports the new task in the pending-review list — the original UX gap that motivated this task is closed.

## Out of scope

- Renaming `Ready` / `Backlog` / `Frozen` sections. The semantic distinction stays; only the default changes.
- Auto-promotion tuning (when Backlog → Active happens). Existing `backlog_auto_promoted` logic is unchanged.
- Operator-CLI changelog or migration banner. The change is small and additive (explicit `-s Ready` works); no operator-facing announcement infrastructure today and none warranted.
- `ap2 update` default-section semantics. Update doesn't move tasks across sections without an explicit flag; out of scope.
- Touching the MM-handler / ideation queue-append paths. They explicitly name their target section in payload; the CLI default doesn't propagate there.

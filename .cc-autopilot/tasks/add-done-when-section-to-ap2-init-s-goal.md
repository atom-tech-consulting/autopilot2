# Add `## Done when` section to `ap2 init`'s `GOAL_TEMPLATE` (fix template/validator drift)

## Goal

This task is anchored in goal.md's `## Done when` criterion: "An operator can point ap2 at a fresh project, paste a `goal.md` (with Mission + `## Done when`), and walk away for a week without intervention." That criterion explicitly names `## Done when` as a required section of the fresh-project goal.md — but `ap2 init`'s shipped template (`GOAL_TEMPLATE` in `ap2/init.py:154-166`) only includes Mission + Current focus + Non-goals + Constraints. Done-when is missing.

Two concrete consequences of the drift:

1. **No done-signal for ideation on fresh projects.** Per goal-draft.md's own framing: "ideation can tell 'more work' from 'goal achieved' each cycle. Without one, the only done-signal is the operator manually intervening, which defeats the walk-away promise."
2. **TB-161 anchor validator surface is asymmetric.** The validator matches against `## Current focus` AND `## Done when` heading/bullet text. Fresh projects can only anchor against Current focus — until the operator manually adds Done-when, half the anchor surface is unavailable.

This task closes the template/validator drift: `GOAL_TEMPLATE` grows a `## Done when` section with a comment-shaped placeholder describing what belongs there, so `ap2 init` on a fresh project produces a goal.md ready for both validator surfaces.

Why now: this is the exact onboarding gap the goal-draft.md → goal.md promotion exposed in THIS project — the live goal.md has Done-when because we hand-added it, but a new ap2-managed project running `ap2 init` today would inherit a template missing the section. The TB-161/164 validators (which shipped during 2026-05-04) key on Done-when explicitly, so deferring the template fix means every new project starts in a half-broken state.

## Scope

- `ap2/init.py` — `GOAL_TEMPLATE` (line 154-166) gains a `## Done when` section between `## Mission` and `## Current focus`. The placeholder body describes what belongs there: a bulleted list of concrete "the project ships when X" criteria that ideation can read to recognize "stop proposing here." Reference goal-draft.md's own Done-when examples in the placeholder text so operators have a model.
- `ap2/tests/test_init.py` (or wherever `GOAL_TEMPLATE` tests live — locate via `grep -rnE "GOAL_TEMPLATE" ap2/tests/`) — extend / add a test that pins the template's section presence AND order (Mission → Done when → Current focus → Non-goals → Constraints).
- No changes to existing goal.md files. This task is forward-looking only — projects with already-initialized goal.md files are untouched.

## Design

### Suggested template body

```python
GOAL_TEMPLATE = (
    "# Project Goals\n\n"
    "## Mission\n"
    "(one-sentence statement of what this project is FOR)\n\n"
    "## Done when\n"
    "- (concrete criterion the project ships against — e.g. \"the API\n"
    "  handles N requests/sec at p99 latency Xms in production\")\n"
    "- (add more as needed; ideation treats all-met criteria as\n"
    "  \"stop proposing here\")\n\n"
    "## Current focus\n"
    "- (area or theme actively in flight now)\n\n"
    "## Non-goals\n"
    "- (explicit things this project is NOT trying to do, so ideation\n"
    "  doesn't propose them)\n\n"
    "## Constraints\n"
    "- (hard constraints — tech stack, deadlines, dependencies,\n"
    "  blast-radius limits)\n"
)
```

### Why between Mission and Current focus

Done-when is the "what success looks like" anchor; placing it RIGHT AFTER Mission groups the strategic framing (Mission + Done-when) before the tactical state (Current focus + Constraints). Mirrors the structure of THIS project's goal.md, which has the same ordering and reads well operationally.

### Placeholder body intentionally non-empty

The placeholder body has TWO bullets (one example criterion, one note on add-more). Single empty placeholder bullet would be enough to satisfy structural validation, but the explanatory note helps operators understand WHY the section exists — and that ideation treats all-met criteria as a stop signal. Operators editing the template will read the placeholder and learn the intent without consulting docs.

### Backwards compatibility

Forward-looking only: `ap2 init` runs once per project (it's idempotent — `_ensure_file` skips when the file exists). Existing projects with goal.md files keep their current shape; only fresh `ap2 init` runs get the new template.

## Verification

- `uv run pytest -q ap2/tests/` — full regression gate passes.
- `python3 -c "from ap2.init import GOAL_TEMPLATE; assert '## Done when' in GOAL_TEMPLATE; assert GOAL_TEMPLATE.index('## Done when') < GOAL_TEMPLATE.index('## Current focus')"` — Done-when section is present AND positioned before Current focus.
- `python3 -c "from ap2.init import GOAL_TEMPLATE; sections = ['## Mission', '## Done when', '## Current focus', '## Non-goals', '## Constraints']; positions = [GOAL_TEMPLATE.index(s) for s in sections]; assert positions == sorted(positions)"` — all five canonical sections present in canonical order.
- prose: a test in `test_init.py` (or wherever `init_project` is tested) invokes `init_project` against a fresh `tmp_path` and reads the produced `goal.md`; asserts the file contains a `## Done when` heading and the placeholder body mentions "criterion" (or equivalent) so an operator reading the placeholder understands what belongs there.
- prose: an integration-flavored test seeds a fresh project via `init_project`, writes a minimal briefing with a `## Goal` body that quotes the new template's `## Done when` placeholder text verbatim, and asserts `_validate_briefing_structure` accepts the briefing (anchor matched against the just-generated goal.md). Pins the round-trip: the template's anchors satisfy the validator's own anchor-extraction logic out of the box.

## Out of scope

- Backfilling `## Done when` into existing on-disk goal.md files for projects that already ran `ap2 init`. Forward-looking only; operators manage their own goal.md after the initial template lands.
- Adding placeholder content to other sections (Current focus, Non-goals, Constraints). The Done-when placeholder is the gap; other sections' placeholders work today.
- Changing the TB-161 anchor validator's matching surface (e.g. extending it to `## Constraints` bullets). Separate concern; the validator's current scope is correct as-is.
- Authoring a separate `goal.md` guide doc. Filed separately as a sibling TB if useful.
- Migrating ap2/howto.md, ap2/architecture.md, or ap2/README.md prose to reflect the new template. Separate concern; the template itself is the load-bearing change.

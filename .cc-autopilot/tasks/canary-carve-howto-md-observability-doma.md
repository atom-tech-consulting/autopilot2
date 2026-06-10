## Goal

This task advances goal.md's "Current focus: consolidate the operator manual into auto-triggered, cross-runtime skills" by carving the FIRST domain skill out of `ap2/howto.md` as the canary that establishes the conventions every later carve reuses: agentskills.io `name`/`description` frontmatter (with an auto-trigger `description`), a progressive-disclosure body, deployed-path-relative cross-references, and the docs-drift-gate retarget pattern. The observability domain (event schema + prose-judge diagnostics + `ap2 logs`/stats) is self-contained and backed by exactly one drift gate (`test_every_event_type_documented`), making it the minimal demonstrator — the same role the cron component played as axis-1 canary for the component-boundary focus.

Why now: `ap2/howto.md` is discoverable only via a hand-maintained pointer and cannot auto-surface on a task match; without a canary that nails the SKILL.md shape and the carve-plus-gate-retarget pattern, the parallel carves would each re-invent conventions and drift.

## Scope

- Create `skills/ap2-observability/SKILL.md` with YAML frontmatter (`name`, and a `description` written for implicit auto-invocation on observability / event-schema / diagnostics tasks) and a progressive-disclosure body.
- Move howto's `## Event schema (the canonical timeline)`, `### Prose-judge diagnostics`, `## Stats dashboard`, and the `ap2 logs` reference content into the skill body.
- Retarget `ap2/tests/test_docs_drift.py::test_every_event_type_documented` to assert event-type coverage against the new skill instead of `HOWTO_PATH`.
- Rewrite cross-references in the new skill so they resolve at the deployed path (refer to sibling skills by name; do NOT emit repo-relative `ap2/howto.md#...` links).
- Remove the moved sections from `ap2/howto.md` and fix any now-dangling cross-references elsewhere in howto.

## Design

- Frontmatter `description` is a tight third-person trigger sentence, e.g. "Use when inspecting ap2 event types, the events.jsonl timeline, prose-judge verification diagnostics, or `ap2 logs` / stats output."
- Gate retarget: add a module-level constant (e.g. `OBSERVABILITY_SKILL = ... / "skills/ap2-observability/SKILL.md"`) and read it in the event-type gate; leave the other gates on `HOWTO_PATH` for the follow-up carves (TB-398/399) to move.
- This task does NOT delete `ap2/howto.md` and does NOT touch `sync_assets` — it moves exactly one domain and one gate so the canary stays small.

## Verification

- `test -f skills/ap2-observability/SKILL.md` — canary skill exists.
- `grep -qE '^name:' skills/ap2-observability/SKILL.md` — frontmatter `name` present.
- `grep -qE '^description:' skills/ap2-observability/SKILL.md` — auto-trigger `description` present.
- `! grep -q 'ap2/howto.md' skills/ap2-observability/SKILL.md` — no repo-relative howto cross-refs (links resolve at the deployed path).
- `grep -q 'ap2-observability' ap2/tests/test_docs_drift.py` — event-type drift gate retargeted onto the skill.
- `uv run pytest -q ap2/tests/` — full suite green (catches any cross-gate fallout from the section move).

## Out of scope

- Deleting `ap2/howto.md` or dropping its `sync-assets` target (a later retirement task, once every carve has landed).
- Carving the config / board-ops / task-authoring domains (TB-398 / TB-399 / TB-400).
- Cross-runtime deploy, `AGENTS.md`, or discovery-pointer management (TB-401).

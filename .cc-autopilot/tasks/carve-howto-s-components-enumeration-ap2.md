## Goal

This is the final domain carve for the project's **Current focus: consolidate the operator manual into auto-triggered, cross-runtime skills**. `ap2/howto.md` now holds exactly one substantive uncarved operator-domain section — `## Components enumeration (ap2 status)` (howto.md L186-259) — describing the `## Components` block that `ap2 status` renders: text-mode layout, the env-flag polarity conventions, `--json` parity, and an out-of-scope list. Move it into the existing **ap2-observability** skill (the runtime-monitoring / diagnostics domain that already owns `ap2 logs`, the stats dashboard, and prose-judge diagnostics) so an operator asking "which components are wired and what's their on/off state?" gets it auto-triggered, and leave a one-line pointer stub in howto.

Why now: until this last operator-domain section moves, `ap2/howto.md` survives as a canonical operation manual surface — the focus's delete-test ("if howto.md survives as the canonical operation manual, the consolidation didn't happen") is not met, and the downstream file-retirement step cannot run while substantive content remains.

## Scope

- Move the substantive prose of `## Components enumeration (ap2 status)` from `ap2/howto.md` into `skills/ap2-observability/SKILL.md`, under a clearly-titled section (e.g. "The `ap2 status` components block / runtime monitoring"). Preserve the content faithfully: text-mode rendering layout, the three polarity conventions (`env_flag=None` always-on, `*_DISABLED` suppress, opt-in require), the `<env_flag_desc>` rendering rules, and `--json` parity.
- Replace the carved howto section with a one-line pointer stub matching the other carved-section stubs (heading + "— see the ap2-observability skill").
- Boundary guard (avoid duplication): the `AP2_*` env-flag tuning catalogue stays canonical in `skills/ap2-config/SKILL.md` and the `ap2 status` CLI-verb reference stays in `skills/ap2-board-ops/SKILL.md`; the observability skill cross-references those rather than re-listing the full knob set / verb table.
- Update `skills/ap2-observability/SKILL.md`'s frontmatter `description` so the `ap2 status` component-monitoring surface is part of its auto-trigger description.
- Register a docs-location pin in `ap2/tests/test_docs_drift.py`: add a skill-path constant for the observability skill if not already present, and a test asserting the components-enumeration prose lives in `skills/ap2-observability/SKILL.md` and is NOT duplicated in `ap2/howto.md`. This section is prose with no `HOWTO_PATH`-keyed coverage gate to retarget, so a no-duplication location pin is the correct shape, not a gate flip.

## Design

Follow the established carve convention: move prose, leave a pointer stub, and register the docs pin in the SAME commit so `test_docs_drift.py` stays green. The `## Components` rendering BEHAVIOR is pinned by `ap2/tests/test_tb379_effective_config_snapshot.py` and is untouched — this is a docs-only move. Recommended home is ap2-observability to keep the skill count at 7 (goal.md L130-133 caps fragmentation at ~6-9 coherent skills; a standalone ~75-line monitoring skill is too thin); the operator may redirect to a standalone skill via the review gate.

## Verification

- `grep -qF "default_registry().components" skills/ap2-observability/SKILL.md` — the carved registry-walk reference now lives in the observability skill.
- `! grep -qF "default_registry().components" ap2/howto.md` — the substantive components-enumeration prose is gone from howto (absence check: passes iff the string is absent).
- `grep -q "ap2-observability skill" ap2/howto.md` — a pointer stub remains in howto in place of the carved section.
- `uv run --extra dev pytest -q ap2/tests/test_docs_drift.py` — the new docs-location pin plus the existing drift gates pass.
- `uv run --extra dev pytest -q ap2/tests/test_tb379_effective_config_snapshot.py` — the `## Components` status-render behavior is unchanged.
- `skills/ap2-observability/SKILL.md` Prose: the carved section cross-references the ap2-config skill for the `AP2_*` env-flag knob catalogue and the ap2-board-ops skill for the `ap2 status` verb instead of duplicating them; judge confirms via Read.

## Out of scope

- The file-retirement of `ap2/howto.md` (residual orientation/core sections, sync-assets target drop, gate flips, file delete) — that is the separately-tracked retirement task, hard-sequenced after this carve.
- A `/components` web page or per-component diagnostics (already deferred in the section's own out-of-scope list).

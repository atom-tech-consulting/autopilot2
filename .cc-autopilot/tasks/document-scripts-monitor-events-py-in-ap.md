# Document scripts/monitor_events.py in ap2/howto.md

Tags: #autopilot #docs #monitoring #regression-pin

## Goal

Add a short discoverability entry for `scripts/monitor_events.py`
(committed in `f1ddc24`) to `ap2/howto.md` so operators reading the
howto can find the tool without having to grep the repo. Closes the
goal.md `## Done when` bullet "an operator can point ap2 at a fresh
project, paste a `goal.md` (with Mission + `## Done when`), and walk
away for a week without intervention" — the monitor script materially
helps the "stay informed while walking away" half: an operator
wanting to see arc-relevant events as they fire (without manually
grepping `events.jsonl` or sitting in front of `ap2 logs -n`) needs
to know the tool exists.

Why now: `scripts/monitor_events.py` was added today (2026-05-27) as
a self-contained operator helper that tails events.jsonl and emits
one compact line per arc-relevant event from a hard-coded allowlist
(ideation lifecycle / validation+queue / task lifecycle / focus +
attention + watchdog + daemon). Tool was used live during the
session to track all 17 TBs shipping. It needs an entry in the
operator-facing docs so future operators (and future agents reading
howto.md as context) discover it without trial-and-error grepping.

## Scope

(1) `ap2/howto.md`: add a brief entry for `scripts/monitor_events.py`
in the operator-tooling / observability section (the natural home is
near where `ap2 logs` is documented, or in the "watching the loop"
subsection if one exists; agent should locate by greping for `ap2
logs` references and pick the closest sibling). Entry covers:
  - What it does: tails `.cc-autopilot/events.jsonl` and filters to
    an arc-relevant event-type allowlist, emitting one compact line
    per matching event.
  - When to use it: live monitoring of an active arc (task dispatch
    sequences, ideation cycles, focus advances, attention conditions)
    — complements `ap2 logs -n` (the static tail) and `ap2 status`
    (the periodic snapshot).
  - Basic usage examples (mirror the script's module docstring):
    ```
    # From the project root:
    python3 -u scripts/monitor_events.py

    # Explicit project path:
    python3 -u scripts/monitor_events.py /path/to/project

    # Explicit events.jsonl path (e.g. comparing two projects):
    python3 -u scripts/monitor_events.py --events /path/to/events.jsonl
    ```
  - Output shape one-liner: `HH:MM:SS | <event_type> | key=val ... | summary=...`
  - Note: edits to the allowlist (`KEEP` set at the top of the
    script) widen or narrow coverage; tool is intentionally noisy-
    filtered for arc tracking, not exhaustive event logging
    (which `ap2 logs` covers).

(2) Keep the entry surgical — one short subsection or table row, no
more than ~15-25 lines. The script is a small helper, not a major
feature; the howto entry should match that weight.

(3) No README.md / architecture.md updates this TB — keep scope to
the howto.md docs gap the operator named.

## Design

Pure documentation addition. No code changes, no test changes. The
script committed in `f1ddc24` is the source of truth for behavior;
this TB only makes it discoverable from the operator's primary
reference doc.

## Verification

- `grep -q 'scripts/monitor_events.py' ap2/howto.md` — path referenced in docs.
- `grep -q 'monitor_events' ap2/howto.md` — at least one mention.
- `uv run pytest -q` — full suite passes (no code changes; guard against an accidental code-file edit).

## Out of scope

- Updating `ap2/README.md` — separate doc, separate scope. The user
  asked specifically for `ap2/howto.md`.
- Extending the script itself (allowlist additions, new flags,
  alternative output formats) — separate TB if needed.
- Creating a `scripts/README.md` or similar to document the
  `scripts/` directory generally — only one tool there today; a
  directory-level doc is premature.
- Adding tests for the script — the smoke tests run during today's
  session validated the behavior; the script is a thin wrapper
  over `tail -F` + a static filter set.

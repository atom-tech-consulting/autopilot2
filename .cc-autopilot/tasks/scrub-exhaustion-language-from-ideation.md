# Scrub exhaustion language from ideation_state.md after each ideation write

## Goal

Prevent ideation_state.md from carrying forward self-confirming verdict
language — sentences claiming the focus is exhausted, close to
exhausted, or that name conditions of exhaustion ("once X ships this
focus is done") — that primes the next ideation cycle to repeat the
verdict and parks the loop on a stale judgment. Closes the goal.md
`## Done when` failure mode "Ideation reliably proposes goal-aligned
next steps that substantively advance the goal (not just goal-shaped
pro-forma compliance)" by removing the anchor that biases ideation
toward declaring exhaustion prematurely. Each ideation cycle should
reason freely against goal.md + current state, not inherit a prior
LLM's "we're nearly done" framing. Also deletes the latent
`focus_exhausted` skip predicate in `ap2/ideation.py` that reads
cached exhausted statuses (now never written, since the scrub strips
the verdict language that produced them).

Why now: the 2026-05-23 premature `focus_advanced` incident was driven
by a done-when judge, but the same self-reinforcing pattern shows up
in ideation_state.md itself — once an ideation pass writes a paragraph
like "this focus is approaching exhaustion since X is shipped," the
next ideation reads it as authoritative context and pattern-matches
toward confirming exhaustion. The latent ideation.py skip-predicate
bug noted in `investigation-focus-done-when-premature.md` is a worked
example: a stale `exhausted-needs-operator` status in the cache forced
ideation to skip with `reason=focus_exhausted` until an operator ran
`ap2 ideate --force`. The done-when judge removal TB closes one half;
this TB closes the upstream verdict-language half so ideation context
stays fresh across cycles.

## Scope

(1) New module `ap2/ideation_scrub.py` exporting
`scrub_exhaustion_language(text: str, *, sdk) -> str`. Sends the
input markdown to a small SDK call (Haiku, since this is cheap
sentence-level classification) with a prompt instructing it to
remove any sentence asserting exhaustion, near-exhaustion, or
conditions of exhaustion (e.g. "this focus is essentially done",
"once Y ships nothing remains", "all axes covered"), while
preserving structure (headings, axis breadcrumbs, proposed-task
lists, factual observations). Returns the scrubbed text. On any
LLM error, returns the input unchanged (fail-safe — never lose
breadcrumbs).

(2) Wire the scrub into the ideation write path in `ap2/ideation.py`:
after the existing code that writes `ideation_state.md`, immediately
read it back, run `scrub_exhaustion_language`, and overwrite if the
scrubbed text differs. Emit an `ideation_state_scrubbed
removed_chars=<N>` audit event when the scrub changed the file.

(3) Register `ideation_state_scrubbed` in `ap2/events.py` (event
vocabulary + brief comment describing the trigger and payload).

(4) Delete the `focus_exhausted` skip predicate in `ap2/ideation.py`
around L870-883 (the block that calls
`parse_focus_statuses(ideation_state.md).values()` and skips
ideation when all values are `exhausted-needs-operator`). The
empty-cycles advance signal (other TB) is now the authority on
exhaustion; this predicate became dead code once verdict language
stops being written.

(5) Regression-pin module `ap2/tests/test_scrub_exhaustion_language.py`
covers: seeded state with exhaustion sentences → scrubbed output
omits them and preserves the surrounding structure; clean state →
scrubbed output is byte-identical (no-op); LLM-error path →
returns input unchanged; integration test that wires the scrub
into the ideation write path and asserts the event fires when the
scrub modifies the file; assertion that the `focus_exhausted`
skip predicate no longer fires (the skip path in `_maybe_ideate`
no longer reaches `reason=focus_exhausted`).

(6) Env knob `AP2_IDEATION_SCRUB_MODEL` (default
`claude-haiku-4-5-20251001`) so the operator can override the
scrub model (parallel to how `AP2_AGENT_MODEL` is wired). Add
the knob to `ap2/config.py` and the hot-reload allowlist in
`ap2/env_reload.py`.

## Design

Scrub runs as a post-write filter, not inline in ideation's own
prompt — clean separation between "what ideation chose to write"
and "what's allowed to survive into the next cycle's context."
Idempotent by construction: an already-clean file scrubs to itself.
Haiku is the right cost point because the task is mechanical
sentence classification, not deep reasoning; latency is a one-shot
call per ideation cycle folded into the existing ideation pass cost
envelope. Fail-open on errors preserves the breadcrumb-vs-verdict
distinction: structure is more valuable to keep than verdicts are
to remove on any single cycle.

The scrub prompt operates at sentence granularity rather than block
granularity so axis breadcrumbs and proposed-task lists survive
even if they sit in the same paragraph as a verdict sentence. The
ideation_state.md structure (headings, lists, fenced blocks) is
preserved through plain text-in / text-out — the scrub LLM is
instructed not to reformat, only to delete the named sentence
shapes.

Deleting the `focus_exhausted` predicate is folded into this TB
rather than split out because the two changes interlock: the
predicate only worked because the cache accumulated
`exhausted-needs-operator` verdicts; with the scrub removing the
upstream verdict language, the cache no longer carries those
values and the predicate becomes vestigial. Splitting would leave
an intermediate state where the predicate exists but its inputs
are absent (harmless but pointless code).

## Verification

- `test -f ap2/ideation_scrub.py` — new module exists.
- `grep -q 'scrub_exhaustion_language' ap2/ideation_scrub.py` — function exported.
- `grep -q 'scrub_exhaustion_language' ap2/ideation.py` — wired into the ideation write path.
- `grep -q 'ideation_state_scrubbed' ap2/events.py` — event registered.
- `grep -q 'AP2_IDEATION_SCRUB_MODEL' ap2/config.py` — env knob wired.
- `! grep -q 'reason=focus_exhausted\|focus_exhausted' ap2/ideation.py` — skip predicate fully removed.
- `test -f ap2/tests/test_scrub_exhaustion_language.py` — regression-pin module exists.
- `uv run pytest -q ap2/tests/test_scrub_exhaustion_language.py` — module passes.
- `uv run pytest -q` — full suite passes.

## Out of scope

- Backfilling the existing `ideation_state.md` content with a
  one-shot scrub run (the file will self-clean on the next
  ideation cycle; an explicit backfill adds complexity for
  marginal value).
- Restructuring `ideation_state.md`'s schema or per-focus block
  format (this TB only scrubs sentence content; structural
  changes belong to a separate iteration if the schema turns out
  to need it).
- Replacing the done-when judge (separate TB in the same arc;
  this TB is independent and can land in either order).
- Renaming `Done when:` → `Progress signals` in `goal.md`
  format (separate follow-up TB).

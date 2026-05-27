## Goal

Add a `_render_attention_card` sibling to `ap2/web_home.py` that
surfaces currently-active attention conditions as a home-page
card (alongside `_render_focus_card` / `_render_automation_card`
/ `_render_pending_queue` / `_render_operator_decisions` /
`_render_ideation_status_block` / `_render_env_stale_warning`).
The card consumes the SAME `attention.detect_attention_conditions(cfg)`
entrypoint the `/attention` page (TB-296), the status-report cron
(TB-282), the immediate-Mattermost push (TB-297), and the new
`ap2 status` line (TB-298 — sibling proposal) consume, so all
five operator-facing surfaces stay in lockstep. Closes Current
focus: operator-legible reporting and monitoring's third Progress
signal — "Attention-needing conditions ... are surfaced
proactively in operator-legible terms, distinct from routine
progress updates" — on the home-page-entry-point pull surface.

Why now: TB-296 added a `/attention` page reachable from a
chrome nav link, but the web home (`/`) — the first surface an
operator sees when they open the browser — has no attention
summary at all (verified: `grep -i attention ap2/web_home.py` →
no matches). The home page already composes six sibling cards
for the same operator-attention cluster (focus state, auto-
approve state, pending queue, operator decisions, ideation
status, env staleness); attention is the one operator-attention
axis missing. An operator landing on `/` who hasn't yet
internalized the nav has no visual signal that attention is
firing — they only learn by clicking the nav link or by reading
the next Mattermost post. Closes the home-page-as-entry-point
gap symmetric to TB-242's focus card.

## Scope

(1) New `_render_attention_card(cfg) -> str` function in
`ap2/web_home.py` (sibling to `_render_focus_card` and
`_render_automation_card`). Calls
`attention.detect_attention_conditions(cfg)`; renders an
operator-legible card with bullet shape matching
`web_attention._render_attention` (warn-glyph `⚠`, bold TB-N
when `extras['task']` present, em-dash, detector-supplied
`summary`) so the home card and the dedicated page render
identically. Card heading: `## Attention` with a small `(N)`
suffix when conditions fire.

(2) Truncation: cap inline bullets at 3 with a `(+M more — see
/attention)` link-tail when more conditions are active; the
full list lives on the dedicated `/attention` page so home stays
compact. Mirrors TB-298's text-render cap of 3 for shape
symmetry across CLI + home surfaces.

(3) Empty-state discipline: OMIT THE ENTIRE CARD when zero
conditions fire (no heading, no body, no zero-noise) so a quiet
project's home page stays clean. Mirrors `_render_focus_card` /
`_render_automation_card` omit-on-empty discipline (which both
already render `""` for fresh / pre-pivot projects).

(4) Defensive fallback: a detector exception is swallowed and
rendered as a tinted notice — the home page must never 500
because one detector errored. Mirrors `web_attention.py`'s
swallow-on-error contract (the `_render_attention` `try/except
Exception` wrap).

(5) Wire into `_render_home` (`ap2/web_home.py`): call the new
helper alongside the existing cards. Insertion point: directly
AFTER `_render_focus_card` (TB-242 axis-4 surface) and BEFORE
`_render_automation_card` (TB-227 axis-1+3 surface) so the
operator-attention cluster orders by urgency — attention
conditions are the most actionable signal (they name a specific
condition needing eyes), focus and automation are state.

(6) Link-through: bullets with `extras['task']` set link to
`/task/<TB-N>` (existing route); the `(+M more — see /attention)`
tail links to `/attention`. Mirrors the `/events` row link-through
pattern TB-296 added (event rows for `attention_raised` link to
`/attention`).

(7) Regression-pin module `ap2/tests/test_tb299_web_home_attention.py`
covers: (a) home page omits the card entirely when
`detect_attention_conditions` returns []; (b) home page renders
the card with operator-legible bullets when conditions fire;
(c) bullets cap at 3 with `(+M more — see /attention)` link-tail
when >3; (d) per-task bullets link to `/task/<TB-N>`;
(e) detector exception renders a tinted notice rather than
500-ing the home page; (f) card sits between focus and
automation cards in the rendered HTML (assert relative order via
substring positions).

(8) Documentation: add `_render_attention_card` to the docstring
at the top of `ap2/web_home.py` (the existing "Cards owned by
this module" inventory list) so the home-page sibling map stays
current.

## Design

Sibling pattern follows the established `_render_*_card`
shape — pure read-layer consumer of `attention.detect_attention_conditions`
with no caching, no event mutation, no detector logic. The card
goes between focus and automation because attention conditions
are the most actionable signal in the operator-attention cluster
(they name a specific condition needing eyes); focus and
automation are state cards. The 3-bullet inline cap matches
TB-298's CLI cap so an operator switching between `ap2 status`
and the browser home page sees consistent shape. Empty-state
omit-on-empty mirrors the sibling card discipline — quiet
projects stay clean. The detector-exception swallow mirrors
`web_attention._render_attention` so a single broken detector
never takes down the home page.

## Verification

- `test -f ap2/tests/test_tb299_web_home_attention.py` — regression-pin module exists.
- `uv run pytest -q ap2/tests/test_tb299_web_home_attention.py` — module passes.
- `grep -Eq "_render_attention_card" ap2/web_home.py` — new card helper defined in home module.
- `grep -Eq "detect_attention_conditions" ap2/web_home.py` — home consumes the shared detector entrypoint.
- `grep -Eq "_render_attention_card" ap2/web_home.py` — helper wired into composition path (occurs in both the def and the call within `_render_home`).
- `uv run pytest -q ap2/tests/` — full suite passes.

## Out of scope

- New detector kinds (the 5 enumerated condition kinds from
  Progress signal #3 are all shipped and detector-backed; this
  task is purely a new consumer surface).
- `attention_cleared` event class — separate event-vocabulary
  expansion deferred until a concrete consumer surfaces.
- Auto-refresh / polling on the home card (point-in-time;
  operator reloads when desired).
- Cross-project aggregation (per-project legibility scope guard
  at goal.md focus-2 L227-228 still applies).
- Modifying `detect_attention_conditions` itself or any detector
  module — this is a pure new-consumer task.
- Replacing the dedicated `/attention` page — that remains the
  detail-view destination; this card is summary-only with
  link-through.

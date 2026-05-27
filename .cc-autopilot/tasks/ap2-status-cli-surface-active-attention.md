## Goal

Add an `attention:` cluster line to `ap2 status` (text branch) and
an `attention` key to its JSON branch so a walk-away operator
running `ap2 status` from a terminal sees currently-active
attention conditions WITHOUT having to open the web `/attention`
page or wait for the next 2h status-report Mattermost post.
Reuses the SAME `attention.detect_attention_conditions(cfg)`
entrypoint the web page (TB-296), the status-report cron
(TB-282), and the immediate-MM push (TB-297) consume, so all
four operator-facing surfaces stay in lockstep. Closes the
Current focus: operator-legible reporting and monitoring third
Progress signal — "Attention-needing conditions ... are surfaced
proactively in operator-legible terms, distinct from routine
progress updates" — on the CLI-pull surface.

Why now: focus-2's push surfaces (status-report cron, optional
Mattermost push) and the browser pull surface (`/attention`)
shipped (TB-282, TB-287..TB-290, TB-296, TB-297). The remaining
operator entry point — `ap2 status` polled from a terminal —
collects auto-approve state, audit state, env-staleness, focus
state, janitor counts, classifications, and operator decisions
(see `ap2/cli_daemon.py:cmd_status` lines ~200-580), but never
imports the `attention` module or calls
`detect_attention_conditions`. The walk-away operator who polls
CLI before opening a browser or checking chat has no signal that
attention is firing — defeats the per-project legibility
promise on the most lightweight surface.

## Scope

(1) `ap2/cli_daemon.py:cmd_status`: import `attention` and call
`attention.detect_attention_conditions(cfg)` once near the other
read-layer collections (after `_focus_item` / `auto_approve_state`
/ `audit_state` / `env_staleness` / `operator_decisions`,
co-located so all six operator-attention reads sit in one block).
The call is pure and fast (walks the events tail + a small board
read); no caching needed.

(2) Text branch: append an `attention:  N condition(s) — <bullet>;
<bullet>; (+M more)` line in the operator-attention cluster
(after `audit:` / `env stale` / before the `version:` footer).
Each bullet renders as `TB-N <summary>` when the condition's
`extras['task']` is set, or just `<summary>` for singleton
detectors (`validator_judge_noisy`, `auto_approve_paused`,
`cost_cap_approach`). Cap at 3 bullets inline with a
`(+M more — ap2 web /attention)` tail when more conditions are
active; mirror the TB-151 pending-review-line truncation pattern
(`_format_pending_review_line` in `status_report.py`). OMIT THE
LINE ENTIRELY when zero conditions fire so a quiet project does
not grow a zero-noise line (mirrors the TB-258 `audit:` /
TB-260 `env stale` / TB-177 `janitor:` omit-on-empty discipline).

(3) JSON branch: always carry an `attention` key with shape
`{"count": <int>, "conditions": [{"task": <str|null>, "type":
<str>, "key": <str>, "summary": <str>}, ...]}` so JSON consumers
get parser stability even when zero conditions fire (mirrors
TB-227 `auto_approve` / TB-258 `audit` / TB-260 `env_stale`
parser-stability promise). Conditions list is the full unfiltered
output (no truncation in JSON — the truncation is a text-render
concern only).

(4) Shared truncation helper: factor the bullet truncation logic
into `_format_attention_status_line(conditions, cap=3) -> str`
in `ap2/status_report.py` (sibling to `_format_pending_review_line`
in the same module) so the CLI surface and any future
text-render consumer share the same shape. Helper returns the
post-`attention:  ` body without the prefix.

(5) Regression-pin module `ap2/tests/test_tb298_status_attention.py`
covers: (a) text branch omits the `attention:` line when
`detect_attention_conditions` returns []; (b) text branch
renders `attention:  N condition(s) — ...` when one to three
conditions fire; (c) text branch caps at 3 with `(+M more — ap2
web /attention)` suffix when >3; (d) JSON branch always contains
an `attention` key with `{count, conditions}` shape even when
zero; (e) JSON branch's `conditions` list is unfiltered (no
truncation); (f) the shared truncation helper exists in
`status_report.py` and is callable from outside the module.

(6) Documentation: extend the `ap2/cli_daemon.py:cmd_status`
docstring (and/or `ap2/README.md` `ap2 status` section if it
enumerates lines) to name the new `attention:` cluster line
between `audit:` and `version:`. Cross-reference TB-282 /
TB-296 / TB-297 in a comment near the new call site so a
future maintainer sees the four surfaces share one detector
entrypoint.

## Design

Pure CLI consumer of the existing detector layer — no new
detector kinds, no event-vocabulary expansion, no daemon
mutation. The call is read-only; matches the cmd_status
discipline that every line is a function of disk state at the
moment of the call (the call site does not block on async or
fetch network). The truncation lives in the text render path
only (JSON consumers get the full list — they have their own
rendering preferences); the cap is 3 to keep the cluster
compact alongside other one-line cluster entries (pending-review
uses cap 5; attention's bullets tend to be longer prose-summary
text, so a tighter cap reads better). The shared helper goes in
`status_report.py` rather than a new module because that file
already owns the sibling `_format_pending_review_line` helper —
co-locating keeps the operator-attention text-render shape
discoverable in one place.

## Verification

- `test -f ap2/tests/test_tb298_status_attention.py` — regression-pin module exists.
- `uv run pytest -q ap2/tests/test_tb298_status_attention.py` — module passes.
- `grep -Eq "detect_attention_conditions" ap2/cli_daemon.py` — CLI imports + calls the shared detector entrypoint.
- `grep -Eq "_format_attention_status_line" ap2/status_report.py` — shared truncation helper exists in status_report.py.
- `grep -Eq "_format_attention_status_line" ap2/cli_daemon.py` — CLI uses the shared helper for text render.
- `uv run pytest -q ap2/tests/` — full suite passes.

## Out of scope

- New detector kinds (the 5 enumerated condition kinds from
  Progress signal #3 are all shipped and detector-backed; this
  task is purely a new consumer surface).
- `attention_cleared` event class — separate event-vocabulary
  expansion deferred until a concrete consumer surfaces.
- Cross-project aggregation (per-project legibility scope guard
  at goal.md focus-2 L227-228 still applies).
- Modifying `detect_attention_conditions` itself or any detector
  module — this is a pure new-consumer task.
- Auto-refresh / polling — `ap2 status` is a one-shot CLI;
  operators re-invoke on demand.

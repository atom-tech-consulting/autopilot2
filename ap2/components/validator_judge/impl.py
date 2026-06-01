"""Validator-judge dep-coherence check (TB-235 check #7, TB-247 observability).

Hosts the Haiku-4.5-driven LLM judge that identifies hard predecessors
named implicitly in a task briefing's prose (Scope / Design / Why-now /
description) and the dispatcher (`_check_dependency_coherence`) that
turns the judge's verdict into a queue-append-time gate.

Moved out of `ap2/tools.py` by TB-262 — the judge + parse pipeline (knob
defaults, `_DepJudgeOutcome`, `_parse_dep_judge_response`,
`_judge_dep_coherence_default`, `_check_dependency_coherence`) form one
coherent surface: the LLM-call wrapper that briefing-structure validation
fans out to. Keeping the briefing-shape regexes + this LLM-call surface in
one file forced anyone touching either to load both contexts; the split
isolates the SDK / async / parse-error categorization concerns here.

Public symbols (still re-exported from `ap2.tools` for backward compat):
- `_VALIDATOR_JUDGE_*` env-knob defaults + the deprecated-knob ledger.
- `_DepJudgeTimeout` / `_DepJudgeOutcome` — the sentinel exception and
  the parsed-judge-data NamedTuple consumed by the dispatcher.
- `_DEP_JUDGE_PARSE_ERRORS` — the four parse-failure category labels
  surfaced on `validator_judge_fail` events.
- `_parse_dep_judge_response` — pure-ish parse helper (writes a debug
  dump on failure, returns `_DepJudgeOutcome`).
- `_judge_dep_coherence_default` — the SDK-backed judge entry point.
- `_check_dependency_coherence` — the validator-side dispatcher used by
  `briefing_validators._validate_briefing_structure` check #7.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import time
from pathlib import Path
from typing import Any, NamedTuple

# TB-316: the flat module `ap2/validator_judge.py` moved here as the
# subpackage's `__init__.py`. Pre-TB-316 the relative imports
# `from . import events` and `from .json_extract import …` resolved
# against the `ap2` package; post-relocation the same dots would
# resolve against `ap2.components.validator_judge`, breaking the
# cross-module access. Rewrite as absolute imports (`from ap2 import
# events` / `from ap2.json_extract import …`) so the runtime
# references survive the move without behavior drift. The
# `from . import …` shape inside the sibling `manifest.py` is
# intra-package by design (it sources symbols from this very file).
from ap2 import events
from ap2.config import Config
from ap2.json_extract import extract_rightmost_json_object

# TB-331 axis-5: `os` (env-read source) intentionally dropped from
# this module's import list. The four pre-TB-331 direct-env reads of
# the operator-facing knob names (the disabled / timeout-s / max-turns
# / max-tokens cluster) now route through
# `cfg.get_component_value("validator_judge", <key>)` via the four
# helpers below (`_validator_judge_disabled`, `…_timeout_s`,
# `…_max_turns`, `…_max_tokens_legacy`). Re-introducing `import os` here
# would re-enable the env-read shape the TB-331 grep gate forbids; the
# component body's resolved-config reads now flow exclusively through
# `Config.get_component_value`, whose call-time env-first precedence
# (sectioned env > flat env via reverse-`FLAT_TO_SECTIONED` > cfg
# snapshot > default) preserves the pre-TB-331 behavior bit-for-bit.


# TB-235: knob defaults for the LLM-driven dependency-coherence check
# (validator check #7). Module-level so `test_env_knobs.py`-style probes
# can read the defaults without instantiating the validator, and so the
# docs-drift gate's source-walk finds the canonical knob names here.
#
# TB-269: bumped 15.0 → 60.0. The TB-257 investigation artifact
# (`.cc-autopilot/insights/validator-judge-timeout-2026-05-18.md`)
# measured `_judge_dep_coherence_default` at 17.6-46.8s wall-clock
# against the pre-TB-269 15s default + 5s outer-thread grace
# (`worker.join(timeout=timeout_s + 5)` below) — a 20s ceiling that sat
# BELOW the median completion of even the smallest measured briefing
# (4621 B → ~22s avg). 15/15 recent operator queue-appends timed out;
# the axis-1 dep-coherence gate (load-bearing per goal.md L82-85's
# "upstream gates already make this safe in practice" floor) was
# silently fail-open on essentially every call for 7+ days. 60s sits
# 1.5× the artifact's worst-case ~47s (rounded up to the smallest
# round number) — same `_VERIFY_TIMEOUT_AUDIT_FIX_MULT=1.5` ratio the
# TB-252 doctor audit recommends. Operators tighten via the env knob;
# default now sits above the real-world ceiling instead of below the
# median. `validator_judge_timeout_audit` (TB-269) in `ap2/doctor.py`
# surfaces drift if a future workload shift takes the SDK call back
# above this floor.
_VALIDATOR_JUDGE_TIMEOUT_S_DEFAULT = 60.0
# TB-249: the SDK budget primitive is `max_turns` (the Claude Agent SDK
# does NOT accept the pre-TB-249 output-token extra-arg — every other
# ap2 SDK call site uses `max_turns`; see verify.py:571, janitor.py:724,
# daemon.py:208). `max_turns=2` allows ONE assistant message (the JSON
# verdict) + ONE optional tool call (Read/Grep), which the validator's
# inline-payload prompt shouldn't need but kept as a small escape
# hatch. Operator-tunable via `AP2_VALIDATOR_JUDGE_MAX_TURNS`.
_VALIDATOR_JUDGE_MAX_TURNS_DEFAULT = 2
# TB-249 deprecated-alias ceiling. If an operator still has
# `AP2_VALIDATOR_JUDGE_MAX_TOKENS` set from the pre-TB-249 era, we
# accept the value as a `max_turns` override but cap it so a stale
# value like `500` (the old default) doesn't translate into a 500-turn
# runaway. 5 keeps even a wildly-mis-set value bounded.
_VALIDATOR_JUDGE_DEPRECATED_KNOB_CEIL = 5
# TB-235: legacy default kept ONLY for backward-compatibility lookups
# (test_dep_judge_env_knob_defaults pins the historical value, and the
# AP2_VALIDATOR_JUDGE_MAX_TOKENS alias path needs a sentinel to compare
# against). The runtime no longer threads this through the SDK call;
# `max_turns` is the primitive now.
_VALIDATOR_JUDGE_MAX_TOKENS_DEFAULT = 500
# TB-249: process-once flag so the deprecated-knob warning event fires
# exactly once per process, not once per `ap2 add` invocation. Reset
# from the test suite via
# `validator_judge._VALIDATOR_JUDGE_DEPRECATED_KNOB_LOGGED.clear()`
# (also reachable via `tools._VALIDATOR_JUDGE_DEPRECATED_KNOB_LOGGED`
# — TB-262 re-export preserves the historical attribute path).
_VALIDATOR_JUDGE_DEPRECATED_KNOB_LOGGED: set[str] = set()
# Haiku-4.5 is the cost-target floor for the check (≤$0.005 per
# briefing at typical token volumes). Sonnet/Opus escalation would
# blow the budget; if Haiku proves insufficient on real briefings, a
# future TB introduces a model-override env knob (modeled on the
# verify-judge knob pattern) so the model swap doesn't rewrite the
# validator path. Intentionally NOT exposed as an env knob yet —
# defer until empirical evidence shows Haiku falls short.
_VALIDATOR_JUDGE_MODEL = "claude-haiku-4-5"


class _DepJudgeTimeout(Exception):
    """Sentinel raised by a `dep_judge_fn` (or the default SDK call)
    when the judge exceeded `AP2_VALIDATOR_JUDGE_TIMEOUT_S`. The
    validator's check-#7 dispatch (`_check_dependency_coherence`)
    distinguishes this from generic failures so the emitted event
    type is `validator_judge_timeout` vs `validator_judge_fail` —
    the operator sees the right diagnostic shape in
    `ap2 logs` / `/events`.
    """


# TB-247: parse-failure categorization labels surfaced as `parse_error`
# on `validator_judge_fail` events so operators can pattern-detect
# across many failures without opening every dump. Mirrors TB-236's
# `PARSE_ERROR_CATEGORIES` in `ap2/verify.py` — same idea, different
# branch shapes because the dep-judge response is a dict (not a
# pass/fail/rationale envelope), so the failure modes differ:
#   - `empty_text`     — SDK returned no last-assistant-text at all.
#   - `no_braces`      — text exists but has no `{` / `}` to anchor
#                        the JSON-object extraction.
#   - `json_decode`    — `{...}` candidate parsed-failed
#                        (JSONDecodeError).
#   - `non_dict`       — parsed cleanly but the value isn't a dict
#                        (e.g. judge returned `[1, 2, 3]` or a bare
#                        string).
#   - `sdk_exception`  — the SDK call itself raised before any text
#                        came back; categorized at the
#                        `_check_dependency_coherence` emission site
#                        (no raw text to dump for this branch).
_DEP_JUDGE_PARSE_ERRORS: tuple[str, ...] = (
    "empty_text",
    "no_braces",
    "json_decode",
    "non_dict",
    "sdk_exception",
)


class _DepJudgeOutcome(NamedTuple):
    """TB-247: result + diagnostics from the dependency-coherence judge.

    Mirrors `ap2/verify.py::_ParseOutcome` (TB-236, commit `f32374f`).
    Carries the parsed judge data plus optional diagnostic fields so
    `_check_dependency_coherence` can enrich `validator_judge_fail`
    events with the dump-file path and a parse-error category — the
    operator-pre-blessed fix shape (TB-231 rejection text named it,
    TB-236 implemented it for the prose judge, TB-247 transplants it
    onto the validator judge).

    Fields:
      - `data` — parsed `{"hard_predecessors": [...], "reasoning": ...}`
        dict on a clean parse; `None` on every parse-failure branch.
        The `_check_dependency_coherence` caller treats `data is None`
        as fail-open exactly the way the pre-TB-247 code treated
        `judge_fn(...) is None`.
      - `parse_error` — one of `_DEP_JUDGE_PARSE_ERRORS` on every
        parse-failure path, `None` on success.
      - `dump_path` — per-call debug file at
        `<events_file.parent>/debug/<UTC-ts>-validator-judge-response.txt`
        when a parse failure landed AND the diagnostic write succeeded.
        `None` when the parse succeeded (no dump on disk) OR when the
        dump write hit an OSError (best-effort swallow, mirrors
        TB-236's pattern — a full-disk / permission failure must NEVER
        propagate out of the judge call).

    The dispatcher (`_check_dependency_coherence`) also accepts a
    legacy `dict | None` return value from existing test stubs that
    pre-date TB-247 — those are wrapped as
    `_DepJudgeOutcome(data=..., parse_error=None, dump_path=None)`
    so the diagnostic enrichment is purely additive and the existing
    test_dep_validator_judge module stays green without edits.
    """

    data: dict | None
    parse_error: str | None
    dump_path: "Path | None"


def _parse_dep_judge_response(
    text: str,
    *,
    events_file: "Path | None",
) -> _DepJudgeOutcome:
    """TB-247: parse the dep-judge SDK response into a `_DepJudgeOutcome`.

    Extracted from `_judge_dep_coherence_default` so the parse + dump
    logic is testable without an SDK stub — mirrors how TB-236's
    `_parse_judge_response` is a separable, pure-ish function over the
    raw text. The function is impure only by design: on parse failure
    it writes the FULL raw `text` to
    `<events_file.parent>/debug/<UTC-ts>-validator-judge-response.txt`
    so the operator can diagnose WHY the judge returned the shape it
    did without being limited to the catch-all "non-dict judge
    response" error string the pre-TB-247 event carried.

    Successful parses leave NOTHING on disk — the dump is opt-in on
    failure only so steady-state successful judging doesn't bloat the
    debug dir.

    Best-effort write: any OSError on `debug/` mkdir or file write is
    swallowed and `dump_path` stays None on the returned outcome.
    Mirrors TB-236's `try / except` swallow in
    `_judge_prose_bullet` — a diagnostic-write failure must never take
    down the validator path.

    `events_file=None` (e.g. unit tests that don't care about the
    event emission) suppresses the dump entirely; the outcome still
    carries `data` + `parse_error` so the caller's fail-open path
    works the same way.
    """
    parse_error: str | None = None
    data: dict | None = None

    if not text:
        parse_error = "empty_text"
    else:
        # TB-247: two-pass parse. The pre-TB-247 logic jumped straight
        # to `{`..`}` substring extraction, which had a degenerate
        # blind spot — a judge returning a JSON LIST (e.g.
        # `[1, 2, 3]`) had no `{` at all and fell into the
        # `no_braces` branch, hiding the "valid JSON, wrong shape"
        # failure mode under the structural-noise label. Briefing
        # §Scope (b) wants `[1, 2, 3]` categorized as `non_dict`.
        # Try whole-text JSON first; on parse failure, fall back to
        # the substring extraction so preamble/trailing-prose
        # responses (the operator-named TB-228 shape that motivated
        # TB-236) still get extracted cleanly.
        #
        # TB-261: the substring extraction is now centralized in
        # ``ap2.json_extract.extract_rightmost_json_object`` — the
        # rightmost-balanced-object semantics close the preamble-
        # brace-shadowing bug (TB-89 in post-train) that the pre-
        # TB-261 first-``{`` / last-``}`` boundary-finding had at all
        # four call sites in this codebase. The TB-247 enum stays
        # intact; only the boundary-finding moved to the shared util.
        stripped = text.strip()
        try:
            whole = json.loads(stripped)
        except json.JSONDecodeError:
            whole_parsed = False
        else:
            whole_parsed = True
            if isinstance(whole, dict):
                data = whole
            else:
                parse_error = "non_dict"

        if not whole_parsed:
            extracted = extract_rightmost_json_object(text)
            if extracted is None:
                # No parseable JSON OBJECT anywhere. Could be no
                # braces at all OR braces present but every candidate
                # `{` position fails `raw_decode`. Distinguish the
                # two so the TB-247 enum keeps separating structural
                # noise (`no_braces`) from malformed-JSON shape
                # (`json_decode`) — operators pattern-match on this
                # split per the original taxonomy.
                if "{" not in text:
                    parse_error = "no_braces"
                else:
                    parse_error = "json_decode"
            else:
                parsed, _, _ = extracted
                # The util only returns dict-typed values per its
                # contract; this isinstance check is redundant but
                # kept for closure across the enum (matches the pre-
                # TB-261 defensive branch).
                if not isinstance(parsed, dict):
                    parse_error = "non_dict"
                else:
                    data = parsed

    dump_path: "Path | None" = None
    if parse_error is not None and events_file is not None:
        try:
            debug_dir = events_file.parent / "debug"
            debug_dir.mkdir(parents=True, exist_ok=True)
            ts = _dt.datetime.now(_dt.timezone.utc).strftime(
                "%Y%m%dT%H%M%SZ",
            )
            candidate = debug_dir / f"{ts}-validator-judge-response.txt"
            # Write FULL raw text — no truncation. The pre-TB-247
            # event's `notes` field topped out at the SDK error string;
            # the dump captures the literal bytes the SDK returned so a
            # future investigator can see unescaped quotes / markdown
            # fences / preamble that the categorization heuristic
            # collapsed to a single label.
            candidate.write_text(text or "")
            dump_path = candidate
        except OSError:
            # Best-effort: diagnostic-write failure must never block
            # the judge. Drop the path (event won't carry it either).
            dump_path = None

    return _DepJudgeOutcome(
        data=data, parse_error=parse_error, dump_path=dump_path,
    )


# TB-270: canonical briefing headings that bound the dep-coherence
# judge's input. The judge's job — hard-predecessor detection — is
# answered from the briefing's narrative-intent sections (Goal: why;
# Scope: what), NOT from Verification (which checks shape, not
# dependencies), Out-of-scope (negatives don't shift the dep graph),
# or Design (internal-to-the-TB, not a cross-task dep claim). Slicing
# to these two sections is a faithful narrowing of input — what's
# removed is material the judge wouldn't have used to change its
# verdict — and shrinks the SDK call's input token count by ~50-70%
# on typical operator-curated briefings (TB-257 artifact §
# `prompt-too-heavy`).
_BRIEFING_SLICE_HEADINGS: tuple[str, ...] = ("## Goal", "## Scope")


def _slice_briefing_for_dep_judge(briefing_text: str) -> str:
    """TB-270: return the substring covering `## Goal` and `## Scope`
    sections only, terminating each section at the next `## ` heading
    or EOF.

    The slice is what the dep-coherence judge actually consumes: the
    briefing's narrative-intent surface. Design / Verification /
    Out-of-scope are dropped because none of them shift the judge's
    hard-predecessor verdict. See `_BRIEFING_SLICE_HEADINGS` above for
    the design rationale.

    Defensive fallback: if either canonical heading is missing, or if
    the resulting slice is empty after whitespace-stripping, return the
    full `briefing_text` unchanged. Briefings authored through
    `ap2 add` pass the queue-time validator's TB-161 / TB-164 Goal-
    section checks, so steady-state briefings always have a non-empty
    slice; only legacy or hand-edited skip-the-validator briefings hit
    this branch. The fallback is a hard guarantee that the judge is
    never blind — slicing must not turn a parseable briefing into a
    zero-token payload.

    Both sections are concatenated in SOURCE order (Goal before Scope,
    matching the canonical heading shape) so the judge reads the
    briefing's intent in the same order an operator would. Returns a
    string that is a contiguous substring when the two sections are
    adjacent in the source (the common case), or a concatenation of
    two slices when an unusual briefing wedges a non-canonical heading
    between them.
    """
    sections: list[tuple[int, str, str]] = []  # (start_offset, slice, body)
    for heading in _BRIEFING_SLICE_HEADINGS:
        # `\b` after the heading ensures `## Scope` doesn't accidentally
        # match `## ScopeAndExtras` (none exist today, but the canonical
        # heading set is closed — be strict at the boundary).
        pattern = rf"^{re.escape(heading)}\b"
        m = re.search(pattern, briefing_text, flags=re.MULTILINE)
        if m is None:
            # Missing heading → defensive fallback (don't blind the judge).
            return briefing_text
        section_start = m.start()
        # Find the next `## ` heading after THIS section's heading line
        # so the slice terminates at the next section boundary or EOF.
        # `## ` (with trailing space) intentionally matches any `## X`
        # heading including `## Out of scope` (the canonical TB-N
        # heading inventory uses `## ` as the marker).
        rest = briefing_text[m.end():]
        next_heading = re.search(r"^## ", rest, flags=re.MULTILINE)
        section_end = (
            m.end() + next_heading.start()
            if next_heading is not None
            else len(briefing_text)
        )
        # The slice includes the heading line itself (so the judge sees
        # the section label); the BODY-emptiness check below looks at
        # only the bytes AFTER the heading line so a stub briefing
        # (`## Goal\n\n## Scope\n\n## Design...`) trips the fallback
        # branch — without that distinction the heading bytes alone
        # would mask the empty-body case from the strip-whitespace
        # check.
        section_slice = briefing_text[section_start:section_end]
        body = briefing_text[m.end():section_end]
        sections.append((section_start, section_slice, body))

    # Both headings present but each section's body is empty (e.g. a
    # stub briefing with headings and no prose between them). Fall
    # back to the full text — same defensive posture as the missing-
    # heading branch above. The judge gets SOMETHING to work with
    # instead of a payload that's just two heading lines.
    if all(not body.strip() for _, _, body in sections):
        return briefing_text

    # Preserve SOURCE order — Goal-then-Scope on canonical briefings,
    # but the sort is order-agnostic so a hypothetical Scope-then-Goal
    # briefing would also slice cleanly (the test pin asserts the
    # canonical order is preserved; the sort is the implementation
    # mechanism that makes that guarantee robust to unusual shapes).
    sections.sort(key=lambda triple: triple[0])
    return "".join(section_slice for _, section_slice, _ in sections)


def _resolve_validator_judge_adapter(*, sdk=None, cfg: "Config | None" = None):
    """Resolve the `AgentAdapter` backing the `validator_judge` kind (TB-363).

    TB-363 (axis-6 migration): `_judge_dep_coherence_default`'s dispatch routes
    through the `AgentAdapter` seam instead of calling `sdk.query` directly. The
    backend is chosen per agent kind by TB-358's `select_adapter` reading the
    merged `[agent_backends]` config for the `validator_judge` kind
    (`AP2_AGENT_BACKEND_VALIDATOR_JUDGE` env override > `[agent_backends]` table
    > the all-`claude` default). With the default map the resolved adapter is a
    `ClaudeCodeAdapter` and the judge's verdict is identical to the
    pre-migration direct `sdk.query` path; an operator can set
    `validator_judge=codex` to route just this judge to the Codex backend while
    every other kind stays on Claude.

    The resolved adapter wraps the injected `sdk` handle so the judge's
    hermetic unit tests stay deterministic: the TB-269 / TB-270 fake-SDK tests
    install a fake `claude_agent_sdk` module (captured by the
    `import claude_agent_sdk as sdk` in `_judge_dep_coherence_default`) and the
    Claude adapter dispatches through it. Only the Claude backend carries an
    injectable `_sdk`; any other backend ignores it.

    `cfg=None` is the seam `_judge_dep_coherence_default` hits — it carries no
    `Config`, so it falls back to a default `ClaudeCodeAdapter`, matching the
    all-`claude` default `select_adapter` would resolve with a Config in hand.
    Absolute imports (not relative dots) keep the adapter references resolving
    against the `ap2` package post-TB-316 relocation, and stay off this
    module's import path until dispatch time. Mirrors
    `ideation_scrub._resolve_scrub_adapter` (the axis-6 canary).
    """
    from ap2.adapters.claude_code import ClaudeCodeAdapter

    if cfg is not None:
        from ap2.adapters.select import select_adapter

        adapter = select_adapter("validator_judge", cfg)
    else:
        adapter = ClaudeCodeAdapter()
    if sdk is not None and isinstance(adapter, ClaudeCodeAdapter):
        adapter._sdk = sdk
    return adapter


def _judge_dep_coherence_default(
    *,
    briefing_text: str,
    description: str,
    blocked_tokens: list[str],
    timeout_s: float,
    max_turns: int,
    events_file: "Path | None" = None,
) -> _DepJudgeOutcome:
    """Real-SDK implementation of the TB-235 dependency-coherence judge.

    Returns a `_DepJudgeOutcome` carrying the parsed JSON dict (when
    the judge produced one) plus optional diagnostic fields — the
    dump path of the raw response (on parse failure) and the
    parse-error category. Returns `_DepJudgeOutcome(data=None, ...)`
    on any non-timeout failure (network, parse error, non-dict
    response). Raises `_DepJudgeTimeout` when the SDK call exceeds
    `timeout_s` so the caller can distinguish timeout from other
    failures and emit the right event type.

    TB-247: the return type is now `_DepJudgeOutcome` (NamedTuple)
    instead of `dict | None`. The dispatcher
    (`_check_dependency_coherence`) also accepts legacy `dict | None`
    return values from test stubs that pre-date this change — those
    are wrapped at the call site, so the existing
    `test_dep_validator_judge` module stays green without edits. The
    NamedTuple choice (rather than an out-parameter mutable ref) was
    picked here because it composes naturally with `events.append(
    **payload)` at the emission site and parallels TB-236's
    `_ParseOutcome` shape — see `_DepJudgeOutcome`'s docstring.

    Lazy-imports `claude_agent_sdk` so test paths that mock the
    `judge_fn` kwarg don't pull the SDK at validator import time.
    The judge gets a strict-JSON system prompt + a user payload
    naming the briefing, the post-em-dash description prose, and the
    task's current `@blocked:` codespan tokens; the response shape
    is `{"hard_predecessors": ["TB-N", ...], "reasoning": "<str>"}`.

    TB-249: budget control is `max_turns` (the SDK-native primitive).
    The TB-235 shipping version passed an output-token extra-arg that
    the Claude Agent SDK rejects as an unknown option — every judge
    call failed with the SDK's `unknown option` stderr and the
    fail-open posture hid the regression from operators (TB-243
    surfaced the climbing `validator_judge_fail_count_24h`).
    `max_turns` matches verify.py / janitor.py / daemon.py's SDK call
    sites and is what the SDK actually accepts as its budget bound.
    """
    import asyncio

    # TB-366: source the (possibly test-injected) SDK module through the
    # adapter layer (`ap2.adapters.load_claude_sdk`) rather than a bare
    # `import claude_agent_sdk as sdk` here, so `claude_agent_sdk` is imported
    # only inside `ap2/adapters/` (the import-direction gate). The injected
    # fake-SDK seam is preserved: `load_claude_sdk` resolves the import
    # against `sys.modules`, so the TB-269 / TB-270 hermetic tests that
    # `monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake)` are still
    # captured here and wrapped into the default `ClaudeCodeAdapter` below.
    from ap2.adapters import load_claude_sdk

    try:
        sdk = load_claude_sdk()
    except Exception:
        return _DepJudgeOutcome(data=None, parse_error=None, dump_path=None)

    # TB-363 (axis-6 migration): resolve the AgentAdapter for the
    # `validator_judge` kind and dispatch through it (below) instead of
    # calling `sdk.query` directly. `_judge_dep_coherence_default` carries no
    # `cfg`, so this hits the `cfg=None` seam → a default `ClaudeCodeAdapter`
    # (what the all-`claude` map resolves anyway) wrapping the `sdk` handle
    # imported above, so the hermetic fake-SDK unit tests (TB-269 / TB-270)
    # stay deterministic and Claude behavior is bit-for-bit unchanged.
    adapter = _resolve_validator_judge_adapter(sdk=sdk, cfg=None)

    # TB-247: tightened final-message contract. Pre-TB-247 the prompt
    # asked for "strict JSON" but did NOT forbid markdown code fences,
    # did NOT cap the reasoning-field length, did NOT show an explicit
    # example, and did NOT ban preamble / trailing prose. Within
    # <4h of TB-243 shipping the count surface, 2/2 wild calls failed
    # with "non-dict judge response" — the dispatcher's pre-TB-247
    # catch-all that hid the underlying shape from the operator. This
    # tightening mirrors TB-236's prose-judge prompt rewrite verbatim
    # (commit `f32374f`); shorter `reasoning` = smaller surface area
    # for JSON-escape bugs (the TB-236 root cause for the prose judge).
    # Intermediate tool calls stay unconstrained (the judge runs
    # without Read/Glob/Grep today, but if those land later the
    # contract is on the FINAL message only — same as TB-236).
    system_text = (
        "You are validating a task briefing for hard-predecessor "
        "dependency coherence. A hard predecessor is another task "
        "whose work must be on disk (committed) before this task's "
        "agent can do its own work — code modules, schema, env knobs, "
        "or other artifacts the new task depends on. Soft references "
        "(historical context, sibling tasks doing parallel work, "
        "references to docstrings or prior commits for "
        "reading-comprehension only) are NOT hard predecessors.\n\n"
        "OUTPUT CONTRACT — your FINAL message must be a JSON object "
        "only:\n"
        '  {"hard_predecessors": ["TB-217"], '
        '"reasoning": "TB-217 created ap2/_shared.py which this '
        'briefing imports"}\n'
        "Rules for the FINAL message:\n"
        "  - It is a JSON object only. No markdown code fences (no "
        "```json or ``` wrapping). No leading prose (no 'Here is the "
        "verdict:' preamble). No trailing commentary after the closing "
        "brace.\n"
        "  - `hard_predecessors` is a (possibly empty) list of strings,"
        " each of the form 'TB-N'.\n"
        "  - `reasoning` is a single short paragraph, MAXIMUM 200 "
        "characters. Cite the briefing file:section or symbol "
        "triggering the dep claim; do NOT quote long briefing excerpts "
        "or paste prose blocks.\n"
        "  - If the reasoning would naturally exceed 200 characters, "
        "summarize: name the strongest single piece of evidence and "
        "stop.\n"
    )
    # TB-270: slice the briefing to Goal+Scope sections only. The judge
    # only needs the briefing's narrative-intent surface for hard-
    # predecessor detection; Design / Verification / Out-of-scope are
    # bytes the judge wouldn't have used. Shrinks typical input size
    # from ~6KB → ~1-2KB and the SDK call's wall-clock proportionally
    # — the secondary axis-1 lever the TB-257 investigation artifact
    # named (`prompt-too-heavy`), complementary to TB-269's timeout
    # bump on the same focus. Defensive fallback in the helper returns
    # full `briefing_text` on malformed briefings so the judge is
    # never blind.
    user_payload = {
        "briefing_markdown": _slice_briefing_for_dep_judge(briefing_text),
        "task_description": description,
        "blocked_codespan_tokens": list(blocked_tokens),
    }
    prompt = (
        f"{system_text}\n\n"
        f"Input:\n```json\n{json.dumps(user_payload, indent=2)}\n```"
    )

    async def _ask() -> str:
        # TB-249: NO `extra_args=` here. The SDK rejects the historical
        # output-token extra-arg as unknown; that pre-TB-249 call was
        # 100% non-functional (every call failed, fail-open path
        # swallowed it). Budget is enforced via `max_turns` — SDK-
        # native primitive every other ap2 call site uses.
        #
        # TB-363 (axis-6 migration): the judge no longer constructs
        # `sdk.ClaudeAgentOptions` / consumes `sdk.query` directly — it builds
        # a backend-neutral `AgentOptions` / `AgentTools` and dispatches through
        # the `AgentAdapter` resolved for the `validator_judge` kind (`adapter`,
        # above). Under the default all-`claude` `[agent_backends]` map the
        # resolved adapter is a `ClaudeCodeAdapter` wrapping the `sdk` handle, so
        # this stays bit-for-bit on Claude and hermetic under the fake-SDK unit
        # tests. The outer `asyncio.wait_for(..., timeout=timeout_s)` below still
        # owns the timeout (so `AgentOptions.timeout_s` is left unset to avoid
        # double-bounding); a backend fault surfaces as a non-`complete`
        # `AgentResult.status`, re-raised here so the worker maps it onto the
        # existing SDK-exception fail-open branch.
        from ap2.adapters.base import AgentOptions, AgentTools

        options = AgentOptions(
            permission_mode="bypassPermissions",
            max_turns=max_turns,
            model=_VALIDATOR_JUDGE_MODEL,
        )
        result = await adapter.run_to_result(prompt, AgentTools(), options)
        if result.status != "complete":
            raise RuntimeError(
                result.error or f"validator judge adapter {result.status}"
            )
        return (result.text or "").strip()

    # If we're already inside a running event loop (the daemon's tick
    # is async and calls sync MCP-tool handlers; e2e tests likewise
    # invoke `do_board_edit` from `asyncio.run(...)`), `asyncio.run`
    # raises `RuntimeError: cannot be called from a running event
    # loop`. Run the coroutine in a fresh thread with its own loop so
    # the sync caller composes correctly in both contexts. The
    # worker-thread path adds <1ms of overhead on the no-loop branch
    # too, which is invisible against a multi-second judge call.
    import threading

    result: dict[str, "str | Exception | None"] = {"text": None, "exc": None}

    def _worker() -> None:
        try:
            result["text"] = asyncio.run(
                asyncio.wait_for(_ask(), timeout=timeout_s),
            )
        except asyncio.TimeoutError as exc:
            result["exc"] = _DepJudgeTimeout(str(exc))
        except Exception as exc:  # noqa: BLE001
            result["exc"] = exc

    worker = threading.Thread(target=_worker, daemon=True)
    # TB-269: stopwatch around the worker join so the
    # `validator_judge_passed` event below carries an honest wall-clock
    # duration. Starting just before `worker.start()` (rather than at
    # `_judge_dep_coherence_default` entry) intentionally excludes the
    # `claude_agent_sdk` import + prompt assembly — same convention
    # TB-252's `verify_passed` uses (subprocess wall-clock, not Python
    # interpreter startup). The figure feeds
    # `validator_judge_timeout_audit` (TB-269) in `ap2/doctor.py`.
    _t0 = time.monotonic()
    worker.start()
    # The worker has its own `asyncio.wait_for` enforcing the timeout;
    # the outer `.join` waits a small grace window past that so a
    # genuinely-stuck worker still surfaces as a timeout to the caller.
    worker.join(timeout=timeout_s + 5)
    if worker.is_alive():
        # Worker overran the inner timeout — treat as timeout so the
        # validator emits `validator_judge_timeout`. The thread leaks
        # (daemon=True so it dies at interpreter shutdown).
        raise _DepJudgeTimeout(
            f"validator judge worker exceeded {timeout_s + 5:.0f}s"
        )
    if isinstance(result["exc"], _DepJudgeTimeout):
        raise result["exc"]
    if result["exc"] is not None:
        # Non-timeout SDK exception. No raw text to dump (we never got
        # past the worker). Return an outcome with no parse_error /
        # dump_path so the dispatcher emits the SDK-exception branch's
        # `validator_judge_fail` event with parse_error="sdk_exception"
        # (categorized at the emission site since the exception object
        # is what the dispatcher actually catches).
        return _DepJudgeOutcome(
            data=None, parse_error=None, dump_path=None,
        )
    text = result["text"] or ""
    duration_s = time.monotonic() - _t0

    # TB-269: emit `validator_judge_passed` for every successful
    # worker return (i.e. the SDK call completed without timeout / SDK
    # exception). Fires BEFORE the JSON parse so the
    # `validator_judge_timeout_audit` doctor surface (TB-269) sees
    # every real-world wall-clock duration the judge actually paid,
    # not just the subset that parsed cleanly — a parse-failure call
    # still spent the same number of seconds against the SDK and that
    # cost matters for sizing `AP2_VALIDATOR_JUDGE_TIMEOUT_S`. Mirrors
    # TB-252's `verify_passed` payload shape verbatim (substituting
    # the validator-judge knob names). Best-effort write: an OSError
    # on the events.jsonl append must NEVER take down the judge —
    # same `try / except OSError: pass` swallow the
    # `validator_judge_{timeout,fail}` emitters use elsewhere in this
    # file.
    if events_file is not None:
        try:
            events.append(
                events_file,
                "validator_judge_passed",
                duration_s=round(duration_s, 3),
                briefing_bytes=len(briefing_text.encode("utf-8")),
                max_turns=max_turns,
                timeout_s=timeout_s,
            )
        except OSError:
            pass

    # TB-247: delegate parse + dump to the testable helper so the
    # four parse-failure branches (empty / no braces / json_decode /
    # non_dict) all flow through one place. The helper writes the
    # FULL raw text to `<events_file.parent>/debug/<ts>-validator-
    # judge-response.txt` on failure and returns the outcome with
    # both `parse_error` and `dump_path` populated.
    return _parse_dep_judge_response(text, events_file=events_file)


def _validator_judge_disabled(cfg: Config) -> bool:
    """TB-331 axis-5: True iff the validator-judge kill switch is set.

    Resolution shape (mirrors the TB-326 pilot template + TB-327 /
    TB-328 / TB-329 / TB-330 cluster siblings): routes through
    `cfg.get_component_value("validator_judge", "disabled")`, which
    evaluates sectioned env (the
    `f"AP2_COMPONENTS_{component.upper()}_{key.upper()}"` shape built
    inside the helper) > flat env (`AP2_VALIDATOR_JUDGE_DISABLED` via
    the `FLAT_TO_SECTIONED` reverse-lookup) > `cfg.components_config`
    snapshot > default at call time. Call-time env-first precedence
    preserves the pre-TB-331 `os.environ.get(...)` lazy-read pattern —
    `monkeypatch.setenv(...)` plus a subsequent helper call picks up
    the new value without rebuilding cfg.

    Same truthy enumeration as the pre-TB-331 inline check
    (`"1"` / `"true"` / `"yes"`, case-insensitive) so the existing
    kill-switch pin (`test_dep_judge_disabled_skips_check` in
    `test_dep_validator_judge.py` and the TB-254 conftest shield) passes
    without modification. The TOML layer's typed `True` / `False` is
    also honored so an operator who opts into
    `[components.validator_judge] disabled = true` gets the same
    behavior the shell-export operator does.

    Default unset → False (judge enabled), bit-for-bit identical to
    the pre-TB-331 env-only behavior.
    """
    raw = cfg.get_component_value("validator_judge", "disabled")
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return False
    return str(raw).strip().lower() in {"1", "true", "yes"}


def _validator_judge_timeout_s(cfg: Config) -> float:
    """TB-331 axis-5: effective per-call timeout (seconds) for the
    dep-coherence SDK invocation.

    Resolution shape (mirrors the TB-326 pilot template + cluster
    siblings): routes through
    `cfg.get_component_value("validator_judge", "timeout_s")`, which
    evaluates sectioned env > flat env (`AP2_VALIDATOR_JUDGE_TIMEOUT_S`
    via the reverse-`FLAT_TO_SECTIONED` lookup) > `cfg.components_config`
    snapshot > default at call time.

    Permissive parse (pre-TB-331 parity): empty / non-float / whitespace-
    only values fall back to `_VALIDATOR_JUDGE_TIMEOUT_S_DEFAULT` (60.0
    post-TB-269). The TOML layer's typed `float` is honored directly so
    an operator who opts into `[components.validator_judge] timeout_s =
    30.0` gets the same behavior the shell-export operator does.
    """
    raw = cfg.get_component_value("validator_judge", "timeout_s")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return _VALIDATOR_JUDGE_TIMEOUT_S_DEFAULT
    try:
        return float(raw)
    except (TypeError, ValueError):
        return _VALIDATOR_JUDGE_TIMEOUT_S_DEFAULT


def _validator_judge_max_turns(cfg: Config) -> int | None:
    """TB-331 axis-5: effective `max_turns` budget from the canonical
    knob, or `None` when unset/invalid so the caller can fall through
    to the deprecated-alias resolution.

    Resolution shape (mirrors the TB-326 pilot template + cluster
    siblings): routes through
    `cfg.get_component_value("validator_judge", "max_turns")`, which
    evaluates sectioned env > flat env (`AP2_VALIDATOR_JUDGE_MAX_TURNS`
    via the reverse-`FLAT_TO_SECTIONED` lookup) > `cfg.components_config`
    snapshot > default at call time.

    Returns `None` (NOT the default) when:
      - the resolved value is `None` (unset across every layer),
      - the value is an empty/whitespace-only string,
      - `int(raw)` raises (operator typo),
      - the parsed int is `<= 0` (zero / negative budgets are
        semantically the same as "knob not set" for the layered
        preference in `_check_dependency_coherence`).
    The caller distinguishes the unset-canonical-knob case from a
    positive override so the TB-249 deprecated-alias path can fire
    when the canonical knob is missing. A positive value short-
    circuits the alias resolution exactly as the pre-TB-331 check
    `if raw_turns:` did.
    """
    raw = cfg.get_component_value("validator_judge", "max_turns")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _validator_judge_max_tokens_legacy(cfg: Config) -> int:
    """TB-331 axis-5: deprecated-alias resolution for
    `AP2_VALIDATOR_JUDGE_MAX_TOKENS` (the pre-TB-249 knob name).

    Resolution shape: routes through
    `cfg.get_component_value("validator_judge", "max_tokens")` so the
    same TOML / env precedence applies. Returns `0` when the layer is
    empty / non-int / non-positive — same sentinel the pre-TB-331
    inline check used (`legacy_val = 0; if legacy_val > 0: …`). The
    caller treats `0` as "alias not set" and falls through to the
    module-level `_VALIDATOR_JUDGE_MAX_TURNS_DEFAULT`.

    Kept on the component-config schema (TB-322) as a back-compat
    sentinel so an operator with a stale `.cc-autopilot/env` /
    `[components.validator_judge] max_tokens = N` opt-in keeps today's
    behavior bit-for-bit while the deprecation warning fires once per
    process.
    """
    raw = cfg.get_component_value("validator_judge", "max_tokens")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return 0
    try:
        legacy_val = int(raw)
    except (TypeError, ValueError):
        return 0
    return legacy_val if legacy_val > 0 else 0


def _check_dependency_coherence(
    cfg: Config,
    *,
    briefing_text: str,
    description: str,
    blocked_csv: str,
    events_file: "Path | None",
    judge_fn=None,
) -> str | None:
    """TB-235 check #7 implementation. See
    `_validate_briefing_structure`'s docstring for the contract.

    Returns an error-string when the LLM judge identifies any TB-N as a
    hard predecessor that is not present in the task's `@blocked:`
    codespan (`blocked_csv`). Returns `None` when:
      - the briefing is consistent (judge identifies no missing hard
        predecessors),
      - the judge returns an empty `hard_predecessors` list (nothing
        to gate on),
      - the off-switch `AP2_VALIDATOR_JUDGE_DISABLED=1` is set
        (resolved post-TB-331 through `_validator_judge_disabled(cfg)`
        → `cfg.get_component_value("validator_judge", "disabled")`),
      - the judge SDK call fails for any reason (timeout / parse
        error / network). The fail-open path emits a
        `validator_judge_{timeout,fail}` event when `events_file` is
        supplied so a rising skip rate is observable in
        `ap2 logs` / the status-report.

    TB-331 axis-5: `cfg` is now a required positional argument. The
    four pre-TB-331 direct-env reads of the operator-facing knob
    names (`DISABLED` / `TIMEOUT_S` / `MAX_TURNS` / `MAX_TOKENS`) all
    route through `cfg.get_component_value("validator_judge", <key>)`
    via the four helpers above; the manifest's `_briefing_validator`
    adapter threads `ctx.cfg` through, with a synthetic empty Config
    fallback for legacy test paths that don't carry a real one
    (test_dep_validator_judge.py et al). See the manifest's
    TB-331 doc block for the chosen access-shape rationale.

    `judge_fn`: callable matching `_judge_dep_coherence_default`'s
    signature. Test paths inject a stub; the production path uses the
    real SDK. The stub must accept the named kwargs
    `briefing_text`, `description`, `blocked_tokens`, `timeout_s`,
    `max_turns`, return a `dict | None` OR a `_DepJudgeOutcome`
    (TB-247), and may raise `_DepJudgeTimeout` to exercise the
    timeout branch. TB-247 added an optional `events_file` kwarg used
    by the production path for the parse-failure dump dir; stubs that
    don't accept it fall through a `TypeError` retry that calls the
    stub without it (so pre-TB-247 test stubs stay green without
    edits).

    TB-249: budget is `max_turns` (SDK-native), resolved from
    `AP2_VALIDATOR_JUDGE_MAX_TURNS` (default 2). The pre-TB-249
    `AP2_VALIDATOR_JUDGE_MAX_TOKENS` knob is kept as a deprecated
    alias: if set AND `AP2_VALIDATOR_JUDGE_MAX_TURNS` is unset, its
    value is reused as `max_turns` (ceiling-capped at
    `_VALIDATOR_JUDGE_DEPRECATED_KNOB_CEIL=5` so a stale `500` from
    the old default doesn't translate into a 500-turn runaway). A
    `validator_judge_deprecated_knob` event fires once per process on
    the first such resolution so the operator sees the deprecation
    without it spamming the event log on every `ap2 add`.
    """
    if _validator_judge_disabled(cfg):
        return None
    timeout_s = _validator_judge_timeout_s(cfg)
    # TB-249 / TB-331: resolve `max_turns` with a layered preference:
    #   (1) AP2_VALIDATOR_JUDGE_MAX_TURNS — canonical knob, default 2.
    #   (2) AP2_VALIDATOR_JUDGE_MAX_TOKENS — deprecated alias; if set
    #       AND (1) is unset, used as `max_turns` capped at
    #       _VALIDATOR_JUDGE_DEPRECATED_KNOB_CEIL. A
    #       `validator_judge_deprecated_knob` event fires once per
    #       process on the first hit.
    #   (3) module default — _VALIDATOR_JUDGE_MAX_TURNS_DEFAULT.
    #
    # TB-331: env reads happen inside the cfg-routed helpers above
    # (`_validator_judge_max_turns` / `_validator_judge_max_tokens_legacy`)
    # so the component body no longer carries any `os.environ.get(...)`
    # call site. The canonical-knob helper returns `None` (not the
    # default) when unset/invalid so the alias-resolution branch
    # below can fire exactly when the pre-TB-331 `if raw_turns:`
    # branch did. The legacy helper returns `0` (sentinel for "alias
    # not set") so the final `legacy_val > 0` guard preserves the
    # pre-TB-331 deprecation semantics bit-for-bit.
    canonical_turns = _validator_judge_max_turns(cfg)
    max_turns = _VALIDATOR_JUDGE_MAX_TURNS_DEFAULT
    if canonical_turns is not None:
        max_turns = canonical_turns
    else:
        legacy_val = _validator_judge_max_tokens_legacy(cfg)
        if legacy_val > 0:
            capped = min(legacy_val, _VALIDATOR_JUDGE_DEPRECATED_KNOB_CEIL)
            max_turns = capped
            # One-shot per-process deprecation notice. The set is
            # module-level so tests can clear it; key on the knob name
            # in case future deprecations land here.
            if (
                events_file is not None
                and "AP2_VALIDATOR_JUDGE_MAX_TOKENS"
                not in _VALIDATOR_JUDGE_DEPRECATED_KNOB_LOGGED
            ):
                try:
                    events.append(
                        events_file,
                        "validator_judge_deprecated_knob",
                        knob="AP2_VALIDATOR_JUDGE_MAX_TOKENS",
                        replacement="AP2_VALIDATOR_JUDGE_MAX_TURNS",
                        legacy_value=legacy_val,
                        applied_max_turns=capped,
                        ceiling=_VALIDATOR_JUDGE_DEPRECATED_KNOB_CEIL,
                    )
                except OSError:
                    pass
                _VALIDATOR_JUDGE_DEPRECATED_KNOB_LOGGED.add(
                    "AP2_VALIDATOR_JUDGE_MAX_TOKENS"
                )
    blocked_tokens = [
        tok.strip()
        for tok in (blocked_csv or "").split(",")
        if tok.strip()
    ]

    fn = judge_fn or _judge_dep_coherence_default
    try:
        raw_ret = fn(
            briefing_text=briefing_text,
            description=description or "",
            blocked_tokens=blocked_tokens,
            timeout_s=timeout_s,
            max_turns=max_turns,
            events_file=events_file,
        )
    except _DepJudgeTimeout as exc:
        if events_file is not None:
            try:
                events.append(
                    events_file,
                    "validator_judge_timeout",
                    timeout_s=timeout_s,
                    error=str(exc),
                )
            except OSError:
                pass
        return None
    except TypeError:
        # TB-247: pre-TB-247 test stubs don't accept the new
        # `events_file` kwarg. Retry without it so legacy stubs
        # (test_dep_validator_judge module) stay green without edits.
        # Production (`_judge_dep_coherence_default`) accepts it
        # natively so this branch never fires in the real path.
        try:
            raw_ret = fn(
                briefing_text=briefing_text,
                description=description or "",
                blocked_tokens=blocked_tokens,
                timeout_s=timeout_s,
                max_turns=max_turns,
            )
        except _DepJudgeTimeout as exc:
            if events_file is not None:
                try:
                    events.append(
                        events_file,
                        "validator_judge_timeout",
                        timeout_s=timeout_s,
                        error=str(exc),
                    )
                except OSError:
                    pass
            return None
        except Exception as exc:  # noqa: BLE001
            if events_file is not None:
                try:
                    events.append(
                        events_file,
                        "validator_judge_fail",
                        error=f"{type(exc).__name__}: {exc}",
                        # TB-247: categorize SDK exceptions so events
                        # readers can filter `parse_error=="sdk_exception"`
                        # to find non-text failures (no raw response to
                        # dump for this branch).
                        parse_error="sdk_exception",
                    )
                except OSError:
                    pass
            return None
    except Exception as exc:  # noqa: BLE001
        if events_file is not None:
            try:
                events.append(
                    events_file,
                    "validator_judge_fail",
                    error=f"{type(exc).__name__}: {exc}",
                    # TB-247: categorize SDK exceptions so events
                    # readers can filter `parse_error=="sdk_exception"`
                    # to find non-text failures (no raw response to
                    # dump for this branch).
                    parse_error="sdk_exception",
                )
            except OSError:
                pass
        return None

    # TB-247: normalize the judge return value into a `_DepJudgeOutcome`.
    # The production path (`_judge_dep_coherence_default`) already
    # returns the NamedTuple post-TB-247; legacy test stubs that
    # return plain `dict | None` are wrapped here so their behavior is
    # unchanged (no diagnostic enrichment when the stub gave us none).
    if isinstance(raw_ret, _DepJudgeOutcome):
        outcome = raw_ret
    else:
        outcome = _DepJudgeOutcome(
            data=raw_ret if isinstance(raw_ret, dict) else None,
            parse_error=None,
            dump_path=None,
        )

    if not isinstance(outcome.data, dict):
        # Malformed JSON / non-object response. Treat as fail-open
        # (mirrors the SDK-error branch above) so a single judge
        # hiccup can't block every `ap2 add`. Emit `validator_judge_fail`
        # so the operator notices if the rate climbs.
        if events_file is not None:
            try:
                # TB-247: enrich the event with `debug_path` + `parse_error`
                # when the outcome carries them (production path post-
                # TB-247). The catch-all `error="non-dict judge response"`
                # string stays so TB-243's count surface (which keys off
                # the event type, not the error string) keeps working
                # and the legacy-stub path (no parse_error in the
                # outcome) still emits the same shape as pre-TB-247.
                payload: dict[str, Any] = {
                    "error": "non-dict judge response",
                }
                if outcome.parse_error is not None:
                    payload["parse_error"] = outcome.parse_error
                if outcome.dump_path is not None:
                    payload["debug_path"] = str(outcome.dump_path)
                events.append(
                    events_file,
                    "validator_judge_fail",
                    **payload,
                )
            except OSError:
                pass
        return None

    data = outcome.data
    hard_preds = data.get("hard_predecessors")
    reasoning = str(data.get("reasoning") or "").strip()
    if not isinstance(hard_preds, list) or not hard_preds:
        # Empty list (or missing field) → no dependency claim → no
        # @blocked requirement. Common path; the judge passes most
        # well-formed briefings through this branch.
        return None
    declared_lower = {t.lower() for t in blocked_tokens}
    for raw in hard_preds:
        if not isinstance(raw, str):
            continue
        tok = raw.strip()
        if not tok or not tok.upper().startswith("TB-"):
            continue
        if tok.lower() in declared_lower:
            continue
        # First missing dependency wins — same shape as the
        # deterministic checks (return on first offender so the
        # operator's error message is specific rather than a
        # multi-line aggregate).
        return (
            f"briefing structure invalid: judge identified {tok} as a "
            f"hard predecessor (reasoning: \"{reasoning}\"). Either "
            f"add @blocked:{tok} to the task's codespan, or rephrase "
            f"the briefing to not claim {tok} as a hard predecessor "
            "(TB-235)."
        )
    return None

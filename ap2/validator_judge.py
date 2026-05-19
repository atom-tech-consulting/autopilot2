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
import os
from pathlib import Path
from typing import Any, NamedTuple

from . import events
from .json_extract import extract_rightmost_json_object


# TB-235: knob defaults for the LLM-driven dependency-coherence check
# (validator check #7). Module-level so `test_env_knobs.py`-style probes
# can read the defaults without instantiating the validator, and so the
# docs-drift gate's source-walk finds the canonical knob names here.
_VALIDATOR_JUDGE_TIMEOUT_S_DEFAULT = 15.0
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

    try:
        import claude_agent_sdk as sdk
    except Exception:
        return _DepJudgeOutcome(data=None, parse_error=None, dump_path=None)

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
    user_payload = {
        "briefing_markdown": briefing_text,
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
        options = sdk.ClaudeAgentOptions(
            permission_mode="bypassPermissions",
            max_turns=max_turns,
            model=_VALIDATOR_JUDGE_MODEL,
        )
        text = ""
        async for msg in sdk.query(prompt=prompt, options=options):
            content = getattr(msg, "content", None)
            if isinstance(content, list):
                for part in content:
                    t = getattr(part, "text", None)
                    if isinstance(t, str) and t.strip():
                        text = t.strip()
            else:
                t = getattr(msg, "result", None)
                if isinstance(t, str) and t.strip():
                    text = t.strip()
        return text

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

    # TB-247: delegate parse + dump to the testable helper so the
    # four parse-failure branches (empty / no braces / json_decode /
    # non_dict) all flow through one place. The helper writes the
    # FULL raw text to `<events_file.parent>/debug/<ts>-validator-
    # judge-response.txt` on failure and returns the outcome with
    # both `parse_error` and `dump_path` populated.
    return _parse_dep_judge_response(text, events_file=events_file)


def _check_dependency_coherence(
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
      - the off-switch `AP2_VALIDATOR_JUDGE_DISABLED=1` is set,
      - the judge SDK call fails for any reason (timeout / parse
        error / network). The fail-open path emits a
        `validator_judge_{timeout,fail}` event when `events_file` is
        supplied so a rising skip rate is observable in
        `ap2 logs` / the status-report.

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
    if os.environ.get("AP2_VALIDATOR_JUDGE_DISABLED", "").lower() in {
        "1", "true", "yes",
    }:
        return None
    try:
        timeout_s = float(
            os.environ.get("AP2_VALIDATOR_JUDGE_TIMEOUT_S", "")
            or _VALIDATOR_JUDGE_TIMEOUT_S_DEFAULT
        )
    except ValueError:
        timeout_s = _VALIDATOR_JUDGE_TIMEOUT_S_DEFAULT
    # TB-249: resolve `max_turns` with a layered preference:
    #   (1) AP2_VALIDATOR_JUDGE_MAX_TURNS — canonical knob, default 2.
    #   (2) AP2_VALIDATOR_JUDGE_MAX_TOKENS — deprecated alias; if set
    #       AND (1) is unset, used as `max_turns` capped at
    #       _VALIDATOR_JUDGE_DEPRECATED_KNOB_CEIL. A
    #       `validator_judge_deprecated_knob` event fires once per
    #       process on the first hit.
    #   (3) module default — _VALIDATOR_JUDGE_MAX_TURNS_DEFAULT.
    raw_turns = os.environ.get("AP2_VALIDATOR_JUDGE_MAX_TURNS", "")
    raw_tokens_legacy = os.environ.get("AP2_VALIDATOR_JUDGE_MAX_TOKENS", "")
    max_turns = _VALIDATOR_JUDGE_MAX_TURNS_DEFAULT
    if raw_turns:
        try:
            parsed = int(raw_turns)
            if parsed > 0:
                max_turns = parsed
        except ValueError:
            pass
    elif raw_tokens_legacy:
        try:
            legacy_val = int(raw_tokens_legacy)
        except ValueError:
            legacy_val = 0
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

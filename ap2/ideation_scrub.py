"""Scrub exhaustion language from ideation_state.md after each ideation write (TB-284).

Each ideation cycle reads the prior cycle's ``ideation_state.md`` as
authoritative context. Self-confirming verdict sentences — "this focus
is essentially done", "all axes covered", "once Y ships nothing
remains" — pattern-match the next cycle toward repeating the verdict
and park the loop on a stale judgment. This module deletes those
sentences post-write so each cycle reasons freely against goal.md +
current state, not inherited "we're nearly done" framing.

Design:

  * **Post-write filter** on the existing ideation write path. Called
    from ``ap2.ideation._run_ideation`` after ``_run_control_agent``
    returns: clean separation between "what ideation chose to write"
    and "what's allowed to survive into the next cycle's context."
  * **Sentence-granular**, not block-granular. Axis breadcrumbs and
    proposed-task lists survive even if they sit in the same paragraph
    as a verdict sentence.
  * **Fail-safe** by construction. On any LLM error (network /
    timeout / parse failure / model unavailable) the original input is
    returned unchanged. Structure (axis breadcrumbs, proposed-task
    lists, factual observations) is more valuable to keep than verdict
    sentences are to remove on any single cycle.
  * **Idempotent** — an already-clean file scrubs to itself.
  * **Haiku-class model** because the task is mechanical sentence
    classification, not deep reasoning. One SDK call per ideation
    cycle, folded into the existing pass's cost envelope.

Configuration:

  * ``AP2_IDEATION_SCRUB_MODEL`` (default ``claude-haiku-4-5-20251001``)
    — operator override for the scrub model. Listed in
    ``env_reload.HOT_RELOADABLE_KNOBS`` so an operator swapping models
    takes effect on the next tick without a daemon restart. Parallel
    to ``AP2_AGENT_MODEL``.

Related: TB-284 also deletes the latent ``focus_exhausted`` skip
predicate in ``ap2/ideation.py`` — once the scrub strips verdict
sentences, the cache no longer accumulates ``exhausted-needs-operator``
status values for that predicate to read.
"""
from __future__ import annotations

import asyncio
import os
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config


# TB-284: default scrub model. Haiku-4.5 is the cost-target floor —
# sentence-level classification, not deep reasoning. Overrideable via
# ``AP2_IDEATION_SCRUB_MODEL`` (listed in
# ``env_reload.HOT_RELOADABLE_KNOBS``).
DEFAULT_SCRUB_MODEL = "claude-haiku-4-5-20251001"

# Wall-clock cap on the scrub SDK call. Sentence-classification over a
# typical ~10-20KB ideation_state.md completes in a few seconds with
# Haiku; the 60s ceiling matches the validator-judge envelope (TB-269)
# as a defensive bound. Past the timeout the fail-safe path returns
# the input unchanged so a slow SDK never blocks the ideation tick.
_SCRUB_TIMEOUT_S = 60.0

# ``max_turns`` budget: ONE assistant message (the scrubbed markdown)
# is the whole job. The +1 (=2) is a small escape hatch — the prompt
# does not request tool use, but giving the SDK a single bonus turn
# costs nothing and matches the validator-judge default (TB-249).
_SCRUB_MAX_TURNS = 2


# TB-294: typed exception classes raised by ``scrub_exhaustion_language``
# on the three failure modes the caller distinguishes for the
# ``ideation_state_scrub_error`` audit event. Coarser-grained than a
# single ``ScrubError`` with a ``reason`` attribute because the test
# seam is cleaner (each path is patched + asserted in isolation) and the
# operator-facing payload field ``reason`` mirrors the exception type
# 1:1 (``ScrubTimeoutError`` → ``reason=timeout``, etc.). The
# fail-safe semantics are preserved at the caller layer
# (``_maybe_scrub_ideation_state``): on exception the original
# ``ideation_state.md`` content is NOT overwritten, just an audit
# event fires so the operator sees the broken scrub instead of the
# pre-TB-294 silent fail-open.
class ScrubError(Exception):
    """Base class for scrub failure modes (TB-294)."""


class ScrubTimeoutError(ScrubError):
    """SDK call exceeded ``_SCRUB_TIMEOUT_S`` (or the worker-join grace).

    Distinguishes the timeout-specific failure mode from generic SDK
    errors so the ``ideation_state_scrub_error reason=timeout`` audit
    event payload accurately names the latency-class issue (separate
    operator triage from a network / model-availability blip).
    """


class ScrubSDKError(ScrubError):
    """Any non-timeout SDK / network / parse exception during the scrub call.

    Wraps the underlying exception so the audit event carries the
    failure-type name (``error=<ExceptionType>``) without exposing the
    SDK exception hierarchy upstream.
    """


class ScrubEmptyOutputError(ScrubError):
    """SDK returned an empty / whitespace-only response.

    Treated as a failure (not a no-op) because preserving the original
    is correct, but the operator needs the audit event — an empty SDK
    response is a model-side bug or a prompt-shape regression, not the
    intended happy path.
    """


_SCRUB_SYSTEM_PROMPT = (
    "You are a markdown sentence filter for an autopilot's per-cycle "
    "assessment file. Your single job: remove sentences that assert "
    "exhaustion / near-exhaustion of a goal, focus, axis, or criteria "
    "— or that name conditions of exhaustion. Preserve every other "
    "sentence and the surrounding structure verbatim.\n"
    "\n"
    "DELETE sentences that:\n"
    "  - assert the named subject is exhausted, complete, done, fully "
    "covered, or that say a goal / focus / axis is essentially or "
    "substantially met. Example: 'This focus is essentially done.'\n"
    "  - name conditions of exhaustion or near-exhaustion. Examples: "
    "'Once TB-N ships nothing remains.', 'After Y lands this axis is "
    "covered.', 'All axes have shipped at least one task this cycle.'\n"
    "  - claim the operator should advance / rotate / close out the "
    "focus or the roadmap.\n"
    "\n"
    "KEEP:\n"
    "  - factual observations of shipped work ('TB-N landed', 'X is "
    "in place', 'Progress so far: <facts>').\n"
    "  - gap / next-step / status lines, even if they sit in the same "
    "paragraph or list item as a verdict sentence.\n"
    "  - headings, bullet markers, axis breadcrumbs, proposed-task "
    "lists, code fences, table rows.\n"
    "\n"
    "STRUCTURAL RULES:\n"
    "  - Do NOT reformat. Do NOT change heading levels, bullet markers, "
    "code fences, or whitespace structure beyond removing the deleted "
    "sentences. Collapse any resulting double blank line to a single "
    "blank line.\n"
    "  - Do NOT add commentary, preamble, or trailing prose.\n"
    "  - If NO sentences match the delete criteria, return the input "
    "verbatim (byte-identical except for incidental trailing whitespace).\n"
    "\n"
    "OUTPUT CONTRACT — your FINAL message is the scrubbed markdown "
    "only. No code fences (no ```markdown wrapping). No preamble "
    "('Here is the scrubbed text:'). No trailing commentary after the "
    "last line of markdown."
)


def _resolved_model(cfg: "Config | None" = None) -> str:
    """Return the scrub model, honoring ``AP2_IDEATION_SCRUB_MODEL``.

    TB-335 (axis-5 core-cluster migration): resolves through
    ``cfg.get_core_value("ideation_scrub_model")`` — the sectioned-env >
    flat-env > TOML-snapshot > schema-default precedence chain
    ``Config.get_core_value`` defines (TB-334). TB-346 dropped the
    redundant inline ``default=""`` so the resolver's schema-default
    backstop (``CORE_CONFIG_SCHEMA["ideation_scrub_model"]`` →
    ``DEFAULT_IDEATION_SCRUB_MODEL``) is the single source of truth;
    behavior is unchanged since that schema default equals this module's
    ``DEFAULT_SCRUB_MODEL`` and the empty-value fallback below is
    untouched. The helper
    reads env at call time so a hot-reload via
    ``env_reload.maybe_reload_env`` propagates without rebinding any
    cached state (parity with the pre-TB-335 direct env read).

    Empty / whitespace-only overrides fall back to the module default
    (the safer choice — a typo'd empty value shouldn't silently route
    the SDK call to "" and trip an opaque SDK error).

    Default ``cfg=None`` preserves the legacy env-read fallback for
    test paths that ``monkeypatch.setenv("AP2_IDEATION_SCRUB_MODEL",
    ...)`` without threading a Config. Cfg-kwarg-+-TypeError-guard
    shape per TB-327.
    """
    # Late import keeps the boundary between `ideation_scrub` and the
    # heavyweight `config` module narrow — `Config` is referenced
    # only at call time, mirroring `_run_scrub`'s lazy SDK pattern.
    from .config import Config
    if cfg is not None and not isinstance(cfg, Config):
        raise TypeError(
            "_resolved_model(cfg=...) expects a Config instance; "
            f"got {type(cfg).__name__}",
        )
    if cfg is not None:
        raw = cfg.get_core_value("ideation_scrub_model")
        value = str(raw or "").strip()
    else:
        # Legacy fallback (TB-335 back-compat shape — `os.getenv` for
        # cross-package grep-gate hygiene; the canonical NEW-read path
        # is `cfg.get_core_value`).
        value = (os.getenv("AP2_IDEATION_SCRUB_MODEL", "") or "").strip()
    return value or DEFAULT_SCRUB_MODEL


def scrub_exhaustion_language(
    text: str, *, sdk, cfg: "Config | None" = None,
) -> str:
    """Return ``text`` with exhaustion-asserting sentences removed.

    ``sdk`` is a module-like object exposing ``query`` and
    ``ClaudeAgentOptions`` matching ``claude_agent_sdk``'s shape.
    Production callers pass the real SDK module; tests inject a stub.
    The kwarg-only signature mirrors the SDK-threading convention used
    by ``validator_judge._judge_dep_coherence_default`` and
    ``daemon._run_control_agent`` so test paths can stub without
    monkey-patching the import.

    TB-294: raises typed exceptions on failure so the caller
    (``ideation._maybe_scrub_ideation_state``) can distinguish the
    three failure modes and emit the ``ideation_state_scrub_error``
    audit event with an accurate ``reason`` field:

      * ``ScrubTimeoutError`` — SDK call exceeded the inner
        ``asyncio.wait_for`` budget or the outer worker-join grace.
      * ``ScrubSDKError`` — any non-timeout exception during the SDK
        call (network, parse, model unavailable, etc.). Wraps the
        original exception's ``type(e).__name__`` in the message.
      * ``ScrubEmptyOutputError`` — SDK returned an empty / whitespace-
        only response. Distinct from a clean-input no-op (which
        returns the input verbatim).

    The caller catches these and writes nothing back to the file on
    exception (fail-safe semantics preserved at the file layer — the
    pre-TB-294 design that silently swallowed errors here moved one
    layer up so the audit event has somewhere to fire from). The
    breadcrumb-vs-verdict trade-off still favours structure: losing
    the surrounding axis context to a transient API hiccup would be a
    worse outcome than failing to scrub one cycle's verdict sentences.

    Empty / whitespace-only input returns unchanged with NO SDK call
    (saves a roundtrip on the first-ever ideation cycle where
    ``ideation_state.md`` may not exist yet). This is a true happy-path
    no-op — no exception raised.

    Idempotent: an already-clean input is returned byte-identical
    (well-prompted Haiku returns the input verbatim when no sentence
    matches the delete criteria — the model is the source of
    idempotency, not a wrapper check, because the wrapper can't
    cheaply tell "needs scrub" from "already clean" without calling
    the model in the first place).
    """
    if not text or not text.strip():
        return text
    model = _resolved_model(cfg)
    prompt = _build_scrub_prompt(text)
    try:
        scrubbed = _run_scrub(sdk=sdk, prompt=prompt, model=model)
    except TimeoutError as exc:
        # Re-raise as the typed scrub timeout so the caller can
        # discriminate this from a generic SDK error without
        # introspecting the exception hierarchy.
        raise ScrubTimeoutError(str(exc)) from exc
    except ScrubError:
        # Already a typed scrub error (defensive — no current path
        # raises one inside ``_run_scrub``, but if a future refactor
        # does, pass it through unmodified rather than re-wrapping
        # it as a generic SDK error).
        raise
    except Exception as exc:  # noqa: BLE001
        # Any other exception (SDK, network, parse, model
        # unavailable, etc.) is a generic SDK error. The audit event
        # payload's ``error`` field carries the exception type name
        # for operator triage.
        raise ScrubSDKError(f"{type(exc).__name__}: {exc}") from exc
    if not scrubbed or not scrubbed.strip():
        # Model returned nothing usable. Treat as failure: the caller
        # preserves the original (doesn't write the empty string back)
        # AND fires the audit event so the operator notices.
        raise ScrubEmptyOutputError("SDK returned empty / whitespace-only output")
    return scrubbed


def _build_scrub_prompt(text: str) -> str:
    """Assemble the SDK prompt: system contract + the input markdown.

    The separator + ``INPUT:`` label is the simplest unambiguous
    boundary between the prompt's instructions and the markdown to
    scrub. The model returns the scrubbed markdown verbatim — no
    JSON envelope, no markdown fence — per the OUTPUT CONTRACT block
    in the system prompt.
    """
    return f"{_SCRUB_SYSTEM_PROMPT}\n\n---\n\nINPUT:\n\n{text}"


def _run_scrub(*, sdk, prompt: str, model: str) -> str:
    """Synchronously invoke the SDK and return the final assistant text.

    Runs the coroutine in a fresh thread with its own event loop so
    the call composes correctly whether or not the caller already
    sits inside a running loop (the daemon's tick is async; tests can
    invoke this from ``asyncio.run(...)`` too). Mirrors the worker-
    thread pattern in
    ``validator_judge._judge_dep_coherence_default``.

    Raises ``TimeoutError`` when the worker overruns the inner
    ``asyncio.wait_for`` plus a small grace window — the caller
    (``scrub_exhaustion_language``) catches everything and falls
    back to the input unchanged.
    """

    async def _ask() -> str:
        # TB-294: Haiku 4.5 auto-engages extended thinking on the scrub's
        # per-sentence DELETE/KEEP classification prompt, producing a
        # multi-thousand-character internal reasoning trace that pushes
        # total latency past the 60s ``_SCRUB_TIMEOUT_S`` budget (real-
        # content 8KB input measured at 110s end-to-end). Disabling
        # thinking yields identical output (same sentences removed,
        # same structure preserved) at ~24s wall-clock — ~40% headroom
        # under the existing budget. The classification task is
        # mechanical pattern-matching against concrete rules in the
        # system prompt; extended thinking doesn't improve verdict
        # quality, it just spends tokens + latency on unobservable
        # internal monologue. ``{"type": "disabled"}`` is the
        # canonical SDK shape (matches the Anthropic API's
        # ``thinking`` config object).
        options = sdk.ClaudeAgentOptions(
            permission_mode="bypassPermissions",
            max_turns=_SCRUB_MAX_TURNS,
            model=model,
            thinking={"type": "disabled"},
        )
        text = ""
        async for msg in sdk.query(prompt=prompt, options=options):
            # Mirrors the message-walking shape used by
            # ``validator_judge._judge_dep_coherence_default`` — text
            # may arrive as a ``content`` list of parts OR as a
            # top-level ``result`` field. Keep the LAST non-empty
            # text encountered (the SDK's final assistant message).
            content = getattr(msg, "content", None)
            if isinstance(content, list):
                for part in content:
                    t = getattr(part, "text", None)
                    if isinstance(t, str) and t.strip():
                        text = t
            else:
                t = getattr(msg, "result", None)
                if isinstance(t, str) and t.strip():
                    text = t
        return text

    result: dict[str, "str | Exception | None"] = {"text": None, "exc": None}

    def _worker() -> None:
        try:
            result["text"] = asyncio.run(
                asyncio.wait_for(_ask(), timeout=_SCRUB_TIMEOUT_S),
            )
        except Exception as exc:  # noqa: BLE001
            result["exc"] = exc

    worker = threading.Thread(target=_worker, daemon=True)
    worker.start()
    # Small grace window past the inner timeout so a genuinely-stuck
    # worker still surfaces as a TimeoutError (the inner wait_for
    # exception is the steady-state timeout signal; the outer join
    # is the defense-in-depth backstop).
    worker.join(timeout=_SCRUB_TIMEOUT_S + 5)
    if worker.is_alive():
        raise TimeoutError(
            f"scrub worker exceeded {_SCRUB_TIMEOUT_S + 5:.0f}s"
        )
    if result["exc"] is not None:
        raise result["exc"]
    return result["text"] or ""

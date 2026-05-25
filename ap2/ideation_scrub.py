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


def _resolved_model() -> str:
    """Return the scrub model, honoring ``AP2_IDEATION_SCRUB_MODEL``.

    Read fresh from ``os.environ`` at call-time so a hot-reload via
    ``env_reload.maybe_reload_env`` propagates without rebinding any
    cached state. Empty / whitespace-only overrides fall back to the
    module default (the safer choice — a typo'd empty value shouldn't
    silently route the SDK call to "" and trip an opaque SDK error).
    """
    return (
        os.environ.get("AP2_IDEATION_SCRUB_MODEL", "").strip()
        or DEFAULT_SCRUB_MODEL
    )


def scrub_exhaustion_language(text: str, *, sdk) -> str:
    """Return ``text`` with exhaustion-asserting sentences removed.

    ``sdk`` is a module-like object exposing ``query`` and
    ``ClaudeAgentOptions`` matching ``claude_agent_sdk``'s shape.
    Production callers pass the real SDK module; tests inject a stub.
    The kwarg-only signature mirrors the SDK-threading convention used
    by ``validator_judge._judge_dep_coherence_default`` and
    ``daemon._run_control_agent`` so test paths can stub without
    monkey-patching the import.

    Fail-safe by construction: on ANY exception during the SDK call —
    timeout, network error, SDK exception, parse error, model
    unavailable — the original ``text`` is returned unchanged. The
    breadcrumb-vs-verdict trade-off favours structure: losing the
    surrounding axis context to a transient API hiccup would be a
    worse outcome than failing to scrub one cycle's verdict sentences.

    Empty / whitespace-only input returns unchanged with NO SDK call
    (saves a roundtrip on the first-ever ideation cycle where
    ``ideation_state.md`` may not exist yet).

    Idempotent: an already-clean input is returned byte-identical
    (well-prompted Haiku returns the input verbatim when no sentence
    matches the delete criteria — the model is the source of
    idempotency, not a wrapper check, because the wrapper can't
    cheaply tell "needs scrub" from "already clean" without calling
    the model in the first place).
    """
    if not text or not text.strip():
        return text
    model = _resolved_model()
    prompt = _build_scrub_prompt(text)
    try:
        scrubbed = _run_scrub(sdk=sdk, prompt=prompt, model=model)
    except Exception:  # noqa: BLE001
        # Fail-safe: any error returns input unchanged. Never lose
        # breadcrumbs to a transient SDK hiccup.
        return text
    if not scrubbed or not scrubbed.strip():
        # Model returned nothing usable. Treat as failure: preserve
        # the original rather than zeroing the file.
        return text
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
        options = sdk.ClaudeAgentOptions(
            permission_mode="bypassPermissions",
            max_turns=_SCRUB_MAX_TURNS,
            model=model,
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

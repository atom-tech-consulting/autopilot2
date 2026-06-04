"""Session-scoped codex-coverage guard logic for the real-SDK smokes (TB-375).

The smoke suite converts a transient / credential / transport hiccup into
`pytest.skip` (`_transient.call_with_transient_retry`) and the codex variants
`importorskip("openai_codex")` (`_adapter.gate_backend`). That is the RIGHT
behavior when codex is genuinely absent — a Claude-only box should skip the
codex variants quietly. But it is exactly the failure mode that hid the
phantom-SDK bug for weeks: a backend that targeted a nonexistent API always
`importorskip`ped, so a regression read as "green-by-skipping". Coverage
erosion was indistinguishable from passing.

This module holds the pure decision logic the `conftest.py` session hook wires
into pytest: *when codex was EXPECTED to run but a codex-parametrized variant
skipped anyway, fail the whole run loudly; when codex is legitimately absent,
stay quiet.* It is a separate, import-only module (no pytest hooks here) so the
verdict can be unit-tested hermetically — faking credential/handle/skip
presence — without spawning a live smoke run.

"Codex expected to run" is the conjunction of three presence signals:

  1. `AP2_REAL_SDK` is set (the suite opted into live calls at all), AND
  2. `openai_codex` is importable (the codex backend handle is installed), AND
  3. a codex credential is present — reusing the daemon-start auth gate's
     `ap2.cli_daemon._codex_credentials_present` helper, the ONE source of
     truth for "a codex credential is present" (`OPENAI_API_KEY`, or a
     `$CODEX_HOME`/`~/.codex/auth.json` ChatGPT-login session). Presence-only:
     no token contents are read or logged.

A "codex variant" is any test whose nodeid mentions codex — both the
`[codex]`-parametrized tool-round-trip smokes and the standalone
`test_codex_real_sdk.py` dispatch smoke. The `[claude]` variants never match.
"""
from __future__ import annotations

import importlib.util
import os
from typing import Iterable, Optional

# One source of truth for the stdout marker the cron's `run_smoke_check`
# greps for; importing it here (rather than re-spelling the literal) keeps the
# guard ↔ cron contract from drifting.
from ap2.smoke_runner import CODEX_SKIP_GUARD_MARKER


def is_codex_variant(nodeid: str) -> bool:
    """True iff `nodeid` names a codex smoke variant.

    Matches both the `[codex]`-parametrized tool-round-trip smokes and the
    standalone `test_codex_real_sdk.py` dispatch smoke (its path carries
    `codex`). The `[claude]` parametrization never matches.
    """
    return "codex" in nodeid.lower()


def codex_variants_skipped(skipped_nodeids: Iterable[str]) -> list[str]:
    """De-duplicated, order-preserving list of skipped codex variant nodeids."""
    out: list[str] = []
    for nid in skipped_nodeids:
        if is_codex_variant(nid) and nid not in out:
            out.append(nid)
    return out


def real_sdk_set(environ: Optional[dict] = None) -> bool:
    """True iff `AP2_REAL_SDK` is set to a non-empty value.

    Mirrors the smokes' own module-level gate (`pytest.mark.skipif(not
    os.environ.get("AP2_REAL_SDK"))`): when it is unset EVERY variant skips, so
    the guard must stay quiet — hence an empty value reads as "not expected".
    """
    env = os.environ if environ is None else environ
    return bool(env.get("AP2_REAL_SDK", "").strip())


def codex_importable() -> bool:
    """True iff the `openai_codex` backend handle is installed (presence-only)."""
    return importlib.util.find_spec("openai_codex") is not None


def credentials_present() -> bool:
    """True iff a codex credential is present, via the daemon-start auth gate's
    helper (the ONE source of truth). Presence-only — never reads token
    contents."""
    from ap2.cli_daemon import _codex_credentials_present

    return _codex_credentials_present()


def codex_expected(*, real_sdk: bool, importable: bool, creds: bool) -> bool:
    """The "codex was supposed to run" condition: all three presence signals."""
    return bool(real_sdk and importable and creds)


def evaluate_guard(
    *,
    skipped_nodeids: Iterable[str],
    real_sdk: bool,
    importable: bool,
    creds: bool,
) -> Optional[str]:
    """Return the marker detail line if the guard should FAIL the run, else None.

    The guard fires ONLY when codex was expected to run (all three presence
    signals) AND at least one codex variant skipped. When codex is legitimately
    absent (any presence signal missing) it returns None — a Claude-only box, or
    a box without codex creds, still passes. The returned string begins with
    `CODEX_SKIP_GUARD_MARKER` (so the cron can recognize it) and names the
    skipped variants.
    """
    if not codex_expected(real_sdk=real_sdk, importable=importable, creds=creds):
        return None
    skipped = codex_variants_skipped(skipped_nodeids)
    if not skipped:
        return None
    return (
        f"{CODEX_SKIP_GUARD_MARKER}: codex was expected to run (AP2_REAL_SDK "
        f"set, openai_codex importable, codex credential present) but these "
        f"codex smoke variant(s) reported skipped: {', '.join(skipped)}"
    )


def evaluate_guard_from_env(skipped_nodeids: Iterable[str]) -> Optional[str]:
    """`evaluate_guard` wired to the live environment — the form the conftest
    session hook calls. Reads the three presence signals from the process env /
    importable handle / auth-gate helper."""
    return evaluate_guard(
        skipped_nodeids=skipped_nodeids,
        real_sdk=real_sdk_set(),
        importable=codex_importable(),
        creds=credentials_present(),
    )

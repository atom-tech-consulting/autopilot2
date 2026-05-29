"""TB-338: enforce the 12-factor ``_KNOBS_STAYING_ENV_ONLY`` cut-line via CI.

Closes goal.md L401-403 progress signal 6:

    "The set of true 12-factor env-only knobs (secrets, deployment
    identity) is documented in a single comment block in
    ``ap2/config_compat.py`` and is clearly minimal."

The comment block at ``ap2/config_compat.py`` L170-223 documents the
cut-line for an auditor. This module is the enforcement mechanism:
without it, a future PR can add a new ``os.environ.get("AP2_*")``
read outside the documented exempt set + outside the bootstrap path
and silently degrade the cleanness signal. With it, the cut-line is
a CI contract — every direct AP2 env read is structurally accounted
for at PR time, in the same docs-drift shape TB-305
(``test_every_env_knob_documented``) and TB-325
(``test_every_config_key_documented``) use for adjacent surfaces.

Two assertions:

  (1) **Disjointness**: ``FLAT_TO_SECTIONED.keys()`` and
      ``_KNOBS_STAYING_ENV_ONLY`` are disjoint. A knob in BOTH
      would be both "migrated to TOML" and "documented permanent
      env-only" — a contradiction. (Mirrors
      ``test_tb323_config_compat.py::
      test_flat_and_env_only_sets_are_disjoint``; pinned here as
      well so a future reorganization of either set has TWO loud
      regression sites instead of one — the cut-line gate stays
      self-contained even if the TB-323 module gets refactored.)

  (2) **Source-level cut-line**: every ``os.environ.get("AP2_<KNOB>")``
      AST call node under ``ap2/`` (excluding ``ap2/tests/`` and
      ``__pycache__``) reads a knob that is EITHER (a) listed in
      ``_KNOBS_STAYING_ENV_ONLY`` (the documented exempt set), or
      (b) inside the bootstrap file allowlist (``ap2/config.py`` and
      ``ap2/env_reload.py`` — both CONSTRUCT cfg from env, so they
      legitimately must read ``os.environ`` directly), or (c) one of
      the documented ``_PENDING_MIGRATION_KNOBS`` debt entries —
      pre-existing reads that previous axis-5 TBs explicitly deferred
      as out-of-scope (TB-334's core-cluster split left
      ``AP2_VERIFY_JUDGE_EFFORT`` and ``AP2_STATUS_REPORT_EFFORT``
      behind, both wired as ``per-site env > global env > per-site
      default`` chains around ``cfg.get_core_value("agent_effort", …)``).
      The pending-migration set is the docs-drift-debt twin of
      ``test_docs_drift._DOCS_DRIFT_EXEMPT_ENV_KNOBS``: each entry
      carries an inline comment naming the follow-up migration TB so
      the audit trail is visible, and the set stays empty-by-design
      after the named follow-ups land. Anything outside the three
      carve-outs is a violation that names the offending file + knob
      and points at the four remediation paths.

Why an AST walk, not a regex over file text: the briefing's
verification grep (``os\\.environ\\.get\\(.AP2_[A-Z_]+``) would
false-positive on commented-out lines and on docstring mentions
that quote the call shape for historical context — e.g.
``ap2/config_loader.py`` L33 docstring carries the literal
``os.environ.get("AP2_*")`` for context. AST parsing visits only
real ``Call`` nodes; docstrings parse as ``Constant`` strings (or
``Expr`` statements) and never as ``Call`` nodes that match the
``os.environ.get`` attribute chain. Same shape ``ast``-based
gate ``ap2/tests/test_core_import_direction.py`` uses.

The ``os.getenv("AP2_…")`` legacy back-compat fallback the axis-5
migration pattern uses for the ``cfg=None`` branch (TB-326/327/328/
329/330/331/332/333/334/335/336) is intentionally NOT matched here
— that's the standard cross-package back-compat shape and matches
the briefing-level regex by construction. The cleanness signal
applies to the canonical ``os.environ.get(...)`` shape, which is
what unmigrated reads have. (When a cfg-helper migration lands,
the call site flips from ``os.environ.get(...)`` to ``os.getenv(...)``
inside the back-compat fallback, dropping out of this gate's
surface.)
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from ap2.config_compat import (
    FLAT_TO_SECTIONED,
    _KNOBS_STAYING_ENV_ONLY,
)


# Repository root, derived from this file's location:
# ap2/tests/test_tb338_env_only_cut_line.py -> repo/
_REPO_ROOT = Path(__file__).resolve().parents[2]
_AP2_DIR = _REPO_ROOT / "ap2"


# ---------------------------------------------------------------------------
# Bootstrap file allowlist (the structural cut-line carve-out).
#
# These files CONSTRUCT ``cfg`` from ``os.environ`` at daemon start. They
# can't read from cfg (it doesn't exist yet) and must read os.environ
# directly. The allowlist is per-FILE (not per-knob) and stays small by
# design — goal.md L401-403's "clearly minimal" framing applies to the
# exempt knob set; this is a separate carve-out for the bootstrap path.
#
# Adding a third file requires explicit operator review on a case-by-case
# basis (per the briefing's Out-of-scope clause). The rationale: anything
# beyond the construct-cfg path should route through ``cfg.get_core_value``
# / ``cfg.get_component_value`` instead — the axis-5 migration's entire
# point.
# ---------------------------------------------------------------------------
_BOOTSTRAP_FILES: frozenset[str] = frozenset({
    "ap2/config.py",       # Config.from_env(): the construct-from-env path.
    "ap2/env_reload.py",   # maybe_reload_env(): the hot-reload mirror.
})


# ---------------------------------------------------------------------------
# Pending-migration debt allowlist (docs-drift-debt twin).
#
# Each entry is a pre-existing direct ``os.environ.get("AP2_…")`` read that
# a previous axis-5 TB explicitly deferred as out-of-scope. The cut-line
# gate accepts these so the cleanness signal can be pinned NOW without
# blocking on the follow-up migrations; meanwhile, the gate still catches
# any NEW direct env reads added beyond this documented debt.
#
# Same shape as ``test_docs_drift._DOCS_DRIFT_EXEMPT_ENV_KNOBS``: per-knob,
# with an inline comment naming the deferring TB / follow-up TB. The set
# stays empty-by-design once the named follow-ups land; a future PR that
# migrates one of these reads should also remove the entry here, so the
# next New direct read of the same knob still trips the gate.
#
# Adding a new entry to this set requires explicit operator review on a
# case-by-case basis (per goal.md L401-403's "clearly minimal" framing —
# the briefing-author's intent is that this debt set trends toward empty,
# not grow).
# ---------------------------------------------------------------------------
_PENDING_MIGRATION_KNOBS: frozenset[str] = frozenset({
    # TB-334 (axis-5 core cluster) left the per-call-site `*_EFFORT`
    # knobs behind — they wrap a `cfg.get_core_value("agent_effort", …)`
    # default in a `per-site env > global env > per-site default` chain.
    # The wrapping read can't be a naive cfg helper (the fallback value
    # depends on a cfg read), so it stays direct until a helper that
    # accepts a fallback callable lands. Deferring TB: TB-334.
    # Source: ap2/verify.py L588.
    "AP2_VERIFY_JUDGE_EFFORT",
    # Same pattern as above for the status-report cron's effort knob.
    # Deferring TB: TB-334. Source: ap2/status_report.py L2028.
    "AP2_STATUS_REPORT_EFFORT",
})


# Only AP2_-prefixed identifier-shaped strings count. The trailing
# character class matches what env knobs actually look like — uppercase
# letters, digits, underscores. The ``AP2_*`` literal in a docstring
# (which doesn't even parse as a valid identifier) is rejected by this
# regex AND by AST node type.
_AP2_KNOB_RE = re.compile(r"^AP2_[A-Z_][A-Z_0-9]*$")


def _iter_source_files() -> list[Path]:
    """Every ``*.py`` under ``ap2/`` excluding ``ap2/tests/`` and
    ``__pycache__/``. Mirrors the source-walk shape in
    ``test_docs_drift.py::_iter_source_files`` so a future addition in
    one place auto-shows up in the others.
    """
    out: list[Path] = []
    for path in sorted(_AP2_DIR.rglob("*.py")):
        rel = path.relative_to(_AP2_DIR)
        parts = rel.parts
        if parts and parts[0] == "tests":
            continue
        if "__pycache__" in parts:
            continue
        out.append(path)
    return out


def _is_os_environ_get_call(node: ast.AST) -> bool:
    """True iff ``node`` is an AST ``Call`` whose func is the
    ``os.environ.get`` attribute chain. Rejects shadowed-name shapes
    (``environ.get``, ``getenv``, etc.) — the cut-line gate's surface
    is exactly the canonical ``os.environ.get(...)`` form.
    """
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr != "get":
        return False
    inner = func.value
    if not isinstance(inner, ast.Attribute) or inner.attr != "environ":
        return False
    base = inner.value
    if not isinstance(base, ast.Name) or base.id != "os":
        return False
    return True


def _walk_ap2_env_reads() -> list[tuple[str, str, int]]:
    """Yield ``(rel_path, knob_name, lineno)`` for every
    ``os.environ.get("AP2_<KNOB>", …)`` call node under ``ap2/`` (excluding
    tests + ``__pycache__``). First positional arg must be a string
    ``Constant`` matching ``_AP2_KNOB_RE``; calls with a non-literal first
    arg fall outside the gate's surface by construction (the operator
    can't audit a dynamic env name from source-walk alone).
    """
    hits: list[tuple[str, str, int]] = []
    for path in _iter_source_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - defensive
            continue
        rel = path.relative_to(_REPO_ROOT).as_posix()
        for node in ast.walk(tree):
            if not _is_os_environ_get_call(node):
                continue
            if not node.args:
                continue
            first = node.args[0]
            if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
                continue
            if not _AP2_KNOB_RE.match(first.value):
                continue
            hits.append((rel, first.value, node.lineno))
    return hits


# ---------------------------------------------------------------------------
# (1) Disjointness — a knob can't be both migrated AND exempt.
# ---------------------------------------------------------------------------


def test_flat_and_env_only_sets_are_disjoint():
    """``FLAT_TO_SECTIONED.keys()`` and ``_KNOBS_STAYING_ENV_ONLY`` must
    be disjoint. A knob in BOTH means a future PR-author reading
    ``config_compat.py`` can't tell whether the knob is migrating to
    TOML (the FLAT_TO_SECTIONED side) or staying env-only (the
    _KNOBS_STAYING_ENV_ONLY side); the back-compat layer would also
    emit ``env_deprecated`` for an explicitly env-only knob, then
    silently skip the override because the runtime double-check in
    ``_apply_flat_back_compat`` masks the contradiction.

    Mirrored from ``test_tb323_config_compat.py``'s identical-shape
    assertion so the cut-line gate stays self-contained: a future
    refactor of either ``test_tb323_config_compat.py`` or of either
    set lights up BOTH regression-pin modules instead of just one.
    """
    flat_keys = set(FLAT_TO_SECTIONED.keys())
    overlap = sorted(flat_keys & _KNOBS_STAYING_ENV_ONLY)
    assert not overlap, (
        f"knob `{overlap[0]}` is in BOTH FLAT_TO_SECTIONED and "
        f"_KNOBS_STAYING_ENV_ONLY — pick one.\n\n"
        f"FLAT_TO_SECTIONED maps a flat env knob to its sectioned "
        f"TOML counterpart (the back-compat path with a "
        f"deprecation event). _KNOBS_STAYING_ENV_ONLY is the "
        f"documented-permanent 12-factor exempt set (secrets, "
        f"identity, deployment paths) — these never migrate to "
        f"TOML by design. A knob in both is contradictory: the "
        f"back-compat layer would fire `env_deprecated` for an "
        f"explicitly env-only knob, then silently skip the "
        f"override. Full overlap set: {overlap}"
    )


# ---------------------------------------------------------------------------
# (2) Source-level cut-line — every AP2 env read is exempt or bootstrap.
# ---------------------------------------------------------------------------


def test_every_ap2_env_read_is_exempt_or_bootstrap():
    """Every ``os.environ.get("AP2_<KNOB>")`` call node in ``ap2/`` source
    (excluding tests and ``__pycache__``) reads a knob that is EITHER:

      (a) listed in ``_KNOBS_STAYING_ENV_ONLY`` (the documented exempt
          set in ``ap2/config_compat.py`` L170-223), or
      (b) inside the bootstrap file allowlist
          (``ap2/config.py`` and ``ap2/env_reload.py``).

      (c) listed in ``_PENDING_MIGRATION_KNOBS`` (the documented debt
          set tracking pre-existing reads previous axis-5 TBs deferred
          as out-of-scope).

    Anything else is a violation and the test fails naming the offending
    file + knob + line number. The failure message is the docs: it
    points the offending PR-author at the four remediation paths
    (migrate via cfg helper, add to exempt set with a one-line
    justification, add file to bootstrap allowlist, add to pending-
    migration debt set with the deferring TB).

    The walker uses ``ast`` (not regex) to avoid false-positives on
    commented-out lines and on docstring mentions that quote the
    call-site shape for historical context — e.g.
    ``ap2/config_loader.py`` L33 docstring carries the literal
    ``os.environ.get("AP2_*")`` for context, which the regex would
    match but the AST walker ignores (it's a string ``Constant``
    inside a module docstring, not a ``Call`` node).
    """
    hits = _walk_ap2_env_reads()
    # Sanity: the walker found something. A zero-hit walk would mean a
    # global axis-5 cleanup hit completion AND every bootstrap read
    # somehow disappeared too — at minimum, ``ap2/config.py``'s
    # ``Config.from_env`` reads multiple AP2 knobs directly, so a
    # zero-hit result indicates the walker is broken.
    assert hits, (
        "TB-338 walker found zero `os.environ.get(\"AP2_…\")` AST nodes "
        "under ap2/ — at minimum, `ap2/config.py`'s `Config.from_env` "
        "and `ap2/env_reload.py`'s `maybe_reload_env` carry several. "
        "The walker likely regressed (check _is_os_environ_get_call "
        "or _iter_source_files)."
    )

    violations: list[str] = []
    for rel, knob, lineno in hits:
        if knob in _KNOBS_STAYING_ENV_ONLY:
            continue
        if rel in _BOOTSTRAP_FILES:
            continue
        if knob in _PENDING_MIGRATION_KNOBS:
            continue
        violations.append(f"{rel}:{lineno}: reads `{knob}` directly")

    assert not violations, (
        "TB-338 cut-line violation: each file below reads an AP2 env "
        "knob directly via `os.environ.get(...)`, but the knob is NOT "
        "in `_KNOBS_STAYING_ENV_ONLY` (the documented 12-factor exempt "
        "set in ap2/config_compat.py), the file is NOT in the bootstrap "
        "allowlist (ap2/config.py, ap2/env_reload.py), and the knob is "
        "NOT in `_PENDING_MIGRATION_KNOBS` (the documented debt set).\n\n"
        + "\n".join(violations)
        + "\n\nFour remediation paths (pick one):\n"
        "  (1) Migrate the read via `cfg.get_core_value(...)` or "
        "`cfg.get_component_value(...)` — the axis-5 migration's "
        "default path (TB-326 .. TB-336 worked examples).\n"
        "  (2) Add the knob to `_KNOBS_STAYING_ENV_ONLY` in "
        "`ap2/config_compat.py` with a one-line justification in the "
        "comment block above the frozenset (Mattermost identity, "
        "integration secret, deployment-environment path, etc.). The "
        "justification line IS the audit trail.\n"
        "  (3) Add the file to `_BOOTSTRAP_FILES` in this test "
        "module — requires explicit operator review on a case-by-case "
        "basis, since each addition expands the structural carve-out "
        "beyond the construct-cfg path (today: `ap2/config.py` and "
        "`ap2/env_reload.py`).\n"
        "  (4) Add the knob to `_PENDING_MIGRATION_KNOBS` with an "
        "inline comment naming the deferring TB and the follow-up "
        "migration TB — for pre-existing reads with a known reason "
        "they weren't migrated yet (e.g. wrapping a cfg-default in a "
        "per-site fallback chain). Discouraged for new reads.\n\n"
        "Source-of-truth for the exempt set: "
        "`ap2/config_compat.py::_KNOBS_STAYING_ENV_ONLY`. "
        "Cut-line rationale comment block: same file, L170-223."
    )


def test_pending_migration_knobs_still_referenced():
    """Each entry in ``_PENDING_MIGRATION_KNOBS`` is actually still being
    read directly by source — a stale entry (the read was migrated but
    the debt allowlist wasn't pruned) would silently degrade the
    cleanness signal by accepting a non-existent read against a stale
    name. Pin the referential integrity here so a future migration PR
    that forgets to prune the debt entry fails this gate loudly.
    """
    hits = _walk_ap2_env_reads()
    read_knobs = {knob for _rel, knob, _lineno in hits}
    stale = sorted(_PENDING_MIGRATION_KNOBS - read_knobs)
    assert not stale, (
        f"TB-338 stale pending-migration entries (no longer read directly "
        f"by source — likely migrated by a follow-up TB without pruning "
        f"the debt allowlist): {stale}. Remove from "
        f"`_PENDING_MIGRATION_KNOBS` so the gate stays tight."
    )


def test_pending_migration_and_env_only_sets_are_disjoint():
    """``_PENDING_MIGRATION_KNOBS`` (the debt set) and
    ``_KNOBS_STAYING_ENV_ONLY`` (the exempt set) must be disjoint. A
    knob in BOTH is contradictory: the debt set tracks reads expected
    to migrate away; the exempt set tracks reads documented permanent.
    A future migration of a debt-set knob would also need to remove
    the exempt-set entry, but the contradiction would mask that.
    """
    overlap = sorted(_PENDING_MIGRATION_KNOBS & _KNOBS_STAYING_ENV_ONLY)
    assert not overlap, (
        f"`_PENDING_MIGRATION_KNOBS` and `_KNOBS_STAYING_ENV_ONLY` must "
        f"be disjoint; overlap: {overlap}. Pick one — debt (will "
        f"migrate) or exempt (permanent)."
    )


def test_bootstrap_files_actually_exist():
    """Each entry in ``_BOOTSTRAP_FILES`` resolves to an existing file
    under the repo root. A typo in the allowlist (e.g.
    ``ap2/conifg.py``) would silently FAIL-OPEN the cut-line gate by
    accepting a non-match against a non-existent path. Pin the
    allowlist's referential integrity here so a typo fails loudly.
    """
    missing = sorted(
        rel for rel in _BOOTSTRAP_FILES
        if not (_REPO_ROOT / rel).is_file()
    )
    assert not missing, (
        f"TB-338 bootstrap allowlist entries name non-existent files: "
        f"{missing}. A typo would silently fail-open the cut-line gate. "
        f"Fix the path or remove the entry."
    )

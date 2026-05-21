"""TB-277: drift-gate pinning every daemon-written `.cc-autopilot/` state file
is classified as EITHER committed (member of `_STATE_FILE_NAMES`, so an
`ap2 rollback` restores it) OR ignored (member of `NESTED_GITIGNORE_BLOCKS`,
so it's runtime-only and never accidentally `git add`'d) — never neither,
never both.

Why this gate exists
--------------------
`ap2 init`'s `.cc-autopilot/.gitignore` template is the only deterministic
source of truth for what an ap2-managed project ignores. Three runtime state
files turned up untracked-and-unignored in two days (an operator scratch
file; `focus_pointer.json` — template-present but the self-hosted repo had
drifted behind its own template; and `daemon_state.json` — genuinely absent
from the template), each one a "new daemon-written file ships without its
gitignore entry, nobody notices until `git status`" recurrence.

This gate models on the existing drift gates (env-knob, MCP-tool, event-type,
CLI-verb) that already enforce "every X is registered/documented" by failing
loudly with an actionable remedy message rather than a bare assert. The
canonical authors for new daemon-written `.cc-autopilot/` files now have
exactly two buckets to choose between — committed (rollback restores) or
ignored (runtime-only) — and a missing classification is a CI failure with
the remedy in plain English.

What this gate canonically enforces
-----------------------------------
- Every file in `_DAEMON_WRITTEN_CCAUTOPILOT_FILES` (the explicit enumeration
  this test maintains) is in EXACTLY ONE of:
    * `_STATE_FILE_NAMES` from `ap2.state_commit` — "committed" bucket.
    * The flat list of patterns in `NESTED_GITIGNORE_BLOCKS` from `ap2.init`
      — "ignored" bucket.
- A file in BOTH buckets would be a rollback-rule contradiction (would be
  committed AND ignored — git treats this as committed but with a "be
  careful" footgun); the test flags it.
- A file in NEITHER bucket is the silent-drift class this gate was built
  to catch — fresh `ap2 init` projects would inherit it as
  untracked-and-unignored, one stray `git add` away from being committed
  when it shouldn't be.

The enumeration `_DAEMON_WRITTEN_CCAUTOPILOT_FILES` is the maintained
contract: any TB adding a new daemon-written `.cc-autopilot/` state file
must (1) extend this enumeration AND (2) classify the file into one of
the two buckets. The dual edit is the whole point — the test fails until
both halves are done.
"""
from __future__ import annotations

from ap2.init import NESTED_GITIGNORE_BLOCKS
from ap2.state_commit import _STATE_FILE_NAMES


# The maintained enumeration of files the daemon writes under `.cc-autopilot/`.
# Names are bare-leaf (no `.cc-autopilot/` prefix) so the gate compares
# directly against the template's bare-leaf entries — that's the shape the
# template stores them in.
#
# Categorized below by their CURRENT classification for documentation; the
# test itself doesn't read the categories — it just asks "is this name in
# committed XOR ignored?".
_DAEMON_WRITTEN_CCAUTOPILOT_FILES: tuple[str, ...] = (
    # ------------------------------------------------------------------
    # Committed — rollback-restored daemon state (members of
    # `_STATE_FILE_NAMES`). Compared against the bare-leaf form here.
    # ------------------------------------------------------------------
    "progress.md",          # daemon appends per-task sections on completion
    "ideation_state.md",    # ideation overwrites each cron cycle
    "cron.yaml",            # operator-curated; daemon stages alongside state
    "retry_state.json",     # per-task retry counter (TB-112)
    "operator_log.md",      # operator-decision log
    # ------------------------------------------------------------------
    # Ignored — ephemeral runtime state (members of
    # `NESTED_GITIGNORE_BLOCKS` template). Rollback must NOT restore
    # these — see the comments next to each entry in `ap2/init.py` for
    # the per-file rationale.
    # ------------------------------------------------------------------
    "events.jsonl",                # append-only daemon log
    "daemon.pid",                  # daemon process pid
    "daemon.log",                  # daemon stdout/stderr capture
    "cron_state.json",             # cron last-fired cursors
    "mm_state.json",               # mattermost poll cursor
    "auto_diagnose_state.json",    # watchdog cooldown bookkeeping
    "operator_queue.jsonl",        # operator-staged board ops (drained)
    "operator_queue_state.json",   # applied-uuid dedup for the queue
    "focus_pointer.json",          # focus-list runtime pointer (TB-226)
    "daemon_state.json",           # env-file-mtime stash (TB-260)
)


def _committed_bare_leaves() -> set[str]:
    """`_STATE_FILE_NAMES` carries fully-qualified paths (e.g.
    `.cc-autopilot/progress.md`); reduce to bare leaves so the comparison
    against `NESTED_GITIGNORE_BLOCKS`'s bare-leaf entries is apples-to-apples.

    Files outside `.cc-autopilot/` (TASKS.md / CLAUDE.md / goal.md) are
    skipped — the gate is scoped to `.cc-autopilot/` per the briefing's
    "scope is the nested daemon-state template only" out-of-scope clause.
    """
    out: set[str] = set()
    for name in _STATE_FILE_NAMES:
        if name.startswith(".cc-autopilot/"):
            out.add(name[len(".cc-autopilot/"):])
    return out


def _ignored_template_entries() -> set[str]:
    """Flatten `NESTED_GITIGNORE_BLOCKS` into a set of bare-leaf patterns."""
    flat: set[str] = set()
    for _header, entries in NESTED_GITIGNORE_BLOCKS:
        flat.update(entries)
    return flat


def test_every_daemon_written_ccautopilot_file_is_committed_xor_ignored():
    """Drift gate: every file in `_DAEMON_WRITTEN_CCAUTOPILOT_FILES` is in
    EXACTLY ONE of `_STATE_FILE_NAMES` (committed) or the flat list of
    `NESTED_GITIGNORE_BLOCKS` (ignored). Never neither, never both.

    When this test fails, the failure message names the offending file AND
    the remedy: classify it into `_STATE_FILE_NAMES` (committed: rollback
    restores) or `NESTED_GITIGNORE_BLOCKS` (ignored: runtime-only). The
    next author who adds a daemon-written `.cc-autopilot/` state file
    without updating one of those buckets — the recurring whack-a-mole
    TB-277 was built to stop — gets actionable guidance, not a bare
    assertion error.

    Pre-TB-277 this test would have FAILED on `daemon_state.json` (TB-260
    added the file but never added the gitignore entry; the file was
    daemon-written, but absent from BOTH `_STATE_FILE_NAMES` and the
    template's ignored list). The TB-277 patch adds it to the template's
    ignored bucket; this test then passes and pins the invariant going
    forward.
    """
    committed = _committed_bare_leaves()
    ignored = _ignored_template_entries()

    unclassified: list[str] = []
    double_classified: list[str] = []
    for name in _DAEMON_WRITTEN_CCAUTOPILOT_FILES:
        in_committed = name in committed
        in_ignored = name in ignored
        if not in_committed and not in_ignored:
            unclassified.append(name)
        elif in_committed and in_ignored:
            double_classified.append(name)

    # Single combined remedy message — actionable for the recurring case
    # this gate was built to stop. The message names both buckets and the
    # exact module symbols so the next author can grep straight to the
    # right edit site.
    problems: list[str] = []
    if unclassified:
        problems.append(
            "the following daemon-written `.cc-autopilot/` file(s) are "
            "NEITHER committed NOR ignored — fresh `ap2 init` projects "
            "would inherit them as untracked-and-unignored, one stray "
            "`git add` from being committed when they shouldn't be: "
            f"{sorted(unclassified)}. Remedy: classify each file as either "
            "committed (add to `_STATE_FILE_NAMES` in ap2/state_commit.py — "
            "rollback will restore it) OR ignored (add to "
            "`NESTED_GITIGNORE_BLOCKS` in ap2/init.py under the runtime "
            "block — rollback will NOT touch it). See the in-line "
            "comments next to existing siblings for the rollback-rule "
            "rationale shape."
        )
    if double_classified:
        problems.append(
            "the following file(s) are in BOTH `_STATE_FILE_NAMES` AND "
            "`NESTED_GITIGNORE_BLOCKS` — contradictory rollback rule: "
            f"{sorted(double_classified)}. Remedy: pick ONE bucket and "
            "remove the file from the other."
        )
    assert not problems, "\n\n".join(problems)


def test_daemon_state_json_is_in_the_init_gitignore_template():
    """TB-277 minimal-patch pin: `daemon_state.json` (TB-260's env-file-mtime
    stash, rewritten each daemon start, rollback must not restore it) is
    enumerated in the init gitignore template's runtime block.

    Paired with the cross-classification gate above so a future refactor
    that moves `daemon_state.json` (e.g. promotes it to committed) trips
    BOTH this specific pin AND the broader XOR gate — the dual signal
    surfaces the deliberate change.
    """
    flat: set[str] = set()
    for _header, entries in NESTED_GITIGNORE_BLOCKS:
        flat.update(entries)
    assert "daemon_state.json" in flat, (
        "daemon_state.json (TB-260 env-file-mtime stash) must be in "
        "`NESTED_GITIGNORE_BLOCKS` so fresh `ap2 init` projects ignore it. "
        "Rewritten at every daemon start; rollback must not restore a "
        "prior mtime baseline."
    )


def test_daemon_state_json_is_enumerated_by_the_drift_gate():
    """Meta-pin: the drift-gate enumeration includes `daemon_state.json`
    explicitly. Catches a refactor that drops the entry from the
    enumeration (which would silently turn off the gate for that file
    — the XOR check only runs over what's enumerated).

    The enumeration is the maintained contract; this test makes sure the
    contract still names the file TB-277 was originally built to catch.
    """
    assert "daemon_state.json" in _DAEMON_WRITTEN_CCAUTOPILOT_FILES, (
        "TB-277 enumeration must include daemon_state.json — that's the "
        "file the gate was originally built to catch (TB-260 added the "
        "file but missed the gitignore entry)."
    )

#!/usr/bin/env bash
# deploy-skills.sh — sync <repo>/skills/* into $HOME/.claude/skills/.
#
# Why this script exists (TB-140): Claude Code's `/ap2`, `/ap2-task`, and
# `/migrate-to-ap2` slash commands read from the deployed copy at
# `$HOME/.claude/skills/<skill>/`. Edits to `<repo>/skills/<skill>/` don't
# take effect until they're copied — without an explicit sync step, repo
# updates drift away from what live operators see. We've already burned
# operator confusion on this (TB-135 pre-TB-140 surfaced repo-only).
#
# Behavior:
#   - Default (no args): dry-run. Print a per-skill diff summary; nothing
#     under $HOME/.claude/skills/ is mutated. Exit 0 even when drift is
#     present (this is a status check, not a gate — the caller decides
#     whether to apply).
#   - `--apply`: copy each skill subdir from <repo>/skills/<name>/ to
#     $HOME/.claude/skills/<name>/. Per-skill `--delete` semantics: files
#     present in the destination but absent in the source are removed
#     (so renames and deletions in the repo propagate). The destination
#     parent ($HOME/.claude/skills/) is NEVER deleted from — sibling
#     skills the repo doesn't own (e.g. `taskboard`, which is a global
#     skill outside this repo) are left untouched.
#   - `--dest <dir>`: override destination root (default $HOME/.claude/skills).
#     Used by tests against a temp dir.
#   - `--source <dir>`: override source root (default <repo>/skills).
#
# Idempotent: repeated `--apply` runs leave the destination identical
# to the source. Exit nonzero on rsync failure or unreadable source.

set -euo pipefail

# Resolve the repo root from the script's location so the script works
# regardless of cwd. `dirname` chain: scripts/deploy-skills.sh → scripts/ → repo
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

SOURCE_ROOT="${REPO_ROOT}/skills"
DEST_ROOT="${HOME}/.claude/skills"
APPLY=0

usage() {
    cat <<'EOF'
usage: deploy-skills.sh [--apply] [--source DIR] [--dest DIR]

Sync <repo>/skills/* into $HOME/.claude/skills/.

  (no args)        dry-run; print per-skill diff summary, no mutations.
  --apply          copy each repo skill onto its deployed copy
                   (per-skill rsync --delete, so renames propagate).
  --source DIR     override source root (default: <repo>/skills).
  --dest DIR       override destination root (default: $HOME/.claude/skills).
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --apply) APPLY=1; shift ;;
        --source) SOURCE_ROOT="$2"; shift 2 ;;
        --dest) DEST_ROOT="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "deploy-skills.sh: unknown arg: $1" >&2; usage >&2; exit 2 ;;
    esac
done

if [[ ! -d "$SOURCE_ROOT" ]]; then
    echo "deploy-skills.sh: source directory not found: $SOURCE_ROOT" >&2
    exit 1
fi

# Collect skill names from the source root. Each direct subdir is a skill.
# Sort for stable output ordering across runs.
SKILLS=()
while IFS= read -r d; do
    name="$(basename "$d")"
    SKILLS+=("$name")
done < <(find "$SOURCE_ROOT" -mindepth 1 -maxdepth 1 -type d | sort)

if [[ ${#SKILLS[@]} -eq 0 ]]; then
    echo "deploy-skills.sh: no skills under $SOURCE_ROOT — nothing to sync"
    exit 0
fi

# Make sure the destination root exists (only on apply — dry-run leaves
# the filesystem entirely untouched).
if [[ "$APPLY" -eq 1 ]]; then
    mkdir -p "$DEST_ROOT"
fi

mode_label="dry-run"
[[ "$APPLY" -eq 1 ]] && mode_label="apply"

echo "deploy-skills.sh: ${mode_label}"
echo "  source: ${SOURCE_ROOT}"
echo "  dest:   ${DEST_ROOT}"
echo

OVERALL_DRIFT=0
for name in "${SKILLS[@]}"; do
    src="${SOURCE_ROOT}/${name}"
    dst="${DEST_ROOT}/${name}"

    # Compute drift summary using rsync --dry-run --itemize-changes. Trailing
    # slashes on src/ matter: rsync mirrors src's *contents* into dst, not
    # src itself. Without them rsync would create dst/<name>/<name>/.
    if [[ -d "$dst" ]]; then
        # Count files that differ (sent by rsync) and files that would be
        # deleted (only present in dst). `--itemize-changes` formats:
        #   >f.....  path     # would send file
        #   *deleting path    # would delete from dst
        diff_lines="$(rsync -an --delete --itemize-changes "$src/" "$dst/" 2>/dev/null \
            | grep -E '^(>|<|c|\*deleting)' || true)"
    else
        # Destination doesn't exist yet — every file in src is "new". Use
        # an empty staging dir so rsync's itemize output reflects the full
        # initial copy. The staging dir is removed on exit.
        staging="$(mktemp -d -t deploy-skills.XXXXXX)"
        trap 'rm -rf "$staging"' EXIT
        diff_lines="$(rsync -an --itemize-changes "$src/" "$staging/" 2>/dev/null \
            | grep -E '^(>|<|c)' || true)"
        rm -rf "$staging"
        trap - EXIT
    fi

    sent=0
    deleted=0
    if [[ -n "$diff_lines" ]]; then
        sent=$(printf '%s\n' "$diff_lines" | grep -cE '^[<>c]' || true)
        deleted=$(printf '%s\n' "$diff_lines" | grep -cE '^\*deleting' || true)
    fi
    drift=$((sent + deleted))

    if [[ "$drift" -eq 0 ]]; then
        printf '  %-20s in sync\n' "${name}:"
        continue
    fi

    OVERALL_DRIFT=$((OVERALL_DRIFT + drift))
    if [[ "$APPLY" -eq 1 ]]; then
        rsync -a --delete "$src/" "$dst/"
        printf '  %-20s synced (%d updated, %d deleted)\n' \
            "${name}:" "$sent" "$deleted"
    else
        printf '  %-20s drift (%d would update, %d would delete)\n' \
            "${name}:" "$sent" "$deleted"
    fi
done

echo
if [[ "$APPLY" -eq 1 ]]; then
    echo "deploy-skills.sh: apply complete."
else
    if [[ "$OVERALL_DRIFT" -eq 0 ]]; then
        echo "deploy-skills.sh: dry-run — all skills in sync."
    else
        echo "deploy-skills.sh: dry-run — drift detected. Re-run with --apply to sync."
    fi
fi

exit 0

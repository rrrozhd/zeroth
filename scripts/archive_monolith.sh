#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT_DIR=$(cd "$SCRIPT_DIR/.." && pwd)
PHASE_DIR="$ROOT_DIR/.planning/phases/27-ship-zeroth-as-pip-installable-library-zeroth-core"
ARTIFACT_DIR="$PHASE_DIR/artifacts"
INVENTORY_PATH="$ARTIFACT_DIR/archive-inventory.txt"
PREFLIGHT_PATH="$ARTIFACT_DIR/archive-preflight.txt"
ARCHIVE_ROOT="$HOME/archives/zeroth-monolith"
TARBALL_PATH="$ARCHIVE_ROOT/zeroth-monolith-2026-04-10.tar.gz"
MIRROR_PATH="$ARCHIVE_ROOT/zeroth-monolith.git"
SNAPSHOT_DIR="$ARCHIVE_ROOT/tarball-staging"

record_inventory() {
  mkdir -p "$ARTIFACT_DIR"

  cat >"$INVENTORY_PATH" <<EOF
worktrees=$(git -C "$ROOT_DIR" worktree list | wc -l | tr -d ' ')
detached_worktrees=$(git -C "$ROOT_DIR" worktree list | rg -c '\(detached HEAD\)')
branches=$(git -C "$ROOT_DIR" branch -a | wc -l | tr -d ' ')
stashes=$(git -C "$ROOT_DIR" stash list | wc -l | tr -d ' ')
python_subpackages=$(find "$ROOT_DIR/src/zeroth" -maxdepth 1 -mindepth 1 -type d | rg -v '__pycache__' | wc -l | tr -d ' ')
EOF
}

create_archive_refs() {
  local stash_index stash_ref stash_branch sha detached_branch

  stash_index=0
  while IFS= read -r stash_ref; do
    [ -n "$stash_ref" ] || continue
    stash_branch="archive/stash-${stash_index}"
    git -C "$ROOT_DIR" branch -f "$stash_branch" "$stash_ref" >/dev/null
    stash_index=$((stash_index + 1))
  done < <(git -C "$ROOT_DIR" stash list | cut -d: -f1)

  while IFS= read -r sha; do
    [ -n "$sha" ] || continue
    detached_branch="archive/detached-wt-${sha:0:12}"
    git -C "$ROOT_DIR" branch -f "$detached_branch" "$sha" >/dev/null
  done < <(
    git -C "$ROOT_DIR" worktree list --porcelain | awk '
      /^HEAD / { head=$2 }
      /^detached$/ { print head }
    '
  )
}

create_tarball() {
  mkdir -p "$ARCHIVE_ROOT"
  rm -rf "$SNAPSHOT_DIR"

  git clone --no-local "$ROOT_DIR" "$SNAPSHOT_DIR" >/dev/null 2>&1

  rsync -a --delete \
    --exclude '.git' \
    --exclude '.venv' \
    --exclude '__pycache__' \
    --exclude '.pytest_cache' \
    --exclude 'node_modules' \
    "$ROOT_DIR/" "$SNAPSHOT_DIR/"

  tar -C "$ARCHIVE_ROOT" -czf "$TARBALL_PATH" "$(basename "$SNAPSHOT_DIR")"
  rm -rf "$SNAPSHOT_DIR"
}

create_mirror() {
  mkdir -p "$ARCHIVE_ROOT"
  rm -rf "$MIRROR_PATH"
  git clone --mirror "$ROOT_DIR" "$MIRROR_PATH" >/dev/null 2>&1
}

write_preflight_report() {
  local tarball_sha tarball_size mirror_ref_count archive_branch_count

  tarball_sha=$(shasum -a 256 "$TARBALL_PATH" | awk '{print $1}')
  tarball_size=$(wc -c <"$TARBALL_PATH" | tr -d ' ')
  mirror_ref_count=$(git -C "$MIRROR_PATH" for-each-ref | wc -l | tr -d ' ')
  archive_branch_count=$(git -C "$ROOT_DIR" for-each-ref refs/heads/archive/ | wc -l | tr -d ' ')

  cat >"$PREFLIGHT_PATH" <<EOF
tarball_path=$TARBALL_PATH
tarball_sha256=$tarball_sha
tarball_size_bytes=$tarball_size
mirror_path=$MIRROR_PATH
mirror_ref_count=$mirror_ref_count
archive_branch_count=$archive_branch_count
EOF
}

main() {
  record_inventory
  create_archive_refs
  create_tarball
  create_mirror
  write_preflight_report
}

main "$@"

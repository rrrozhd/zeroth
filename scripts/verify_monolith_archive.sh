#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT_DIR=$(cd "$SCRIPT_DIR/.." && pwd)
PHASE_DIR="$ROOT_DIR/.planning/phases/27-ship-zeroth-as-pip-installable-library-zeroth-core"
ARTIFACT_DIR="$PHASE_DIR/artifacts"
PREFLIGHT_PATH="$ARTIFACT_DIR/archive-preflight.txt"
ARCHIVE_ROOT="$HOME/archives/zeroth-monolith"
MIRROR_PATH="$ARCHIVE_ROOT/zeroth-monolith.git"
RECOVERY_DIR="/tmp/zeroth-monolith-recovery-test"

checkout_and_log() {
  local branch_name="$1"
  local remote_ref="$2"

  printf 'checkout=%s\n' "$branch_name" >>"$PREFLIGHT_PATH"
  git -C "$RECOVERY_DIR" checkout -B "$branch_name" "$remote_ref" >/dev/null 2>&1
  git -C "$RECOVERY_DIR" branch --show-current >>"$PREFLIGHT_PATH"
  git -C "$RECOVERY_DIR" log --oneline -5 >>"$PREFLIGHT_PATH"
  printf '\n' >>"$PREFLIGHT_PATH"
}

main() {
  local expected_stashes expected_detached normal_branch stash_branch

  if [ ! -d "$MIRROR_PATH" ]; then
    echo "Mirror not found: $MIRROR_PATH" >&2
    exit 1
  fi

  expected_stashes=$(git -C "$ROOT_DIR" stash list | wc -l | tr -d ' ')
  expected_detached=$(git -C "$ROOT_DIR" worktree list | rg -c '\(detached HEAD\)')

  if [ "$(git -C "$MIRROR_PATH" for-each-ref --format='%(refname:short)' refs/heads/archive | grep -c '^archive/stash-')" -lt "$expected_stashes" ]; then
    echo "Mirror is missing one or more archive/stash-* refs" >&2
    exit 1
  fi

  if [ "$(git -C "$MIRROR_PATH" for-each-ref --format='%(refname:short)' refs/heads/archive | grep -c '^archive/detached-wt-')" -lt "$expected_detached" ]; then
    echo "Mirror is missing one or more archive/detached-wt-* refs" >&2
    exit 1
  fi

  rm -rf "$RECOVERY_DIR"
  git clone "$MIRROR_PATH" "$RECOVERY_DIR" >/dev/null 2>&1

  normal_branch=$(git -C "$MIRROR_PATH" for-each-ref --format='%(refname:short)' refs/heads | grep -v '^archive/' | head -n 1)
  stash_branch=$(git -C "$MIRROR_PATH" for-each-ref --format='%(refname:short)' refs/heads/archive | grep '^archive/stash-' | head -n 1)

  printf 'recovery_dir=%s\n' "$RECOVERY_DIR" >>"$PREFLIGHT_PATH"
  checkout_and_log "$normal_branch" "origin/$normal_branch"
  checkout_and_log "$stash_branch" "origin/$stash_branch"

  while IFS= read -r detached_branch; do
    [ -n "$detached_branch" ] || continue
    checkout_and_log "$detached_branch" "origin/$detached_branch"
  done < <(git -C "$MIRROR_PATH" for-each-ref --format='%(refname:short)' refs/heads/archive | grep '^archive/detached-wt-')
}

main "$@"

#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT_DIR=$(cd "$SCRIPT_DIR/.." && pwd)
PHASE_DIR="$ROOT_DIR/.planning/phases/27-ship-zeroth-as-pip-installable-library-zeroth-core"
ARTIFACT_DIR="$PHASE_DIR/artifacts"
LOG_PATH="$ARTIFACT_DIR/archive-github-publish.txt"
ARCHIVE_ROOT="$HOME/archives/zeroth-monolith"
MIRROR_PATH="$ARCHIVE_ROOT/zeroth-monolith.git"
ARCHIVE_REMOTE="git@github.com:rrrozhd/zeroth-archive.git"
README_CLONE_DIR="/tmp/zeroth-archive-readme"
ARCHIVE_DESCRIPTION="Archived monolithic Zeroth repo — see rrrozhd/zeroth-core and rrrozhd/zeroth-studio"
BANNER='⚠️ **This repository is archived.** Active development continues in `rrrozhd/zeroth-core` (Python library) and `rrrozhd/zeroth-studio` (Vue frontend). This repo exists only to preserve the pre-split monolith history.'

run_cmd() {
  printf '$ %s\n' "$*"
  "$@"
}

ensure_repo_writable() {
  local archived_state

  archived_state=$(gh api repos/rrrozhd/zeroth-archive --jq '.archived')
  if [ "$archived_state" = "true" ]; then
    run_cmd gh api -X PATCH repos/rrrozhd/zeroth-archive -F archived=false
  fi
}

prepend_banner() {
  local readme_path="$1"
  local temp_path

  if [ ! -f "$readme_path" ]; then
    : >"$readme_path"
  fi

  if grep -Fqx "$BANNER" "$readme_path"; then
    return 0
  fi

  temp_path=$(mktemp)
  printf '%s\n\n' "$BANNER" >"$temp_path"
  cat "$readme_path" >>"$temp_path"
  mv "$temp_path" "$readme_path"
}

main() {
  mkdir -p "$ARTIFACT_DIR"
  : >"$LOG_PATH"
  exec > >(tee -a "$LOG_PATH") 2>&1

  if [ ! -d "$MIRROR_PATH" ]; then
    echo "Local mirror not found: $MIRROR_PATH" >&2
    exit 1
  fi

  if ! gh repo view rrrozhd/zeroth-archive >/dev/null 2>&1; then
    run_cmd gh repo create rrrozhd/zeroth-archive --public --description "$ARCHIVE_DESCRIPTION"
  else
    run_cmd gh repo view rrrozhd/zeroth-archive --json name,description,isArchived
  fi

  ensure_repo_writable
  run_cmd gh repo edit rrrozhd/zeroth-archive --visibility public --accept-visibility-change-consequences

  if git -C "$MIRROR_PATH" remote get-url origin >/dev/null 2>&1; then
    run_cmd git -C "$MIRROR_PATH" remote set-url origin "$ARCHIVE_REMOTE"
  else
    run_cmd git -C "$MIRROR_PATH" remote add origin "$ARCHIVE_REMOTE"
  fi

  run_cmd git -C "$HOME/archives/zeroth-monolith/zeroth-monolith.git" push --mirror origin

  rm -rf "$README_CLONE_DIR"
  run_cmd git clone "$ARCHIVE_REMOTE" "$README_CLONE_DIR"
  prepend_banner "$README_CLONE_DIR/README.md"

  if ! git -C "$README_CLONE_DIR" diff --quiet -- README.md; then
    run_cmd git -C "$README_CLONE_DIR" add README.md
    run_cmd git -C "$README_CLONE_DIR" commit -m "docs: add archived repository notice"
    run_cmd git -C "$README_CLONE_DIR" push origin HEAD
  fi

  run_cmd gh repo edit rrrozhd/zeroth-archive --description "$ARCHIVE_DESCRIPTION"
  run_cmd gh repo archive rrrozhd/zeroth-archive --yes
}

main "$@"

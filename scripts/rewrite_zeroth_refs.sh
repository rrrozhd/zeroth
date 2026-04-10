#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Explicit rewrites owned by this pass:
# - name = "zeroth-core"
# - packages = ["src/zeroth/core"]
# - python -m zeroth.core.service.entrypoint
# - script_location = src/zeroth/core/migrations
# - Dockerfile / docker-compose.yml / README.md / *.md / *.yml / *.yaml / *.toml

uv run python "${ROOT_DIR}/scripts/rename_to_zeroth_core.py" --rewrite-text

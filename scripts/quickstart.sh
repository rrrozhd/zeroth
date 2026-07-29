#!/usr/bin/env bash
# Zeroth quickstart — clone, install, and serve a demo deployment (API + web
# console) in one command:
#
#   curl -fsSL https://raw.githubusercontent.com/rrrozhd/zeroth/main/scripts/quickstart.sh | bash
#
# or, from a checkout:
#
#   ./scripts/quickstart.sh
#
# What it does:
#   1. Installs uv (the Python package manager) if missing.
#   2. Clones this repository (skipped when already inside a checkout).
#   3. Installs Python dependencies (uv provisions Python 3.12 itself).
#   4. Builds the web console when Node 20+ is available (optional).
#   5. Boots examples/10_serve_in_python.py: a deployed Q&A graph served on
#      http://127.0.0.1:8000 with the console at /console/.
#
# Environment overrides:
#   ZEROTH_DIR           checkout directory (default: zeroth)
#   ZEROTH_EXAMPLE_PORT  port to serve on (default: 8000)
#   OPENAI_API_KEY       LLM key for the demo agent (prompted if unset)
set -euo pipefail

REPO_URL="https://github.com/rrrozhd/zeroth.git"
DIR="${ZEROTH_DIR:-zeroth}"
PORT="${ZEROTH_EXAMPLE_PORT:-8000}"

say()  { printf '\033[1m[zeroth]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[zeroth]\033[0m %s\n' "$*" >&2; exit 1; }

command -v git >/dev/null 2>&1 || fail "git is required — install it and re-run."
command -v curl >/dev/null 2>&1 || fail "curl is required — install it and re-run."

# 1. uv — official installer, lands in ~/.local/bin.
if ! command -v uv >/dev/null 2>&1; then
  say "Installing uv (Python package manager)…"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
  command -v uv >/dev/null 2>&1 || fail "uv installed but not on PATH — open a new shell and re-run."
fi

# 2. Repository — reuse the current checkout when run from inside one.
if [ -f pyproject.toml ] && grep -q '^name = "zeroth-core"' pyproject.toml 2>/dev/null; then
  say "Running from an existing checkout: $(pwd)"
elif [ -d "$DIR/.git" ]; then
  say "Using existing checkout: $DIR"
  cd "$DIR"
else
  say "Cloning $REPO_URL → $DIR"
  git clone --depth 1 "$REPO_URL" "$DIR"
  cd "$DIR"
fi

# 3. Python dependencies.
say "Installing Python dependencies (uv sync)…"
uv sync

# 4. Web console (optional — the API works without it).
if [ -f frontend/out/index.html ]; then
  say "Web console build already present."
elif command -v npm >/dev/null 2>&1; then
  say "Building the web console (one-time, ~1 min)…"
  (cd frontend && npm install --no-fund --no-audit && npm run build)
else
  say "Node/npm not found — skipping the web console build. The API still works."
  say "To add the console later: install Node 20+, then: cd frontend && npm install && npm run build"
fi

# 5. LLM key for the demo agent.
if [ -z "${OPENAI_API_KEY:-}" ]; then
  if [ -t 0 ]; then
    read -rp "$(printf '\033[1m[zeroth]\033[0m OPENAI_API_KEY is not set — paste one (Enter to skip): ')" OPENAI_API_KEY || true
    export OPENAI_API_KEY
  fi
  if [ -z "${OPENAI_API_KEY:-}" ]; then
    export OPENAI_API_KEY="sk-placeholder-no-real-key"
    say "No LLM key — runs will fail at the agent step, but you can explore the console,"
    say "Studio templates, and the API. Re-run with OPENAI_API_KEY set for live answers."
  fi
fi

say "Starting the demo deployment…"
say "  API:      http://127.0.0.1:$PORT        (health: /health)"
say "  Console:  http://127.0.0.1:$PORT/console/"
say "  API key:  demo-operator-key   (paste it in the console's Connect bar)"
say "Stop with Ctrl-C."
exec env ZEROTH_EXAMPLE_PORT="$PORT" uv run python examples/10_serve_in_python.py

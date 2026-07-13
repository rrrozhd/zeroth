#!/usr/bin/env bash
# Build the zeroth-console distribution: compile the Next.js static export,
# copy it into the zeroth_console package, sync the version from the root
# pyproject.toml, and build sdist + wheel into dist/ (alongside zeroth-core's).
#
# Usage: scripts/build_console_dist.sh [--skip-frontend-build]
#   --skip-frontend-build  reuse an existing frontend/out (CI builds it earlier)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONSOLE_PKG="$ROOT/packaging/console"
STATIC_DIR="$CONSOLE_PKG/src/zeroth_console/static"

# 1. Frontend static export.
if [ "${1:-}" != "--skip-frontend-build" ]; then
  echo "[console-dist] Building frontend static export…"
  (cd "$ROOT/frontend" && npm install --no-fund --no-audit && npm run build)
fi
if [ ! -f "$ROOT/frontend/out/index.html" ]; then
  echo "[console-dist] ERROR: frontend/out/index.html not found — build the frontend first." >&2
  exit 1
fi

# 2. Copy the export into the package (fresh each time).
rm -rf "$STATIC_DIR"
mkdir -p "$STATIC_DIR"
cp -R "$ROOT/frontend/out/." "$STATIC_DIR/"

# 3. Version lockstep with the root package. Plain-regex parsing (not tomllib)
# so the script works on any python3, including pre-3.11.
read_version() {
  python3 - "$1" <<'EOF'
import re, sys
text = open(sys.argv[1]).read()
print(re.search(r'(?m)^version = "([^"]+)"$', text).group(1))
EOF
}
ROOT_VERSION=$(read_version "$ROOT/pyproject.toml")
CONSOLE_VERSION=$(read_version "$CONSOLE_PKG/pyproject.toml")
if [ "$ROOT_VERSION" != "$CONSOLE_VERSION" ]; then
  echo "[console-dist] Syncing zeroth-console version $CONSOLE_VERSION -> $ROOT_VERSION"
  python3 - "$CONSOLE_PKG/pyproject.toml" "$ROOT_VERSION" <<'EOF'
import re, sys
path, version = sys.argv[1], sys.argv[2]
text = open(path).read()
text = re.sub(r'(?m)^version = "[^"]+"$', f'version = "{version}"', text, count=1)
open(path, "w").write(text)
EOF
fi

# 4. Build into the repo-root dist/ so release publishing picks up both dists.
echo "[console-dist] Building sdist + wheel…"
uv build "$CONSOLE_PKG" -o "$ROOT/dist"
echo "[console-dist] Done:"
ls -la "$ROOT/dist" | grep -i console

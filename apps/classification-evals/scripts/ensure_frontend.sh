#!/usr/bin/env bash
# Build the workbench frontend bundle when it is missing.
# start.sh runs this before serving, so a clean checkout serves the app
# at / instead of a missing-bundle 404.
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
PRODUCT_APP_ROOT="${PRODUCT_APP_ROOT:-$DIR/../product}"
FRONTEND_DIR="$PRODUCT_APP_ROOT/frontend"
DIST_INDEX="$FRONTEND_DIR/dist/index.html"

if [ -f "$DIST_INDEX" ]; then
  exit 0
fi

if ! command -v npm >/dev/null 2>&1; then
  echo "error: frontend bundle missing: $DIST_INDEX" >&2
  echo "error: npm not found. Install Node.js and npm, or build by hand:" >&2
  echo "error:   cd $FRONTEND_DIR && npm ci && npm run build" >&2
  exit 1
fi

echo "building frontend bundle (first run): $FRONTEND_DIR"
(
  cd "$FRONTEND_DIR"
  npm ci
  npm run build
)

if [ ! -f "$DIST_INDEX" ]; then
  echo "error: frontend build finished but $DIST_INDEX is still missing" >&2
  exit 1
fi

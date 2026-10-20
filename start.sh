#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"

if [ -f "$DIR/.env" ]; then
  set -a
  source "$DIR/.env"
  set +a
fi

cd "$DIR"
if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
if [ ! -f ".venv/.worker-installed" ] || [ "pyproject.toml" -nt ".venv/.worker-installed" ]; then
  pip install -e .
  touch ".venv/.worker-installed"
fi

exec uvicorn worker.app:app \
  --host "${CLASSIFY_EVAL_HOST:-127.0.0.1}" \
  --port "${CLASSIFY_EVAL_PORT:-8100}" \
  --no-access-log

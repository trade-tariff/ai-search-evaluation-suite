#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"

if [ -f "$DIR/.env" ]; then
  set -a
  source "$DIR/.env"
  set +a
fi

DEFAULT_PRODUCT_APP_ROOT="$DIR/../product"
export PRODUCT_APP_ROOT="${PRODUCT_APP_ROOT:-$DEFAULT_PRODUCT_APP_ROOT}"
export PYTHONPATH="${PRODUCT_APP_ROOT}/backend${PYTHONPATH:+:$PYTHONPATH}"

cd "$DIR"
if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
if [ ! -f ".venv/.worker-requirements-installed" ] \
  || [ "requirements-worker.txt" -nt ".venv/.worker-requirements-installed" ]; then
  pip install -r requirements-worker.txt
  touch ".venv/.worker-requirements-installed"
fi

exec uvicorn backend.worker_app:app \
  --host "${CLASSIFY_EVAL_HOST:-127.0.0.1}" \
  --port "${CLASSIFY_EVAL_PORT:-8100}" \
  --no-access-log

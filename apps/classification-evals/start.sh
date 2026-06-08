#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"

if [ -f "$DIR/.env" ]; then
  set -a
  source "$DIR/.env"
  set +a
fi

DEFAULT_PRODUCT_APP_ROOT="$DIR/../product"
export PRODUCT_APP_ROOT="-e"
export CLASSIFY_EVAL_STATE_DIR="${CLASSIFY_EVAL_STATE_DIR:-$DIR/var}"
export AI_FAN_OUT_KG_LABEL_PROFILE="${AI_FAN_OUT_KG_LABEL_PROFILE:-full}"
export PYTHONPATH="${PRODUCT_APP_ROOT}/backend${PYTHONPATH:+:$PYTHONPATH}"
mkdir -p "$CLASSIFY_EVAL_STATE_DIR"

cd "$DIR"
if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
PRODUCT_REQUIREMENTS="${PRODUCT_APP_ROOT}/backend/requirements.txt"
if [ ! -f ".venv/.requirements-installed" ] \
  || [ "requirements.txt" -nt ".venv/.requirements-installed" ] \
  || [ "$PRODUCT_REQUIREMENTS" -nt ".venv/.requirements-installed" ]; then
  pip install -r requirements.txt
  touch ".venv/.requirements-installed"
fi

exec uvicorn backend.app:app \
  --host "${CLASSIFY_EVAL_HOST:-127.0.0.1}" \
  --port "${CLASSIFY_EVAL_PORT:-8100}"

#!/usr/bin/env bash
# oracle.sh <row_id> "<question>"  -> simulated importer's answer (from ATAR facts only)
set -euo pipefail
set -a; source "$HOME/Desktop/Work/HMRC/.env"; set +a   # OPENAI_API_KEY
DIR="$(cd "$(dirname "$0")" && pwd)"
python3 "$DIR/oracle.py" "${ORACLE_SAMPLE:-$DIR/../eval_sample210.json}" "$1" "$2"

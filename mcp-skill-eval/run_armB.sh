#!/usr/bin/env bash
# Arm B runner via `claude -p` (no Anthropic API key needed - reuses the logged-in Claude CLI).
# Portable: runs locally now; runs on the EC2 once `claude login` is done there (adjust the
# absolute paths below to the EC2 checkout). Idempotent: skips ids that already have a result,
# so it doubles as a backfill for any rows the workflow missed.
#
# Usage:  PAR=6 ./run_armB.sh         (PAR = parallel claude -p jobs; default 6)
set -uo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
SK=/Users/dragusingabriel/Desktop/Work/HMRC/customs-skills/uk-commodity-code-classifier/SKILL.md
EV="$DIR/eval"
SAMPLE="$DIR/eval_sample210.json"
OUT=/tmp/armB_results
PAR="${PAR:-6}"
mkdir -p "$OUT"

classify_one() {
  local id="$1"
  [ -s "$OUT/$id.json" ] && { echo "skip $id"; return; }
  local prompt="Classify ONE product to a UK commodity code (service=uk) by following a skill.
Product is in this file: /tmp/armB_tasks/$id.txt (read it).
Read and follow this skill (+ its references/ folder): $SK
Call the OTT MCP via Bash: $EV/mcp_call.sh <tool> '<json>' (tools, always service uk: classification_search {\"query\":...,\"limit\":20,\"service\":\"uk\"}; lookup_commodity {\"commodity_code\":\"##########\",\"service\":\"uk\"}; show_heading {\"heading_id\":\"....\",\"service\":\"uk\"}; navigate_hierarchy {\"code\":\"...\",\"service\":\"uk\"}; show_chapter {\"chapter_id\":\"..\",\"service\":\"uk\"}. note_mentions currently returns a backend 422 - skip it, use the hierarchy.
Ask the importer via Bash: $EV/oracle.sh $id \"your question\" (max 5, only if a missing fact changes the code).
Resolve by the GIRs to ONE declarable 10-digit code (confirm declarable via lookup_commodity). Then write the answer to $OUT/$id.json EXACTLY as {\"commodity_code\":\"##########\",\"gir\":\"...\",\"alternatives\":[\"...\"],\"questions_asked\":N}."
  claude -p "$prompt" --allowedTools "Bash Read" --output-format text >/dev/null 2>&1
  [ -s "$OUT/$id.json" ] && echo "done $id" || echo "FAIL $id"
}
export -f classify_one; export SK EV OUT

python3 -c "import json;[print(r['id']) for r in json.load(open('$SAMPLE'))]" \
  | xargs -P "$PAR" -I{} bash -c 'classify_one "$@"' _ {}
echo "results: $(ls "$OUT"/*.json 2>/dev/null | wc -l)/210"

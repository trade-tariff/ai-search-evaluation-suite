#!/usr/bin/env bash
# Claude (Opus) MCP arm via claude -p over the 40-row arm_sample, ranked top-10.
# Mirrors the gpt-5.5 arm_openai_top10 but driven by the local Claude CLI (no Anthropic key needed).
# Idempotent: skips ids with a result. Usage: PAR=6 ./run_claude_mcp.sh
set -uo pipefail
DIR=/Users/dragusingabriel/Desktop/Work/HMRC/mcp-bench
SK=/Users/dragusingabriel/Desktop/Work/HMRC/customs-skills/uk-commodity-code-classifier/SKILL.md
EV=$DIR/eval
SAMPLE=$DIR/arm_sample40.json
OUT=/tmp/mcp_claude
export ORACLE_SAMPLE=$SAMPLE
PAR="${PAR:-6}"
mkdir -p "$OUT" /tmp/mcp_claude_tasks

classify_one() {
  local id="$1"
  [ -s "$OUT/$id.json" ] && { echo "skip $id"; return; }
  local prompt="Classify ONE product to a UK commodity code (service=uk) by following a skill.
Product is in: /tmp/mcp_claude_tasks/$id.txt (read it). Follow this skill (+ its references/): $SK
Call the OTT MCP via Bash: $EV/mcp_call.sh <tool> '<json>' (tools, always service uk: classification_search {\"query\":...,\"limit\":20,\"service\":\"uk\"}; lookup_commodity {\"commodity_code\":\"##########\",\"service\":\"uk\"}; show_heading {\"heading_id\":\"....\",\"service\":\"uk\"}; navigate_hierarchy {\"code\":\"...\",\"service\":\"uk\"}; show_chapter {\"chapter_id\":\"..\",\"service\":\"uk\"}. note_mentions is broken (422) - skip it.
Ask the importer via Bash: $EV/oracle.sh $id \"your question\" (max 5, only if a missing fact changes the code).
Resolve by the GIRs. Then write a RANKED top-10 to $OUT/$id.json EXACTLY as {\"commodity_code\":\"##########\",\"ranked_codes\":[\"...\" up to 10, best first],\"gir\":\"...\"} (10 digits no spaces; ranked_codes[0] == commodity_code)."
  claude -p "$prompt" --allowedTools "Bash Read" --output-format text >/dev/null 2>&1
  [ -s "$OUT/$id.json" ] && echo "done $id" || echo "FAIL $id"
}
export -f classify_one; export SK EV OUT ORACLE_SAMPLE

python3 -c "import json;[print(r['id']) for r in json.load(open('$SAMPLE'))]" \
  | xargs -P "$PAR" -I{} bash -c 'classify_one "$@"' _ {}
echo "claude mcp done: $(ls "$OUT"/*.json 2>/dev/null | wc -l)/40"

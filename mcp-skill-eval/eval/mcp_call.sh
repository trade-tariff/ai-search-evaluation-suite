#!/usr/bin/env bash
# MCP tool caller with token caching/refresh. Usage:
#   mcp_call.sh <tool_name> '<json-arguments>'
# e.g. mcp_call.sh classification_search '{"query":"leather boots","limit":20,"service":"uk"}'
# Returns the tool's text payload. Sources OTT creds from ~/.claude/.env.
set -euo pipefail
set -a; source "$HOME/.claude/.env"; set +a
TOKF=/tmp/ott_tok.txt
need_refresh=1
if [ -f "$TOKF" ]; then
  age=$(( $(date +%s) - $(stat -f %m "$TOKF") ))
  [ "$age" -lt 3000 ] && need_refresh=0
fi
if [ "$need_refresh" -eq 1 ]; then
  curl -s --max-time 20 -X POST https://auth.id.trade-tariff.service.gov.uk/oauth2/token \
    -H 'Content-Type: application/x-www-form-urlencoded' \
    -d "grant_type=client_credentials&client_id=${OTT_HUB_CLIENT_ID}&client_secret=${OTT_HUB_CLIENT_SECRET}&scope=tariff/read" \
    | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])' > "$TOKF"
fi
TOKEN=$(cat "$TOKF")
PAYLOAD=$(python3 -c 'import sys,json;print(json.dumps({"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":sys.argv[1],"arguments":json.loads(sys.argv[2])}}))' "$1" "$2")
curl -s --max-time 60 -X POST https://mcp.trade-tariff.service.gov.uk/ \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' -d "$PAYLOAD" \
  | python3 -c 'import sys,json
d=json.load(sys.stdin)
c=d.get("result",{}).get("content",[])
print(c[0]["text"] if c else json.dumps(d))'
